package apply

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/worker"
)

func setVerifiedState(t *testing.T, state *worker.State, zipPath string) {
	t.Helper()
	raw, err := os.ReadFile(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	state.SHA256 = hex.EncodeToString(sum[:])
	state.Size = int64(len(raw))
	state.File = filepath.Base(zipPath)
}

func createTestZip(t *testing.T, entries map[string]string) string {
	t.Helper()
	zipPath := filepath.Join(t.TempDir(), "pkg.zip")
	f, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	for name, body := range entries {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	return zipPath
}

func TestExtractZipValid(t *testing.T) {
	zipPath := createTestZip(t, map[string]string{
		AppExeName:            "new-exe-bytes",
		"frontend/index.html": "<html>ok</html>",
		"sub/data/keep.txt":   "nested-data-is-ok",
	})
	staging := filepath.Join(t.TempDir(), "staging")
	if err := ExtractZip(zipPath, staging); err != nil {
		t.Fatalf("ExtractZip failed: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(staging, AppExeName))
	if err != nil || string(got) != "new-exe-bytes" {
		t.Fatalf("unexpected extracted exe: err=%v got=%q", err, string(got))
	}
	nested, err := os.ReadFile(filepath.Join(staging, "sub", "data", "keep.txt"))
	if err != nil || string(nested) != "nested-data-is-ok" {
		t.Fatalf("unexpected nested file: err=%v got=%q", err, string(nested))
	}
}

func TestExtractZipRejectsDangerousAndInvalidPackages(t *testing.T) {
	cases := []struct {
		name    string
		entries map[string]string
		wantErr string
	}{
		{
			name: "zip-slip parent",
			entries: map[string]string{
				AppExeName:      "exe",
				"../escape.txt": "bad",
			},
			wantErr: "parent traversal",
		},
		{
			name: "zip-slip nested",
			entries: map[string]string{
				AppExeName:           "exe",
				"a/../../escape.txt": "bad",
			},
			wantErr: "parent traversal",
		},
		{
			name: "zip-slip backslash",
			entries: map[string]string{
				AppExeName:      "exe",
				`..\escape.txt`: "bad",
			},
			wantErr: "parent traversal",
		},
		{
			name: "absolute unix path",
			entries: map[string]string{
				AppExeName:    "exe",
				"/etc/passwd": "bad",
			},
			wantErr: "absolute path",
		},
		{
			name: "absolute drive path",
			entries: map[string]string{
				AppExeName:        "exe",
				`C:\temp\bad.txt`: "bad",
			},
			wantErr: "absolute path",
		},
		{
			name: "top-level data dir",
			entries: map[string]string{
				AppExeName:         "exe",
				"data/config.json": "bad",
			},
			wantErr: "top-level data/",
		},
		{
			name: "top-level Data dir case-insensitive",
			entries: map[string]string{
				AppExeName:         "exe",
				"Data/config.json": "bad",
			},
			wantErr: "top-level data/",
		},
		{
			name: "missing exe",
			entries: map[string]string{
				"readme.txt": "no exe here",
			},
			wantErr: AppExeName,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			zipPath := createTestZip(t, tc.entries)
			staging := filepath.Join(t.TempDir(), "staging")
			err := ExtractZip(zipPath, staging)
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
			}
		})
	}
}

func TestExtractZipRejectsTooManyEntries(t *testing.T) {
	zipPath := filepath.Join(t.TempDir(), "many.zip")
	f, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	if _, err := zw.Create(AppExeName); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < MaxZipEntries; i++ {
		if _, err := zw.Create(fmt.Sprintf("f/%05d.txt", i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	_ = f.Close()

	err = ExtractZip(zipPath, filepath.Join(t.TempDir(), "staging"))
	if err == nil || !strings.Contains(err.Error(), "entry count") {
		t.Fatalf("expected entry count rejection, got %v", err)
	}
}

func TestExtractZipRejectsOversizedDeclaredUncompressed(t *testing.T) {
	// Build a minimal valid zip with one file whose central directory and local
	// header declare UncompressedSize > 2 GiB (without actually writing 2 GiB).
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, err := zw.Create(AppExeName)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = w.Write([]byte("hi"))
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	raw := buf.Bytes()

	// Patch central directory header uncompressed size (offset +24 in CEN "PK\x01\x02")
	cenIdx := bytes.Index(raw, []byte{'P', 'K', 0x01, 0x02})
	if cenIdx < 0 {
		t.Fatal("central directory header not found")
	}
	binary.LittleEndian.PutUint32(raw[cenIdx+24:cenIdx+28], uint32(MaxUncompressedBytes+1024))

	zipPath := filepath.Join(t.TempDir(), "huge.zip")
	if err := os.WriteFile(zipPath, raw, 0o644); err != nil {
		t.Fatal(err)
	}

	err = ExtractZip(zipPath, filepath.Join(t.TempDir(), "staging"))
	if err == nil || !strings.Contains(err.Error(), "uncompressed size exceeds") {
		t.Fatalf("expected oversized uncompressed error, got %v", err)
	}
}

func TestApplySuccessWithFakeDeps(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	if err := os.MkdirAll(filepath.Join(appDir, "data"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, AppExeName), []byte("old-exe"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, "old_only.txt"), []byte("stale"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, "Uninstall.exe"), []byte("uninstaller"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "user.txt"), []byte("user-secret"), 0o644); err != nil {
		t.Fatal(err)
	}

	pkgPath := createTestZip(t, map[string]string{
		AppExeName:        "new-exe",
		"frontend/app.js": "console.log(2);",
	})
	statePath := worker.StatePath(dataDir)
	state := worker.State{
		Schema:  worker.StateSchema,
		Version: "0.3.0",
		Phase:   worker.PhaseReady,
		Package: pkgPath,
	}
	setVerifiedState(t, &state, pkgPath)
	if err := worker.SaveState(statePath, state); err != nil {
		t.Fatal(err)
	}

	var (
		gotStart       StartAppOptions
		gotWaitPath    string
		gotWaitVersion string
		sawApplying    bool
		removedPaths   []string
	)
	deps := Deps{
		CopyDir: DefaultDeps().CopyDir,
		Rename:  os.Rename,
		StartApp: func(opts StartAppOptions) (int, error) {
			gotStart = opts
			st, ok, err := worker.LoadState(statePath)
			if err == nil && ok && st.Phase == worker.PhaseApplying {
				sawApplying = true
			}
			return 4242, nil
		},
		WaitBoot: func(bootOKPath, expectedVersion string, timeout time.Duration) error {
			gotWaitPath = bootOKPath
			gotWaitVersion = expectedVersion
			return nil
		},
		KillApp: func(pid int) error {
			t.Fatalf("KillApp must not be called on success (pid=%d)", pid)
			return nil
		},
		RemoveAll: func(path string) error {
			removedPaths = append(removedPaths, path)
			return os.RemoveAll(path)
		},
		Now: func() time.Time { return time.Unix(1700000100, 0) },
	}

	err := Apply(Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: pkgPath,
		Version:     "0.3.0",
		BootTimeout: 5 * time.Second,
		Logger:      logx.NewWriter(nil),
	}, deps)
	if err != nil {
		t.Fatalf("Apply failed: %v", err)
	}

	if !sawApplying {
		t.Fatal("expected state to transition to applying before swap/start")
	}
	wantBootOK := filepath.Join(dataDir, "updates", "boot-ok.json")
	if gotStart.BootOKPath != wantBootOK || gotWaitPath != wantBootOK {
		t.Fatalf("unexpected boot-ok path: start=%q wait=%q want=%q", gotStart.BootOKPath, gotWaitPath, wantBootOK)
	}
	if gotWaitVersion != "0.3.0" {
		t.Fatalf("expected wait version 0.3.0, got %q", gotWaitVersion)
	}

	// New files active, old removed, user data preserved.
	gotExe, err := os.ReadFile(filepath.Join(appDir, AppExeName))
	if err != nil || string(gotExe) != "new-exe" {
		t.Fatalf("expected new exe in appDir, err=%v got=%q", err, string(gotExe))
	}
	if _, err := os.Stat(filepath.Join(appDir, "old_only.txt")); !os.IsNotExist(err) {
		t.Fatalf("expected old_only.txt to be gone, err=%v", err)
	}
	gotUser, err := os.ReadFile(filepath.Join(appDir, "data", "user.txt"))
	if err != nil || string(gotUser) != "user-secret" {
		t.Fatalf("expected preserved data/user.txt, err=%v got=%q", err, string(gotUser))
	}
	gotUninstall, err := os.ReadFile(filepath.Join(appDir, "Uninstall.exe"))
	if err != nil || string(gotUninstall) != "uninstaller" {
		t.Fatalf("expected preserved Uninstall.exe, err=%v got=%q", err, string(gotUninstall))
	}
	if _, err := os.Stat(appDir + ".old"); !os.IsNotExist(err) {
		t.Fatalf("expected .old dir to be deleted, err=%v", err)
	}

	var deletedOld bool
	for _, p := range removedPaths {
		if p == appDir+".old" {
			deletedOld = true
		}
	}
	if !deletedOld {
		t.Fatalf("expected RemoveAll to be called on %s; got %v", appDir+".old", removedPaths)
	}

	st, ok, err := worker.LoadState(statePath)
	if err != nil || !ok {
		t.Fatalf("LoadState failed: ok=%v err=%v", ok, err)
	}
	if st.Phase != worker.PhaseApplied || st.Package != "" || st.LastError != "" {
		t.Fatalf("unexpected final state: %+v", st)
	}
}

func TestApplyRejectsTamperedPackage(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	writeFile(t, filepath.Join(appDir, "好想跑.exe"), "old")
	writeFile(t, filepath.Join(dataDir, "session.json"), "keep")
	zipPath := createTestZip(t, map[string]string{AppExeName: "new"})

	state := worker.DefaultState()
	state.Version = "9.9.9"
	state.Phase = worker.PhaseReady
	state.Package = zipPath
	state.SHA256 = strings.Repeat("a", 64)
	state.Size = 1
	if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
		t.Fatal(err)
	}

	err := Apply(Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: zipPath,
		Version:     "9.9.9",
		BootTimeout: time.Second,
	}, DefaultDeps())
	if err == nil || !strings.Contains(err.Error(), "package verification") {
		t.Fatalf("expected package verification error, got %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(appDir, "好想跑.exe")); statErr != nil {
		t.Fatalf("old app was disturbed: %v", statErr)
	}
	reloaded, _, loadErr := worker.LoadState(worker.StatePath(dataDir))
	if loadErr != nil {
		t.Fatal(loadErr)
	}
	if reloaded.Phase != worker.PhaseDownloading || reloaded.Package != "" {
		t.Fatalf("state not reset for redownload: %+v", reloaded)
	}
}

func TestApplyMissingVerificationStartsOldApp(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	writeFile(t, filepath.Join(appDir, "好想跑.exe"), "old")
	writeFile(t, filepath.Join(dataDir, "user.txt"), "keep")
	zipPath := createTestZip(t, map[string]string{AppExeName: "new"})

	state := worker.DefaultState()
	state.Version = "0.3.0"
	state.Phase = worker.PhaseReady
	state.Package = zipPath
	// sha256 and size deliberately empty: Apply must refuse the package.
	if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
		t.Fatal(err)
	}

	var starts []StartAppOptions
	deps := Deps{
		CopyDir: DefaultDeps().CopyDir,
		Rename:  os.Rename,
		StartApp: func(opts StartAppOptions) (int, error) {
			starts = append(starts, opts)
			return 2001, nil
		},
		WaitBoot:  func(string, string, time.Duration) error { return nil },
		KillApp:   func(int) error { return nil },
		RemoveAll: os.RemoveAll,
		Now:       func() time.Time { return time.Unix(1700000300, 0) },
	}

	err := Apply(Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: zipPath,
		Version:     "0.3.0",
		BootTimeout: time.Second,
	}, deps)
	if err == nil || !strings.Contains(err.Error(), "sha256") {
		t.Fatalf("expected sha256 refusal, got %v", err)
	}
	if len(starts) != 1 || starts[0].ExePath != filepath.Join(appDir, AppExeName) {
		t.Fatalf("old app was not started: %+v", starts)
	}
	got, err := os.ReadFile(filepath.Join(appDir, AppExeName))
	if err != nil || string(got) != "old" {
		t.Fatalf("old app disturbed: err=%v got=%q", err, string(got))
	}
	st, _, err := worker.LoadState(worker.StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}
	if st.Phase != worker.PhaseDownloading || st.Package != "" {
		t.Fatalf("state not reset for redownload: %+v", st)
	}
}

func TestApplyRollbackKeepsAppWhenDataRestoreFails(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	newDataDir := appDir + ".new" + string(filepath.Separator) + "data"
	writeFile(t, filepath.Join(appDir, AppExeName), "old-exe")
	writeFile(t, filepath.Join(dataDir, "user.txt"), "keep-me")
	zipPath := createTestZip(t, map[string]string{AppExeName: "new-exe"})

	state := worker.State{
		Schema:  worker.StateSchema,
		Version: "0.3.0",
		Phase:   worker.PhaseReady,
		Package: zipPath,
	}
	setVerifiedState(t, &state, zipPath)
	if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
		t.Fatal(err)
	}

	var starts []StartAppOptions
	deps := Deps{
		CopyDir: func(src, dst string, exclude []string) error {
			if src == newDataDir {
				return errors.New("data is locked")
			}
			return DefaultDeps().CopyDir(src, dst, exclude)
		},
		Rename: func(oldPath, newPath string) error {
			if oldPath == newDataDir {
				return errors.New("data is locked")
			}
			return os.Rename(oldPath, newPath)
		},
		StartApp: func(opts StartAppOptions) (int, error) {
			starts = append(starts, opts)
			return 3000 + len(starts), nil
		},
		WaitBoot:  func(string, string, time.Duration) error { return errors.New("boot failed") },
		KillApp:   func(int) error { return nil },
		RemoveAll: os.RemoveAll,
		Now:       func() time.Time { return time.Unix(1700000400, 0) },
	}

	err := Apply(Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: zipPath,
		Version:     "0.3.0",
		BootTimeout: time.Second,
	}, deps)
	if err == nil {
		t.Fatal("expected boot-ok failure")
	}
	// The old app must be in place again even though user data could not be
	// moved back; the data stays in .new rather than being deleted.
	got, readErr := os.ReadFile(filepath.Join(appDir, AppExeName))
	if readErr != nil || string(got) != "old-exe" {
		t.Fatalf("app directory left unusable: err=%v got=%q", readErr, string(got))
	}
	if len(starts) != 2 || starts[1].ExePath != filepath.Join(appDir, AppExeName) {
		t.Fatalf("old app was not started after incomplete rollback: %+v", starts)
	}
	kept, readErr := os.ReadFile(filepath.Join(newDataDir, "user.txt"))
	if readErr != nil || string(kept) != "keep-me" {
		t.Fatalf("user data was deleted: err=%v got=%q", readErr, string(kept))
	}
}

func TestApplyBootOKFailureRollsBackAndPreservesData(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	dataDir := filepath.Join(appDir, "data")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, AppExeName), []byte("old-exe"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(appDir, "old_marker.txt"), []byte("v1"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "user.txt"), []byte("keep-me"), 0o644); err != nil {
		t.Fatal(err)
	}

	pkgPath := createTestZip(t, map[string]string{
		AppExeName:       "broken-new-exe",
		"new_marker.txt": "v2",
	})
	statePath := worker.StatePath(dataDir)
	state := worker.State{
		Schema:  worker.StateSchema,
		Version: "0.3.0",
		Phase:   worker.PhaseReady,
		Package: pkgPath,
	}
	setVerifiedState(t, &state, pkgPath)
	if err := worker.SaveState(statePath, state); err != nil {
		t.Fatal(err)
	}

	var (
		starts []StartAppOptions
		killed []int
	)
	deps := Deps{
		CopyDir: DefaultDeps().CopyDir,
		Rename:  os.Rename,
		StartApp: func(opts StartAppOptions) (int, error) {
			starts = append(starts, opts)
			return 1000 + len(starts), nil
		},
		WaitBoot: func(bootOKPath, expectedVersion string, timeout time.Duration) error {
			return errors.New("boot failed from https://user:secret@mirror.example/pkg?token=abc")
		},
		KillApp: func(pid int) error {
			killed = append(killed, pid)
			return nil
		},
		RemoveAll: os.RemoveAll,
		Now:       func() time.Time { return time.Unix(1700000200, 0) },
	}

	err := Apply(Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: pkgPath,
		Version:     "0.3.0",
		BootTimeout: 2 * time.Second,
		Logger:      logx.NewWriter(nil),
	}, deps)
	if err == nil {
		t.Fatal("expected Apply to return error when boot-ok fails")
	}

	if len(killed) != 1 || killed[0] != 1001 {
		t.Fatalf("expected KillApp(1001), got %v", killed)
	}
	if len(starts) != 2 {
		t.Fatalf("expected 2 StartApp calls (new then restored old), got %d", len(starts))
	}
	if starts[0].BootOKPath == "" || starts[1].BootOKPath != "" {
		t.Fatalf("expected BootOKPath on new start only, got %+v", starts)
	}

	// Old app restored, new files removed, data preserved.
	gotExe, err := os.ReadFile(filepath.Join(appDir, AppExeName))
	if err != nil || string(gotExe) != "old-exe" {
		t.Fatalf("expected restored old exe, err=%v got=%q", err, string(gotExe))
	}
	if _, err := os.Stat(filepath.Join(appDir, "old_marker.txt")); err != nil {
		t.Fatalf("expected old_marker.txt restored, err=%v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "new_marker.txt")); !os.IsNotExist(err) {
		t.Fatalf("expected new_marker.txt removed, err=%v", err)
	}
	gotUser, err := os.ReadFile(filepath.Join(appDir, "data", "user.txt"))
	if err != nil || string(gotUser) != "keep-me" {
		t.Fatalf("expected data/user.txt preserved after rollback, err=%v got=%q", err, string(gotUser))
	}

	st, ok, err := worker.LoadState(statePath)
	if err != nil || !ok {
		t.Fatalf("LoadState failed: ok=%v err=%v", ok, err)
	}
	if st.Phase != worker.PhaseRolledBack {
		t.Fatalf("expected phase rolled_back, got %q", st.Phase)
	}
	if st.Package != pkgPath {
		t.Fatalf("expected package kept for inspection (%q), got %q", pkgPath, st.Package)
	}
	if strings.Contains(st.LastError, "secret") || strings.Contains(st.LastError, "token=abc") {
		t.Fatalf("expected sanitized last_error, got %q", st.LastError)
	}
}

func TestDefaultWaitBoot(t *testing.T) {
	bootPath := filepath.Join(t.TempDir(), "updates", "boot-ok.json")
	if err := os.MkdirAll(filepath.Dir(bootPath), 0o755); err != nil {
		t.Fatal(err)
	}

	go func() {
		time.Sleep(60 * time.Millisecond)
		raw, _ := json.Marshal(BootOK{Version: "0.3.0", PID: 123, At: 1700000000000})
		_ = os.WriteFile(bootPath, raw, 0o644)
	}()

	if err := defaultWaitBoot(bootPath, "0.3.0", time.Second); err != nil {
		t.Fatalf("defaultWaitBoot failed: %v", err)
	}

	// Mismatched version fails.
	raw, _ := json.Marshal(BootOK{Version: "0.2.2", PID: 123, At: 1700000000000})
	_ = os.WriteFile(bootPath, raw, 0o644)
	if err := defaultWaitBoot(bootPath, "0.3.0", 200*time.Millisecond); err == nil || !strings.Contains(err.Error(), "mismatch") {
		t.Fatalf("expected version mismatch error, got %v", err)
	}
}
