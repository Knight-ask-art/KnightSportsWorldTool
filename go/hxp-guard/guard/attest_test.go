package guard

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func attestGuard(t *testing.T) *Guard {
	t.Helper()
	// A real path is required: the read-only install-id collector looks next
	// to it (and must not create anything).
	return newWithSecret(tempPath(t), "attest-secret")
}

func TestAttestIsStableForTheSameNonce(t *testing.T) {
	g := attestGuard(t)
	nonce := []byte("0123456789abcdef")
	first, err := g.Attest(nonce)
	if err != nil {
		t.Fatalf("Attest: %v", err)
	}
	second, err := g.Attest(nonce)
	if err != nil {
		t.Fatalf("Attest again: %v", err)
	}
	if !bytes.Equal(first, second) {
		t.Fatalf("same nonce produced different blobs")
	}
}

func TestAttestChangesWithTheNonce(t *testing.T) {
	g := attestGuard(t)
	a, err := g.Attest([]byte("0123456789abcdef"))
	if err != nil {
		t.Fatalf("Attest: %v", err)
	}
	b, err := g.Attest([]byte("fedcba9876543210"))
	if err != nil {
		t.Fatalf("Attest: %v", err)
	}
	if bytes.Equal(a, b) {
		t.Fatalf("different nonces produced the same blob")
	}
}

func TestAttestReturnsThirtyTwoBytes(t *testing.T) {
	g := attestGuard(t)
	blob, err := g.Attest([]byte("0123456789abcdef"))
	if err != nil {
		t.Fatalf("Attest: %v", err)
	}
	if len(blob) != 32 {
		t.Fatalf("len(blob) = %d, want 32", len(blob))
	}
}

func TestAttestRejectsAnEmptyNonce(t *testing.T) {
	g := attestGuard(t)
	for _, nonce := range [][]byte{nil, {}, []byte("short")} {
		if _, err := g.Attest(nonce); err == nil {
			t.Fatalf("Attest(%d bytes) should fail", len(nonce))
		}
	}
}

func TestAttestDoesNotCreateAnInstallID(t *testing.T) {
	g := attestGuard(t)
	if _, err := g.Attest([]byte("0123456789abcdef")); err != nil {
		t.Fatalf("Attest: %v", err)
	}
	// The attestation path is read-only: a read-only data/ (Program Files)
	// must not fail the login.
	path := filepath.Join(filepath.Dir(g.Path), "install_id")
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("Attest created %s (err=%v)", path, err)
	}
}

func TestReadInstallIDIsReadOnly(t *testing.T) {
	state := tempPath(t)
	if id := readInstallID(state); id != "" {
		t.Fatalf("readInstallID on a missing file = %q", id)
	}
	path := filepath.Join(filepath.Dir(state), "install_id")
	if err := os.WriteFile(path, []byte("0123456789abcdef01234567\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if id := readInstallID(state); id != "0123456789abcdef01234567" {
		t.Fatalf("readInstallID = %q", id)
	}
}

func TestAttestConcurrentCallersDoNotShareState(t *testing.T) {
	g := attestGuard(t)
	const workers = 16
	want, err := g.Attest([]byte("0123456789abcdef"))
	if err != nil {
		t.Fatalf("baseline Attest: %v", err)
	}

	var wg sync.WaitGroup
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := g.Attest([]byte("0123456789abcdef"))
			if err != nil {
				errs <- err
				return
			}
			if !bytes.Equal(got, want) {
				errs <- errAttestMismatch
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent Attest: %v", err)
	}
}

var errAttestMismatch = &attestError{}

type attestError struct{}

func (*attestError) Error() string { return "concurrent attest blob mismatch" }
