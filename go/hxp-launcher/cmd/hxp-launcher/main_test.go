package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"haoxiangpao/hxp-launcher/internal/app"
	"haoxiangpao/hxp-launcher/internal/manifest"
	"haoxiangpao/hxp-launcher/internal/worker"
)

func testdataPath(parts ...string) string {
	all := append([]string{"..", "..", "testdata"}, parts...)
	return filepath.Join(all...)
}

func writeBuildInfo(t *testing.T, dir, ver string) {
	t.Helper()
	b, err := json.Marshal(map[string]string{
		"version":  ver,
		"commit":   "9d7650c",
		"built_at": "2026-11-12T12:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "build_info.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestCheckAgainstLocalHTTPTest(t *testing.T) {
	manifestBytes, err := os.ReadFile(testdataPath("manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(manifestBytes)
	}))
	defer srv.Close()

	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "launcher.log")

	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--app", appPath,
		"--log", logPath,
		"--check",
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir)

	if code != 0 {
		t.Fatalf("expected exit 0, got %d; stdout=%s stderr=%s", code, stdout.String(), stderr.String())
	}

	var summary CheckSummary
	if err := json.Unmarshal(stdout.Bytes(), &summary); err != nil {
		t.Fatalf("invalid JSON summary: %v\noutput: %s", err, stdout.String())
	}
	if !summary.Verified || summary.State != "available" || summary.Latest != "9.9.9" || summary.Current != "0.2.2" || summary.KeyID != "test" {
		t.Fatalf("unexpected summary: %+v", summary)
	}
}

func TestDryRunDoesNotStartProcess(t *testing.T) {
	dir := t.TempDir()
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "launcher.log")

	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--app", appPath,
		"--log", logPath,
		"--no-update",
		"--dry-run",
	}, &stdout, &stderr, dir)

	if code != 0 {
		t.Fatalf("expected exit 0 for --no-update --dry-run, got %d; stderr=%s", code, stderr.String())
	}
	logBytes, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(logBytes), "dry-run: would launch") {
		t.Fatalf("expected dry-run log entry, got:\n%s", string(logBytes))
	}
}

func TestMissingManifestAndTimeoutFailOpen(t *testing.T) {
	// 404 server
	srv404 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer srv404.Close()

	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "404.log")

	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--app", appPath,
		"--log", logPath,
		"--dry-run",
		"--allow-local",
		"--manifest", srv404.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir)
	if code != 0 {
		t.Fatalf("expected fail-open exit 0 on 404, got %d", code)
	}
	log404, _ := os.ReadFile(logPath)
	if !strings.Contains(string(log404), "fail-open") || !strings.Contains(string(log404), "dry-run: would launch") {
		t.Fatalf("unexpected log on 404:\n%s", string(log404))
	}

	// Slow server -> timeout
	srvSlow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-time.After(3 * time.Second):
		case <-r.Context().Done():
		}
	}))
	defer srvSlow.Close()

	logTimeoutPath := filepath.Join(dir, "timeout.log")
	stdout.Reset()
	stderr.Reset()
	code = run([]string{
		"--app", appPath,
		"--log", logTimeoutPath,
		"--dry-run",
		"--allow-local",
		"--timeout", "1",
		"--manifest", srvSlow.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir)
	if code != 0 {
		t.Fatalf("expected fail-open exit 0 on timeout, got %d", code)
	}
	logTimeout, _ := os.ReadFile(logTimeoutPath)
	if !strings.Contains(string(logTimeout), "fail-open") || !strings.Contains(string(logTimeout), "dry-run: would launch") {
		t.Fatalf("unexpected log on timeout:\n%s", string(logTimeout))
	}
}

func TestBadSignatureFailOpenAndNoKeyMaterialInLog(t *testing.T) {
	manifestBytes, err := os.ReadFile(testdataPath("manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	pubkeyB64Bytes, err := os.ReadFile(testdataPath("pubkey.b64"))
	if err != nil {
		t.Fatal(err)
	}
	pubkeyB64 := strings.TrimSpace(string(pubkeyB64Bytes))

	tampered := bytes.Replace(manifestBytes, []byte(`"version": "9.9.9"`), []byte(`"version": "9.9.8"`), 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(tampered)
	}))
	defer srv.Close()

	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "badsig.log")

	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--app", appPath,
		"--log", logPath,
		"--dry-run",
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir)
	if code != 0 {
		t.Fatalf("expected fail-open exit 0 on bad signature, got %d", code)
	}

	logBytes, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	logText := string(logBytes)
	if !strings.Contains(logText, "invalid manifest signature") || !strings.Contains(logText, "dry-run: would launch") {
		t.Fatalf("unexpected bad-signature log:\n%s", logText)
	}
	if strings.Contains(logText, pubkeyB64) || strings.Contains(logText, "wqhfnmSTIrauVUP7") {
		t.Fatalf("log must not contain key or signature material:\n%s", logText)
	}
}

func TestProductionNoKeyFailOpen(t *testing.T) {
	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "nokey.log")

	var stdout, stderr bytes.Buffer
	code := run([]string{
		"--app", appPath,
		"--log", logPath,
		"--dry-run",
	}, &stdout, &stderr, dir)
	if code != 0 {
		t.Fatalf("expected exit 0 when no key configured, got %d", code)
	}
	logBytes, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(logBytes), "signature not configured") {
		t.Fatalf("expected 'signature not configured' in log, got:\n%s", string(logBytes))
	}
}

func TestBadUsageExit2(t *testing.T) {
	dir := t.TempDir()
	var stdout, stderr bytes.Buffer

	if code := run([]string{"--pubkey", "foo.b64"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --pubkey without --allow-local, got %d", code)
	}
	if code := run([]string{"--timeout", "0"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --timeout 0, got %d", code)
	}
	if code := run([]string{"--unknown-flag"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for unknown flag, got %d", code)
	}
	if code := run([]string{"--once"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --once without --update-worker, got %d", code)
	}
	if code := run([]string{"--parent-pid", "123"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --parent-pid without --update-worker, got %d", code)
	}
	if code := run([]string{"--update-worker", "--check"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --update-worker --check, got %d", code)
	}
	if code := run([]string{"--update-worker", "--dry-run"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for --update-worker --dry-run, got %d", code)
	}
	if code := run([]string{"--update-worker", "--parent-pid", "-1"}, &stdout, &stderr, dir); code != 2 {
		t.Fatalf("expected exit 2 for negative --parent-pid, got %d", code)
	}
}

func TestCheckAndDryRunNeverSpawnWorker(t *testing.T) {
	manifestBytes, err := os.ReadFile(testdataPath("manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(manifestBytes)
	}))
	defer srv.Close()

	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "launcher.log")

	spawned := false
	deps := launcherDeps{
		launch: app.Launch,
		spawnWorker: func(opts WorkerSpawnOptions) (int, error) {
			spawned = true
			return 999, nil
		},
	}

	var stdout, stderr bytes.Buffer
	code := runWithDeps([]string{
		"--app", appPath,
		"--log", logPath,
		"--check",
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir, deps)
	if code != 0 || spawned {
		t.Fatalf("--check must exit 0 and never spawn worker: code=%d spawned=%v", code, spawned)
	}

	stdout.Reset()
	stderr.Reset()
	code = runWithDeps([]string{
		"--app", appPath,
		"--log", logPath,
		"--dry-run",
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir, deps)
	if code != 0 || spawned {
		t.Fatalf("--dry-run must exit 0 and never spawn worker: code=%d spawned=%v", code, spawned)
	}
}

func TestNormalLauncherOpensAppFirstThenSpawnsWorkerWhenAvailable(t *testing.T) {
	manifestBytes, err := os.ReadFile(testdataPath("manifest.json"))
	if err != nil {
		t.Fatal(err)
	}

	var events []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		events = append(events, "manifest")
		_, _ = w.Write(manifestBytes)
	}))
	defer srv.Close()

	dir := t.TempDir()
	writeBuildInfo(t, dir, "0.2.2")
	appPath := filepath.Join(dir, "好想跑.exe")
	logPath := filepath.Join(dir, "launcher.log")

	var gotSpawn WorkerSpawnOptions
	deps := launcherDeps{
		launch: func(opts app.LaunchOptions) (int, error) {
			events = append(events, "launch")
			return 4321, nil
		},
		spawnWorker: func(opts WorkerSpawnOptions) (int, error) {
			events = append(events, "spawn")
			gotSpawn = opts
			return 8765, nil
		},
	}

	var stdout, stderr bytes.Buffer
	code := runWithDeps([]string{
		"--app", appPath,
		"--log", logPath,
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir, deps)
	if code != 0 {
		t.Fatalf("expected exit 0, got %d; stderr=%s", code, stderr.String())
	}
	if !slices.Equal(events, []string{"launch", "manifest", "spawn"}) {
		t.Fatalf("expected order [launch manifest spawn], got %v", events)
	}
	if gotSpawn.ParentPID != 4321 {
		t.Fatalf("expected ParentPID=4321, got %d", gotSpawn.ParentPID)
	}

	// Up-to-date version must not spawn worker.
	events = nil
	writeBuildInfo(t, dir, "9.9.9")
	code = runWithDeps([]string{
		"--app", appPath,
		"--log", logPath,
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir, deps)
	if code != 0 {
		t.Fatalf("expected exit 0 when current, got %d", code)
	}
	if !slices.Equal(events, []string{"launch", "manifest"}) {
		t.Fatalf("expected no spawn when current, got %v", events)
	}

	// Spawn failure is fail-open (app already launched).
	writeBuildInfo(t, dir, "0.2.2")
	deps.spawnWorker = func(opts WorkerSpawnOptions) (int, error) {
		return 0, errors.New("spawn denied")
	}
	code = runWithDeps([]string{
		"--app", appPath,
		"--log", logPath,
		"--allow-local",
		"--manifest", srv.URL,
		"--pubkey", testdataPath("pubkey.b64"),
	}, &stdout, &stderr, dir, deps)
	if code != 0 {
		t.Fatalf("expected fail-open exit 0 when worker spawn fails, got %d", code)
	}
}

func TestBuildWorkerCommandIncludesUpdateWorker(t *testing.T) {
	cmd, err := buildWorkerCommand(WorkerSpawnOptions{
		ExePath:      `C:\app\hxp-launcher.exe`,
		AppPath:      `C:\app\好想跑.exe`,
		DataDir:      `C:\app\data`,
		ManifestURLs: []string{"https://a.example/latest.json", "https://b.example/latest.json"},
		TimeoutSec:   8,
		LogPath:      `C:\app\data\updates\launcher.log`,
		ParentPID:    555,
		AllowLocal:   true,
		PubkeyPath:   `C:\app\pubkey.b64`,
	})
	if err != nil {
		t.Fatalf("buildWorkerCommand failed: %v", err)
	}
	if !slices.Contains(cmd.Args, "--update-worker") {
		t.Fatalf("worker command missing --update-worker: %v", cmd.Args)
	}
	if !slices.Contains(cmd.Args, "--parent-pid") || !slices.Contains(cmd.Args, "555") {
		t.Fatalf("worker command missing --parent-pid 555: %v", cmd.Args)
	}
	if cmd.SysProcAttr == nil {
		t.Fatal("expected detached SysProcAttr to be configured")
	}
}

func TestWorkerOnceCLIExitCodes(t *testing.T) {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i + 7)
	}
	priv := ed25519.NewKeyFromSeed(seed)
	pub := priv.Public().(ed25519.PublicKey)

	dir := t.TempDir()
	pubkeyPath := filepath.Join(dir, "pub.b64")
	if err := os.WriteFile(pubkeyPath, []byte(base64.StdEncoding.EncodeToString(pub)), 0o644); err != nil {
		t.Fatal(err)
	}

	zipBytes := append([]byte{'P', 'K', 0x03, 0x04}, bytes.Repeat([]byte("A"), 508)...)
	sum := sha256.Sum256(zipBytes)
	zipSHA := hex.EncodeToString(sum[:])

	var serveOK bool
	pkgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !serveOK {
			http.Error(w, "unavailable", http.StatusInternalServerError)
			return
		}
		_, _ = w.Write(zipBytes)
	}))
	defer pkgSrv.Close()

	payload := map[string]any{
		"schema":      1,
		"app":         "haoxiangpao",
		"version":     "0.3.0",
		"min_version": "0.1.4",
		"assets": map[string]any{
			"windows-x64": map[string]any{
				"file":   "haoxiangpao_v0.3.0_win64.zip",
				"url":    pkgSrv.URL + "/pkg.zip",
				"size":   len(zipBytes),
				"sha256": zipSHA,
				"format": "zip",
			},
		},
	}
	canonical, err := manifest.CanonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	msg := append(append([]byte("test"), 0), canonical...)
	sig := ed25519.Sign(priv, msg)
	payload["signature"] = map[string]any{
		"alg":    "ed25519",
		"key_id": "test",
		"value":  base64.StdEncoding.EncodeToString(sig),
	}
	manifestBytes, _ := json.Marshal(payload)
	mSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(manifestBytes)
	}))
	defer mSrv.Close()

	appPath := filepath.Join(dir, "好想跑.exe")
	dataDir := filepath.Join(dir, "data")
	logPath := filepath.Join(dataDir, "updates", "launcher.log")

	var stdout, stderr bytes.Buffer
	// Failure -> exit 1
	code := run([]string{
		"--update-worker", "--once",
		"--app", appPath,
		"--data", dataDir,
		"--manifest", mSrv.URL,
		"--timeout", "2",
		"--log", logPath,
		"--allow-local",
		"--pubkey", pubkeyPath,
		"--parent-pid", "0",
	}, &stdout, &stderr, dir)
	if code != 1 {
		t.Fatalf("expected exit 1 on --once failure, got %d", code)
	}

	// Success -> exit 0
	serveOK = true
	code = run([]string{
		"--update-worker", "--once",
		"--app", appPath,
		"--data", dataDir,
		"--manifest", mSrv.URL,
		"--timeout", "2",
		"--log", logPath,
		"--allow-local",
		"--pubkey", pubkeyPath,
		"--parent-pid", "0",
	}, &stdout, &stderr, dir)
	if code != 0 {
		t.Fatalf("expected exit 0 on --once success, got %d; stderr=%s", code, stderr.String())
	}

	st, ok, err := worker.LoadState(worker.StatePath(dataDir))
	if err != nil || !ok || st.Phase != worker.PhaseReady {
		t.Fatalf("expected ready state after CLI --once success, got ok=%v err=%v st=%+v", ok, err, st)
	}
}
