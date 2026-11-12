package apply

import (
	"os"
	"path/filepath"
	"testing"

	"haoxiangpao/hxp-launcher/internal/worker"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func saveTestState(t *testing.T, dataDir, phase string) {
	t.Helper()
	state := worker.DefaultState()
	state.Phase = phase
	state.Version = "9.9.9"
	if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
		t.Fatal(err)
	}
}

func TestRecoverRestoresOldWhenAppMissing(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	oldDir := appDir + ".old"
	dataDir := filepath.Join(appDir, "data")
	writeFile(t, filepath.Join(oldDir, "data", "session.json"), `{"ok":1}`)
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	saveTestState(t, dataDir, worker.PhaseApplying)

	if err := Recover(appDir, dataDir, nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "data", "session.json")); err != nil {
		t.Fatalf("restored data missing: %v", err)
	}
	state, _, err := worker.LoadState(worker.StatePath(dataDir))
	if err != nil {
		t.Fatal(err)
	}
	if state.Phase != worker.PhaseRolledBack {
		t.Fatalf("phase=%q, want rolled_back", state.Phase)
	}
}

func TestRecoverRollsBackApplyingSwap(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	oldDir := appDir + ".old"
	dataDir := filepath.Join(appDir, "data")
	writeFile(t, filepath.Join(appDir, "new.exe"), "new")
	writeFile(t, filepath.Join(appDir, "data", "new.json"), "new-data")
	writeFile(t, filepath.Join(oldDir, "old.exe"), "old")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	saveTestState(t, dataDir, worker.PhaseApplying)

	if err := Recover(appDir, dataDir, nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "old.exe")); err != nil {
		t.Fatalf("old app not restored: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "data", "new.json")); err != nil {
		t.Fatalf("data not preserved: %v", err)
	}
	if _, err := os.Stat(oldDir); !os.IsNotExist(err) {
		t.Fatalf(".old should be gone after rollback")
	}
}

func TestRecoverRescuesDataFromNewWhenAppMissing(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	oldDir := appDir + ".old"
	newDir := appDir + ".new"
	writeFile(t, filepath.Join(oldDir, "好想跑.exe"), "old")
	writeFile(t, filepath.Join(newDir, "data", "user.txt"), "latest-user-data")

	if err := Recover(appDir, filepath.Join(appDir, "data"), nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "好想跑.exe")); err != nil {
		t.Fatalf("old app not restored: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(appDir, "data", "user.txt"))
	if err != nil || string(got) != "latest-user-data" {
		t.Fatalf("user data not rescued: err=%v got=%q", err, string(got))
	}
	if _, err := os.Stat(newDir); !os.IsNotExist(err) {
		t.Fatalf("data-free .new should be removed, err=%v", err)
	}

	// A second startup must not delete the rescued data.
	if err := Recover(appDir, filepath.Join(appDir, "data"), nil); err != nil {
		t.Fatalf("second Recover: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "data", "user.txt")); err != nil {
		t.Fatalf("second Recover lost data: %v", err)
	}
}

func TestRecoverRescuesDataWhenAppExists(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	newDir := appDir + ".new"
	writeFile(t, filepath.Join(appDir, "好想跑.exe"), "old")
	writeFile(t, filepath.Join(newDir, "data", "user.txt"), "moved-data")

	if err := Recover(appDir, filepath.Join(appDir, "data"), nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(appDir, "data", "user.txt"))
	if err != nil || string(got) != "moved-data" {
		t.Fatalf("user data not rescued: err=%v got=%q", err, string(got))
	}
	if _, err := os.Stat(newDir); !os.IsNotExist(err) {
		t.Fatalf("data-free .new should be removed, err=%v", err)
	}
}

func TestRecoverKeepsNewDirWhenDataCannotBeMerged(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	newDir := appDir + ".new"
	writeFile(t, filepath.Join(appDir, "好想跑.exe"), "old")
	writeFile(t, filepath.Join(appDir, "data", "existing.txt"), "existing")
	writeFile(t, filepath.Join(newDir, "data", "user.txt"), "moved-data")

	if err := Recover(appDir, filepath.Join(appDir, "data"), nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if _, err := os.Stat(filepath.Join(newDir, "data", "user.txt")); err != nil {
		t.Fatalf("Recover deleted unrescued data: %v", err)
	}
	if _, err := os.Stat(filepath.Join(appDir, "data", "existing.txt")); err != nil {
		t.Fatalf("Recover disturbed existing data: %v", err)
	}
}

func TestRecoverCleansAppliedOld(t *testing.T) {
	root := t.TempDir()
	appDir := filepath.Join(root, "app")
	oldDir := appDir + ".old"
	dataDir := filepath.Join(appDir, "data")
	writeFile(t, filepath.Join(appDir, "好想跑.exe"), "new")
	writeFile(t, filepath.Join(oldDir, "好想跑.exe"), "old")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	saveTestState(t, dataDir, worker.PhaseApplied)

	if err := Recover(appDir, dataDir, nil); err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if _, err := os.Stat(oldDir); !os.IsNotExist(err) {
		t.Fatalf(".old should be removed after applied")
	}
	if _, err := os.Stat(filepath.Join(appDir, "好想跑.exe")); err != nil {
		t.Fatalf("app missing after cleanup: %v", err)
	}
}
