package worker

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/manifest"
)

func testKeyPair(t *testing.T) (string, ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i + 1)
	}
	priv := ed25519.NewKeyFromSeed(seed)
	pub := priv.Public().(ed25519.PublicKey)
	return "test-key", pub, priv
}

func makeTestZipBytes(size int) []byte {
	if size < 4 {
		size = 4
	}
	b := make([]byte, size)
	copy(b, []byte{'P', 'K', 0x03, 0x04})
	for i := 4; i < size; i++ {
		b[i] = byte((i * 31) % 251)
	}
	return b
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func signTestManifest(t *testing.T, keyID string, priv ed25519.PrivateKey, version, fileName string, size int64, sha string, urls []string) []byte {
	t.Helper()
	asset := map[string]any{
		"file":    fileName,
		"url":     urls[0],
		"size":    size,
		"sha256":  sha,
		"format":  "zip",
		"layout":  "onedir",
		"entry":   "好想跑.exe",
		"version": version,
	}
	if len(urls) > 1 {
		urlList := make([]any, len(urls))
		for i, u := range urls {
			urlList[i] = u
		}
		asset["urls"] = urlList
	}
	payload := map[string]any{
		"schema":      1,
		"app":         "haoxiangpao",
		"name":        "好想跑",
		"channel":     "stable",
		"version":     version,
		"min_version": "0.1.4",
		"released_at": "2026-11-12T12:00:00+08:00",
		"notes":       []any{"note"},
		"assets": map[string]any{
			"windows-x64": asset,
		},
	}
	canonical, err := manifest.CanonicalJSON(payload)
	if err != nil {
		t.Fatalf("CanonicalJSON: %v", err)
	}
	msg := make([]byte, 0, len(keyID)+1+len(canonical))
	msg = append(msg, keyID...)
	msg = append(msg, 0)
	msg = append(msg, canonical...)
	sig := ed25519.Sign(priv, msg)
	payload["signature"] = map[string]any{
		"alg":    "ed25519",
		"key_id": keyID,
		"value":  base64.StdEncoding.EncodeToString(sig),
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("Marshal signed manifest: %v", err)
	}
	return raw
}

func serveManifest(t *testing.T, raw []byte) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(raw)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestSuccessfulDownloadAndStateReady(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(4096)
	zipSHA := sha256Hex(zipData)

	pkgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", strconv.Itoa(len(zipData)))
		_, _ = w.Write(zipData)
	}))
	defer pkgSrv.Close()

	manifestBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		pkgSrv.URL + "/haoxiangpao_v0.3.0_win64.zip",
	})
	manifestSrv := serveManifest(t, manifestBytes)

	dataDir := t.TempDir()
	logPath := filepath.Join(dataDir, "updates", "launcher.log")
	logger := logx.Open(logPath)
	defer logger.Close()

	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{manifestSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
		Logger:       logger,
	})
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}

	st, ok, err := LoadState(StatePath(dataDir))
	if err != nil || !ok {
		t.Fatalf("LoadState failed: ok=%v err=%v", ok, err)
	}
	if st.Phase != PhaseReady {
		t.Fatalf("expected phase=ready, got %q", st.Phase)
	}
	wantPkg := filepath.Join(dataDir, "updates", "haoxiangpao_v0.3.0_win64.zip")
	if st.Package != wantPkg {
		t.Fatalf("expected package=%q, got %q", wantPkg, st.Package)
	}
	if st.LastError != "" {
		t.Fatalf("expected empty last_error, got %q", st.LastError)
	}
	if err := VerifyPackageFile(st.Package, int64(len(zipData)), zipSHA); err != nil {
		t.Fatalf("package verification failed: %v", err)
	}
	if _, err := os.Stat(wantPkg + ".part"); !os.IsNotExist(err) {
		t.Fatalf("expected .part to be renamed away, stat err=%v", err)
	}
}

func TestMirrorFallbackFirst500Second200(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(2048)
	zipSHA := sha256Hex(zipData)

	var m1Hits, m2Hits atomic.Int32
	m1 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m1Hits.Add(1)
		http.Error(w, "upstream error", http.StatusInternalServerError)
	}))
	defer m1.Close()

	m2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m2Hits.Add(1)
		_, _ = w.Write(zipData)
	}))
	defer m2.Close()

	manifestBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		m1.URL + "/pkg.zip",
		m2.URL + "/pkg.zip",
	})
	manifestSrv := serveManifest(t, manifestBytes)

	dataDir := t.TempDir()
	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{manifestSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err != nil {
		t.Fatalf("expected fallback success, got %v", err)
	}
	if m1Hits.Load() != 1 || m2Hits.Load() != 1 {
		t.Fatalf("unexpected hit counts: m1=%d m2=%d", m1Hits.Load(), m2Hits.Load())
	}

	st, _, err := LoadState(StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}
	if st.LastGoodIndex != 1 || st.Phase != PhaseReady {
		t.Fatalf("expected last_good_index=1 and ready, got %+v", st)
	}
}

func TestResumePartial206AndSurvivesNewWorker(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(5000)
	zipSHA := sha256Hex(zipData)

	split := 1800
	partialSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Send headers and partial bytes, then drop without full content.
		w.Header().Set("Content-Length", strconv.Itoa(len(zipData)))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(zipData[:split])
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		if hj, ok := w.(http.Hijacker); ok {
			conn, _, err := hj.Hijack()
			if err == nil {
				_ = conn.Close()
			}
		}
	}))
	defer partialSrv.Close()

	var sawRange string
	resumeSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawRange = r.Header.Get("Range")
		wantRange := fmt.Sprintf("bytes=%d-", split)
		if sawRange != wantRange {
			http.Error(w, "expected range "+wantRange, http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", split, len(zipData)-1, len(zipData)))
		w.Header().Set("Content-Length", strconv.Itoa(len(zipData)-split))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(zipData[split:])
	}))
	defer resumeSrv.Close()

	dataDir := t.TempDir()

	// Worker 1: fails against partialSrv after writing `split` bytes to .part.
	m1Bytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		partialSrv.URL + "/pkg.zip",
	})
	m1Srv := serveManifest(t, m1Bytes)

	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{m1Srv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err == nil {
		t.Fatal("expected worker 1 to fail on truncated response")
	}

	partPath := filepath.Join(dataDir, "updates", "haoxiangpao_v0.3.0_win64.zip.part")
	info, err := os.Stat(partPath)
	if err != nil {
		t.Fatalf("expected .part to exist: %v", err)
	}
	if info.Size() != int64(split) {
		t.Fatalf("expected .part size %d, got %d", split, info.Size())
	}

	// Worker 2: starts fresh with same version/sha and resumes via HTTP 206.
	m2Bytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		resumeSrv.URL + "/pkg.zip",
	})
	m2Srv := serveManifest(t, m2Bytes)

	err = Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{m2Srv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err != nil {
		t.Fatalf("worker 2 resume failed: %v (Range=%q)", err, sawRange)
	}
	if sawRange != fmt.Sprintf("bytes=%d-", split) {
		t.Fatalf("expected Range bytes=%d-, got %q", split, sawRange)
	}

	st, _, err := LoadState(StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}
	if st.Phase != PhaseReady {
		t.Fatalf("expected ready after resume, got %q", st.Phase)
	}
	if err := VerifyPackageFile(st.Package, int64(len(zipData)), zipSHA); err != nil {
		t.Fatalf("final resumed package verification failed: %v", err)
	}
}

func TestServer200RestartsFromZeroOnRangeRequest(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(2048)
	zipSHA := sha256Hex(zipData)

	var gotRange string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRange = r.Header.Get("Range")
		// Ignore Range and answer 200 OK with the full payload.
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(zipData)
	}))
	defer srv.Close()

	dataDir := t.TempDir()
	updatesDir := UpdatesDir(dataDir)
	if err := os.MkdirAll(updatesDir, 0o755); err != nil {
		t.Fatal(err)
	}
	urls := []string{srv.URL + "/pkg.zip"}
	fileName := "haoxiangpao_v0.3.0_win64.zip"
	if err := os.WriteFile(filepath.Join(updatesDir, fileName+".part"), makeTestZipBytes(12), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := SaveState(StatePath(dataDir), State{
		Schema:   StateSchema,
		Version:  "0.3.0",
		File:     fileName,
		SHA256:   zipSHA,
		Size:     int64(len(zipData)),
		URLsHash: ComputeURLsHash(urls),
		Phase:    PhaseDownloading,
	}); err != nil {
		t.Fatal(err)
	}

	manifestBytes := signTestManifest(t, keyID, priv, "0.3.0", fileName, int64(len(zipData)), zipSHA, urls)
	mSrv := serveManifest(t, manifestBytes)

	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{mSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err != nil {
		t.Fatalf("expected 200 restart to succeed: %v", err)
	}
	if gotRange != "bytes=12-" {
		t.Fatalf("expected Range bytes=12-, got %q", gotRange)
	}
}

func TestChecksumMismatchKeepsPartAndTriesNextMirror(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	goodZip := makeTestZipBytes(1024)
	goodSHA := sha256Hex(goodZip)
	badZip := makeTestZipBytes(1024)
	badZip[100] ^= 0xff

	m1 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(badZip)
	}))
	defer m1.Close()

	m2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "mirror 2 down", http.StatusServiceUnavailable)
	}))
	defer m2.Close()

	// Case 1: mirror 1 checksum mismatch, mirror 2 fails -> overall failure, .part kept.
	dataDir := t.TempDir()
	fileName := "haoxiangpao_v0.3.0_win64.zip"
	mBytesFail := signTestManifest(t, keyID, priv, "0.3.0", fileName, int64(len(goodZip)), goodSHA, []string{
		m1.URL + "/pkg.zip",
		m2.URL + "/pkg.zip",
	})
	mSrvFail := serveManifest(t, mBytesFail)

	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{mSrvFail.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err == nil {
		t.Fatal("expected failure when mirror 1 has bad checksum and mirror 2 is 503")
	}
	partPath := filepath.Join(dataDir, "updates", fileName+".part")
	if _, err := os.Stat(partPath); err != nil {
		t.Fatalf("expected .part to be kept after checksum mismatch, stat err=%v", err)
	}

	// Case 2: mirror 1 checksum mismatch, mirror 2 returns valid zip -> succeeds.
	mGood := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(goodZip)
	}))
	defer mGood.Close()

	mBytesOK := signTestManifest(t, keyID, priv, "0.3.0", fileName, int64(len(goodZip)), goodSHA, []string{
		m1.URL + "/pkg.zip",
		mGood.URL + "/pkg.zip",
	})
	mSrvOK := serveManifest(t, mBytesOK)

	if err := Run(context.Background(), Config{
		DataDir:      t.TempDir(),
		ManifestURLs: []string{mSrvOK.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	}); err != nil {
		t.Fatalf("expected mirror 2 to recover after mirror 1 checksum mismatch: %v", err)
	}
}

func TestZipMagicRejected(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	notAZip := bytes.Repeat([]byte("HTML"), 256)
	sha := sha256Hex(notAZip)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(notAZip)
	}))
	defer srv.Close()

	mBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(notAZip)), sha, []string{
		srv.URL + "/pkg.zip",
	})
	mSrv := serveManifest(t, mBytes)

	dataDir := t.TempDir()
	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{mSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
	})
	if err == nil || !strings.Contains(err.Error(), "magic") {
		t.Fatalf("expected zip magic error, got %v", err)
	}
}

func TestAllMirrorsFailIncrementsAttemptsAndSetsFutureRetry(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(1024)
	zipSHA := sha256Hex(zipData)

	failSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "nope", http.StatusBadGateway)
	}))
	defer failSrv.Close()

	mBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		failSrv.URL + "/m1.zip",
		failSrv.URL + "/m2.zip",
	})
	mSrv := serveManifest(t, mBytes)

	dataDir := t.TempDir()
	fixedNow := time.Unix(1700000000, 0)
	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{mSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
		Now:          func() time.Time { return fixedNow },
		Jitter:       func(d time.Duration) time.Duration { return d },
	})
	if err == nil {
		t.Fatal("expected --once failure when all mirrors fail")
	}

	st, ok, err := LoadState(StatePath(dataDir))
	if err != nil || !ok {
		t.Fatalf("LoadState failed: ok=%v err=%v", ok, err)
	}
	if st.Attempts != 1 {
		t.Fatalf("expected attempts=1, got %d", st.Attempts)
	}
	if st.NextRetryAt != fixedNow.Add(15*time.Second).Unix() {
		t.Fatalf("expected next_retry_at=%d, got %d", fixedNow.Add(15*time.Second).Unix(), st.NextRetryAt)
	}
	if st.Phase != PhaseDownloading || st.Package != "" {
		t.Fatalf("unexpected phase/package: %+v", st)
	}
}

func TestChangedURLsHashResetsIndexAndAttempts(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(1024)
	zipSHA := sha256Hex(zipData)

	failSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "fail", http.StatusInternalServerError)
	}))
	defer failSrv.Close()

	dataDir := t.TempDir()
	fileName := "haoxiangpao_v0.3.0_win64.zip"
	if err := os.MkdirAll(UpdatesDir(dataDir), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(UpdatesDir(dataDir), fileName+".part"), zipData[:300], 0o644); err != nil {
		t.Fatal(err)
	}
	if err := SaveState(StatePath(dataDir), State{
		Schema:        StateSchema,
		Version:       "0.3.0",
		File:          fileName,
		SHA256:        zipSHA,
		Size:          int64(len(zipData)),
		URLsHash:      "old-urls-hash",
		LastGoodIndex: 2,
		NextIndex:     3,
		Attempts:      5,
		NextRetryAt:   1700000100,
		Phase:         PhaseDownloading,
	}); err != nil {
		t.Fatal(err)
	}

	mBytes := signTestManifest(t, keyID, priv, "0.3.0", fileName, int64(len(zipData)), zipSHA, []string{
		failSrv.URL + "/a.zip",
		failSrv.URL + "/b.zip",
	})
	mSrv := serveManifest(t, mBytes)

	_ = Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{mSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
		Jitter:       func(d time.Duration) time.Duration { return d },
	})

	st, _, err := LoadState(StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}
	// Attempts reset to 0 on urls_hash change, then incremented to 1 on pass failure.
	if st.Attempts != 1 {
		t.Fatalf("expected attempts reset to 0 then incremented to 1, got %d", st.Attempts)
	}
	if st.LastGoodIndex != 0 {
		t.Fatalf("expected last_good_index reset to 0, got %d", st.LastGoodIndex)
	}
	if st.NextIndex != 0 {
		t.Fatalf("expected next_index to wrap back to 0 after trying 2 mirrors from 0, got %d", st.NextIndex)
	}
	// Same version/sha/size keeps .part on disk.
	if _, err := os.Stat(filepath.Join(UpdatesDir(dataDir), fileName+".part")); err != nil {
		t.Fatalf("expected .part preserved when only urls_hash changed: %v", err)
	}
}

func TestLogsAndStateNeverContainTokensOrUserinfo(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(512)
	zipSHA := sha256Hex(zipData)

	failSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "denied", http.StatusForbidden)
	}))
	defer failSrv.Close()

	mirrorWithSecret := strings.Replace(failSrv.URL, "http://", "http://alice:SECRET_PASS@", 1) + "/pkg.zip?token=SECRET_QUERY_TOKEN#SECRET_FRAG"
	mBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		mirrorWithSecret,
	})
	mSrv := serveManifest(t, mBytes)
	manifestURLWithSecret := strings.Replace(mSrv.URL, "http://", "http://bob:SECRET_MANIFEST_PASS@", 1) + "/latest.json?token=SECRET_MANIFEST_TOKEN#MFRAG"

	dataDir := t.TempDir()
	logPath := filepath.Join(dataDir, "updates", "launcher.log")
	logger := logx.Open(logPath)

	_ = Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{manifestURLWithSecret},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
		Logger:       logger,
	})
	_ = logger.Close()

	logBytes, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	stateBytes, err := os.ReadFile(StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}

	for _, secret := range []string{
		"token=",
		"SECRET_QUERY_TOKEN",
		"SECRET_PASS",
		"SECRET_FRAG",
		"SECRET_MANIFEST_PASS",
		"SECRET_MANIFEST_TOKEN",
		"MFRAG",
		"alice:",
		"bob:",
	} {
		if strings.Contains(string(logBytes), secret) {
			t.Fatalf("log leaked %q:\n%s", secret, string(logBytes))
		}
		if strings.Contains(string(stateBytes), secret) {
			t.Fatalf("state leaked %q:\n%s", secret, string(stateBytes))
		}
	}
}

func TestManifestFailureBackoffDoesNotWriteFakeReady(t *testing.T) {
	keyID, pub, _ := testKeyPair(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer srv.Close()

	dataDir := t.TempDir()
	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{srv.URL + "/latest.json?token=SECRET_TOKEN"},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      time.Second,
		Once:         true,
	})
	if err == nil {
		t.Fatal("expected manifest failure")
	}

	st, ok, err := LoadState(StatePath(dataDir))
	if err != nil || !ok {
		t.Fatalf("LoadState failed: ok=%v err=%v", ok, err)
	}
	if st.Phase == PhaseReady || st.Package != "" {
		t.Fatalf("must not write fake ready state: %+v", st)
	}
	if st.Attempts != 1 || st.NextRetryAt <= 0 {
		t.Fatalf("expected attempts=1 and next_retry_at > 0, got %+v", st)
	}
	if strings.Contains(st.LastError, "SECRET_TOKEN") || strings.Contains(st.LastError, "token=") {
		t.Fatalf("last_error leaked token: %q", st.LastError)
	}
}

func TestParentDisappearsStopsWorkerAndKeepsStateAndPart(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(1024)
	zipSHA := sha256Hex(zipData)

	failSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "down", http.StatusServiceUnavailable)
	}))
	defer failSrv.Close()

	mBytes := signTestManifest(t, keyID, priv, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipData)), zipSHA, []string{
		failSrv.URL + "/pkg.zip",
	})
	mSrv := serveManifest(t, mBytes)

	dataDir := t.TempDir()
	partPath := filepath.Join(UpdatesDir(dataDir), "haoxiangpao_v0.3.0_win64.zip.part")
	if err := os.MkdirAll(UpdatesDir(dataDir), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(partPath, makeTestZipBytes(16), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := SaveState(StatePath(dataDir), State{
		Schema:   StateSchema,
		Version:  "0.3.0",
		File:     "haoxiangpao_v0.3.0_win64.zip",
		SHA256:   zipSHA,
		Size:     int64(len(zipData)),
		URLsHash: ComputeURLsHash([]string{failSrv.URL + "/pkg.zip"}),
		Phase:    PhaseDownloading,
	}); err != nil {
		t.Fatal(err)
	}

	var checks atomic.Int32
	err := Run(context.Background(), Config{
		DataDir:             dataDir,
		ManifestURLs:        []string{mSrv.URL},
		Keys:                map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:          true,
		Timeout:             time.Second,
		Once:                false,
		ParentPID:           99999,
		ParentCheckInterval: 10 * time.Millisecond,
		IsAlive: func(pid int) bool {
			// Alive for pass 1, gone when sleep check runs.
			return checks.Add(1) <= 2
		},
	})
	if err != nil {
		t.Fatalf("expected clean exit when parent disappears, got %v", err)
	}
	if _, err := os.Stat(partPath); err != nil {
		t.Fatalf("expected .part to persist on parent exit: %v", err)
	}
	if _, err := os.Stat(StatePath(dataDir)); err != nil {
		t.Fatalf("expected state to persist on parent exit: %v", err)
	}
}

func TestRolledBackArtifactIsNotRePromotedToReady(t *testing.T) {
	keyID, pub, priv := testKeyPair(t)
	zipData := makeTestZipBytes(512)
	zipSHA := sha256Hex(zipData)
	fileName := "haoxiangpao_v0.3.0_win64.zip"

	dataDir := t.TempDir()
	updatesDir := UpdatesDir(dataDir)
	if err := os.MkdirAll(updatesDir, 0o755); err != nil {
		t.Fatal(err)
	}
	packagePath := filepath.Join(updatesDir, fileName)
	if err := os.WriteFile(packagePath, zipData, 0o644); err != nil {
		t.Fatal(err)
	}
	state := DefaultState()
	state.Version = "0.3.0"
	state.File = fileName
	state.SHA256 = zipSHA
	state.Size = int64(len(zipData))
	state.Phase = PhaseRolledBack
	state.Package = packagePath
	if err := SaveState(StatePath(dataDir), state); err != nil {
		t.Fatal(err)
	}

	pkgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(zipData)
	}))
	defer pkgSrv.Close()
	manifestBytes := signTestManifest(t, keyID, priv, "0.3.0", fileName, int64(len(zipData)), zipSHA, []string{
		pkgSrv.URL + "/" + fileName,
	})
	manifestSrv := serveManifest(t, manifestBytes)

	err := Run(context.Background(), Config{
		DataDir:      dataDir,
		ManifestURLs: []string{manifestSrv.URL},
		Keys:         map[string]ed25519.PublicKey{keyID: pub},
		AllowLocal:   true,
		Timeout:      2 * time.Second,
		Once:         true,
		Logger:       logx.NewWriter(nil),
	})
	if err != nil {
		t.Fatalf("Run should treat a quarantined artifact as no-op: %v", err)
	}
	st, ok, loadErr := LoadState(StatePath(dataDir))
	if loadErr != nil || !ok {
		t.Fatalf("LoadState: ok=%v err=%v", ok, loadErr)
	}
	if st.Phase != PhaseRolledBack {
		t.Fatalf("rolled_back artifact was re-promoted: phase=%q", st.Phase)
	}
}

func TestMirrorOrderAndBackoffSchedule(t *testing.T) {
	if got := mirrorOrder(5, 2, 3); fmt.Sprint(got) != "[2 0 3 4 1]" {
		t.Fatalf("mirrorOrder(5,2,3)=%v, want [2 0 3 4 1]", got)
	}
	if got := mirrorOrder(4, 0, 0); fmt.Sprint(got) != "[0 1 2 3]" {
		t.Fatalf("mirrorOrder(4,0,0)=%v, want [0 1 2 3]", got)
	}

	want := []time.Duration{
		15 * time.Second,
		30 * time.Second,
		1 * time.Minute,
		2 * time.Minute,
		5 * time.Minute,
		10 * time.Minute,
		30 * time.Minute,
		1 * time.Hour,
		1 * time.Hour,
	}
	for i, w := range want {
		got := BackoffDelay(i+1, func(d time.Duration) time.Duration { return d })
		if got != w {
			t.Fatalf("BackoffDelay(%d)=%v, want %v", i+1, got, w)
		}
	}

	// Default jitter stays within +/- 20%.
	for i := 0; i < 50; i++ {
		d := BackoffDelay(1, nil)
		if d < 12*time.Second || d > 18*time.Second {
			t.Fatalf("jittered 15s out of [12s,18s]: %v", d)
		}
	}
}
