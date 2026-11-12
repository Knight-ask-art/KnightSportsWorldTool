package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
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
}
