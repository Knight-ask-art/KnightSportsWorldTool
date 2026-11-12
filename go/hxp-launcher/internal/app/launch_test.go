package app

import (
	"path/filepath"
	"testing"
)

func TestLaunchDryRunDoesNotStartProcess(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist.exe")
	pid, err := Launch(LaunchOptions{AppPath: missing, DryRun: true})
	if err != nil {
		t.Fatalf("expected dry-run not to fail or start process, got %v", err)
	}
	if pid != 0 {
		t.Fatalf("expected pid 0 in dry-run, got %d", pid)
	}
}

func TestResolveAppPathMakesBareNameAbsolute(t *testing.T) {
	got, err := resolveAppPath("mytestapp.exe")
	if err != nil {
		t.Fatalf("resolveAppPath: %v", err)
	}
	if !filepath.IsAbs(got) {
		t.Fatalf("expected absolute path, got %q", got)
	}
	if filepath.Base(got) != "mytestapp.exe" {
		t.Fatalf("unexpected basename %q", got)
	}
}

func TestLaunchMissingBinaryFails(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does-not-exist.exe")
	if _, err := Launch(LaunchOptions{AppPath: missing, DryRun: false}); err == nil {
		t.Fatal("expected error launching missing executable")
	}
}
