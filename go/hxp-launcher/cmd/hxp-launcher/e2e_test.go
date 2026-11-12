package main

import (
	"archive/zip"
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"haoxiangpao/hxp-launcher/internal/manifest"
	"haoxiangpao/hxp-launcher/internal/worker"
)

const fakeAppSource = `package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

type buildInfo struct {
	Version string ` + "`json:\"version\"`" + `
}

func main() {
	exe, _ := os.Executable()
	dir := filepath.Dir(exe)
	var info buildInfo
	if raw, err := os.ReadFile(filepath.Join(dir, "build_info.json")); err == nil {
		_ = json.Unmarshal(raw, &info)
	}
	_ = os.WriteFile(filepath.Join(dir, "started.txt"), []byte(info.Version), 0o644)
	_ = os.WriteFile(filepath.Join(dir, "pid.txt"), []byte(strconv.Itoa(os.Getpid())), 0o644)
	if _, err := os.Stat(filepath.Join(dir, "fail_boot")); err == nil {
		time.Sleep(60 * time.Second)
		return
	}
	if p := os.Getenv("JHP_BOOT_OK_PATH"); p != "" {
		payload, _ := json.Marshal(map[string]any{
			"version": info.Version,
			"pid":     os.Getpid(),
			"at":      time.Now().UnixMilli(),
		})
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		_ = os.WriteFile(p, payload, 0o644)
	}
	time.Sleep(60 * time.Second)
}
`

func buildFakeApp(t *testing.T) string {
	t.Helper()
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "go.mod"), []byte("module fakeapp\n\ngo 1.22\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(srcDir, "main.go"), []byte(fakeAppSource), 0o644); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "fake.exe")
	cmd := exec.Command("go", "build", "-trimpath", "-o", out, ".")
	cmd.Dir = srcDir
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if raw, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build fake app: %v\n%s", err, raw)
	}
	return out
}

func buildTestLauncher(t *testing.T, pubkeySpec string) string {
	t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "hxp-launcher.exe")
	args := []string{"build", "-trimpath", "-o", out}
	ldflags := "-s -w"
	if pubkeySpec != "" {
		ldflags += " -X haoxiangpao/hxp-launcher/internal/manifest.injectedPublicKeys=" + pubkeySpec
	}
	args = append(args, "-ldflags", ldflags, "./cmd/hxp-launcher")
	cmd := exec.Command("go", args...)
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if raw, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build launcher: %v\n%s", err, raw)
	}
	return out
}

func testManifestBytes(t *testing.T, version, fileName string, size int64, sha string, url string, keyID string, priv ed25519.PrivateKey) []byte {
	t.Helper()
	payload := map[string]any{
		"schema":      1,
		"app":         "haoxiangpao",
		"name":        "好想跑",
		"channel":     "stable",
		"version":     version,
		"min_version": "0.1.4",
		"released_at": "2026-11-12T12:00:00+08:00",
		"notes":       []any{"e2e"},
		"assets": map[string]any{
			"windows-x64": map[string]any{
				"file":    fileName,
				"url":     url,
				"size":    size,
				"sha256":  sha,
				"format":  "zip",
				"layout":  "onedir",
				"entry":   "好想跑.exe",
				"version": version,
			},
		},
	}
	canonical, err := manifest.CanonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	msg := append([]byte(keyID+"\x00"), canonical...)
	sig := ed25519.Sign(priv, msg)
	payload["signature"] = map[string]any{
		"alg":    "ed25519",
		"key_id": keyID,
		"value":  base64.StdEncoding.EncodeToString(sig),
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func makeAppZip(t *testing.T, fakeExe, version string, failBoot bool) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	write := func(name string, data []byte) {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write(data); err != nil {
			t.Fatal(err)
		}
	}
	exe, err := os.ReadFile(fakeExe)
	if err != nil {
		t.Fatal(err)
	}
	write("好想跑.exe", exe)
	write("build_info.json", []byte(`{"version":"`+version+`","commit":"e2e","built_at":"2026-11-12T00:00:00Z"}`+"\n"))
	if failBoot {
		write("fail_boot", nil)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func writeE2EBuildInfo(t *testing.T, dir, version string) {
	t.Helper()
	body := `{"version":"` + version + `","commit":"e2e","built_at":"2026-11-12T00:00:00Z"}` + "\n"
	if err := os.WriteFile(filepath.Join(dir, "build_info.json"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func waitFor(t *testing.T, timeout time.Duration, what string, fn func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func readText(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

func killApp(t *testing.T, appDir string) {
	t.Helper()
	raw := readText(filepath.Join(appDir, "pid.txt"))
	if raw == "" {
		return
	}
	pid, err := strconv.Atoi(raw)
	if err != nil {
		return
	}
	if p, err := os.FindProcess(pid); err == nil {
		_ = p.Kill()
	}
	time.Sleep(300 * time.Millisecond)
}

func runLauncher(t *testing.T, exe string, args ...string) string {
	t.Helper()
	cmd := exec.Command(exe, args...)
	raw, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("launcher failed: %v\n%s", err, raw)
	}
	return string(raw)
}

func TestEndToEndDownloadApplyRollbackAndQuarantine(t *testing.T) {
	if testing.Short() {
		t.Skip("end-to-end update test skipped in -short mode")
	}
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go toolchain not available")
	}
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	keyID := "test"
	pubkeySpec := keyID + ":" + base64.StdEncoding.EncodeToString(pub)
	launcher := buildTestLauncher(t, pubkeySpec)
	fakeExe := buildFakeApp(t)

	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	exe, err := os.ReadFile(fakeExe)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, "好想跑.exe"), exe, 0o644); err != nil {
		t.Fatal(err)
	}
	writeE2EBuildInfo(t, appDir, "0.1.0")
	if err := os.WriteFile(filepath.Join(dataDir, "user.txt"), []byte("keep-me"), 0o644); err != nil {
		t.Fatal(err)
	}
	pubkeyPath := filepath.Join(root, "pubkey.txt")
	if err := os.WriteFile(pubkeyPath, []byte(pubkeySpec+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(dataDir, "updates", "launcher.log")

	zipV2 := makeAppZip(t, fakeExe, "0.2.0", false)
	sumV2 := sha256.Sum256(zipV2)
	shaV2 := hex.EncodeToString(sumV2[:])
	var manifestV2 []byte
	pkgSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest.json":
			_, _ = w.Write(manifestV2)
		case "/pkg-v2.zip":
			_, _ = w.Write(zipV2)
		default:
			http.NotFound(w, r)
		}
	}))
	defer pkgSrv.Close()
	manifestV2 = testManifestBytes(t, "0.2.0", "haoxiangpao_v0.2.0_win64.zip", int64(len(zipV2)), shaV2, pkgSrv.URL+"/pkg-v2.zip", keyID, priv)

	baseArgs := []string{
		"--app", filepath.Join(appDir, "好想跑.exe"),
		"--data", dataDir,
		"--timeout", "2",
		"--allow-local",
		"--pubkey", pubkeyPath,
		"--log", logPath,
		"--manifest", pkgSrv.URL + "/latest.json",
	}

	// 1. First run: launch 0.1.0, then the detached worker downloads 0.2.0.
	runLauncher(t, launcher, baseArgs...)
	waitFor(t, 15*time.Second, "ready state", func() bool {
		st, ok, err := worker.LoadState(worker.StatePath(dataDir))
		return err == nil && ok && st.Phase == worker.PhaseReady && st.Version == "0.2.0"
	})
	if got := readText(filepath.Join(dataDir, "user.txt")); got != "keep-me" {
		t.Fatalf("user data changed during download: %q", got)
	}
	killApp(t, appDir)
	time.Sleep(2500 * time.Millisecond) // let the detached worker observe the dead parent

	// 2. Second run: apply 0.2.0, wait for boot-ok, keep user data.
	runLauncher(t, launcher, baseArgs...)
	waitFor(t, 20*time.Second, "0.2.0 app start", func() bool {
		return readText(filepath.Join(appDir, "started.txt")) == "0.2.0"
	})
	waitFor(t, 5*time.Second, "applied state", func() bool {
		st, ok, err := worker.LoadState(worker.StatePath(dataDir))
		return err == nil && ok && st.Phase == worker.PhaseApplied
	})
	if got := readText(filepath.Join(dataDir, "user.txt")); got != "keep-me" {
		t.Fatalf("user data lost during apply: %q", got)
	}
	if _, err := os.Stat(appDir + ".old"); !os.IsNotExist(err) {
		t.Fatalf(".old should be removed after successful apply")
	}
	killApp(t, appDir)
	time.Sleep(2500 * time.Millisecond)

	// 3. Prepare a broken 0.3.0 package and force a boot-ok failure.
	zipV3 := makeAppZip(t, fakeExe, "0.3.0", true)
	sumV3 := sha256.Sum256(zipV3)
	shaV3 := hex.EncodeToString(sumV3[:])
	pkgV3 := filepath.Join(dataDir, "updates", "haoxiangpao_v0.3.0_win64.zip")
	if err := os.MkdirAll(filepath.Dir(pkgV3), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pkgV3, zipV3, 0o644); err != nil {
		t.Fatal(err)
	}
	st := worker.DefaultState()
	st.Version = "0.3.0"
	st.File = filepath.Base(pkgV3)
	st.SHA256 = shaV3
	st.Size = int64(len(zipV3))
	st.Phase = worker.PhaseReady
	st.Package = pkgV3
	if err := worker.SaveState(worker.StatePath(dataDir), st); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(appDir, "started.txt")); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	runLauncher(t, launcher, append(baseArgs, "--apply-boot-timeout", "2")...)
	waitFor(t, 20*time.Second, "rollback to 0.2.0", func() bool {
		return readText(filepath.Join(appDir, "started.txt")) == "0.2.0"
	})
	waitFor(t, 5*time.Second, "rolled_back state", func() bool {
		got, ok, err := worker.LoadState(worker.StatePath(dataDir))
		return err == nil && ok && got.Phase == worker.PhaseRolledBack
	})
	if got := readText(filepath.Join(dataDir, "user.txt")); got != "keep-me" {
		t.Fatalf("user data lost during rollback: %q", got)
	}
	if _, err := os.Stat(appDir + ".old"); !os.IsNotExist(err) {
		t.Fatalf(".old should be gone after rollback")
	}
	if _, err := os.Stat(appDir + ".new"); !os.IsNotExist(err) {
		t.Fatalf(".new should be gone after rollback")
	}
	killApp(t, appDir)
	time.Sleep(2500 * time.Millisecond)

	// 4. A manifest that still offers 0.3.0 must not re-promote the failed
	//    artifact and loop apply/rollback.
	var manifestV3 []byte
	manifestV3 = testManifestBytes(t, "0.3.0", "haoxiangpao_v0.3.0_win64.zip", int64(len(zipV3)), shaV3, pkgSrv.URL+"/pkg-v3.zip", keyID, priv)
	pkgSrv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest.json":
			_, _ = w.Write(manifestV3)
		case "/pkg-v3.zip":
			_, _ = w.Write(zipV3)
		default:
			http.NotFound(w, r)
		}
	})
	runLauncher(t, launcher, baseArgs...)
	time.Sleep(3 * time.Second)
	after, ok, err := worker.LoadState(worker.StatePath(dataDir))
	if err != nil || !ok {
		t.Fatalf("LoadState after quarantine run: ok=%v err=%v", ok, err)
	}
	if after.Phase != worker.PhaseRolledBack {
		t.Fatalf("failed artifact was re-promoted: phase=%q", after.Phase)
	}
	killApp(t, appDir)
}
