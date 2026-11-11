package guard

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"runtime"
	"sync"
)

// ErrNonceTooShort is returned when attest cannot seed the fragment
// permutation.  Callers pass 16 random bytes from the Python orchestrator.
var ErrNonceTooShort = errors.New("nonce too short")

// attestKeystreamTag domain-separates the XOR keystream from every other use
// of SHA-256 in this package.
const attestKeystreamTag = "hxp.attest.keystream.v1"

type fragmentResult struct {
	index int
	value string
	err   error
}

// Attest collects five machine fragments in parallel, orders them with a
// nonce-seeded Fisher-Yates shuffle, hashes len|fragment, and XORs the digest
// with a nonce-derived keystream.
//
// The result is 32 opaque bytes.  No fragment is ever returned to the caller
// or printed; a failure in any of the five collectors fails the whole call.
func (g *Guard) Attest(nonce []byte) ([]byte, error) {
	if len(nonce) < 8 {
		return nil, ErrNonceTooShort
	}

	collectors := []func() (string, error){
		func() (string, error) {
			// Match New's fallback: a restricted PATH (reg.exe blocked by
			// AppLocker, for example) must not fail the whole attestation.
			if id := machineID(); id != "" {
				return id, nil
			}
			return g.DeviceID, nil
		},
		func() (string, error) {
			name, err := os.Hostname()
			if err != nil {
				return "", err
			}
			if name == "" {
				return "", errors.New("hostname unavailable")
			}
			return name, nil
		},
		func() (string, error) {
			// Read-only on purpose: attestation must not require a writable
			// data/ (an installed app directory may be read-only).  The
			// device id already covers the no-machine-id fallback.
			if id := readInstallID(g.Path); id != "" {
				return id, nil
			}
			return g.DeviceID, nil
		},
		func() (string, error) {
			return fmt.Sprintf("%s/%s/%d|%s",
				runtime.GOOS, runtime.GOARCH, runtime.NumCPU(),
				runtime.Version()), nil
		},
		func() (string, error) {
			home, err := os.UserHomeDir()
			if err != nil {
				return "", err
			}
			// An empty PROCESSOR_IDENTIFIER is acceptable; this collector
			// must never panic.
			return home + "\x00" + os.Getenv("PROCESSOR_IDENTIFIER"), nil
		},
	}

	results := make(chan fragmentResult, len(collectors))
	var wg sync.WaitGroup
	for i, collect := range collectors {
		wg.Add(1)
		go func(index int, fn func() (string, error)) {
			defer wg.Done()
			value, err := fn()
			results <- fragmentResult{index: index, value: value, err: err}
		}(i, collect)
	}
	wg.Wait()
	close(results)

	parts := make([]string, len(collectors))
	for result := range results {
		if result.err != nil {
			return nil, result.err
		}
		parts[result.index] = result.value
	}

	// The permutation is deterministic per nonce, so the same call twice
	// yields the same blob while a different nonce reshuffles the fragments.
	rng := rand.New(rand.NewSource(int64(binary.LittleEndian.Uint64(nonce[:8]))))
	rng.Shuffle(len(parts), func(i, j int) {
		parts[i], parts[j] = parts[j], parts[i]
	})

	digest := sha256.New()
	var size [4]byte
	for _, part := range parts {
		binary.LittleEndian.PutUint32(size[:], uint32(len(part)))
		digest.Write(size[:])
		digest.Write([]byte(part))
	}
	sum := digest.Sum(nil)

	keyMaterial := make([]byte, 0, len(attestKeystreamTag)+len(nonce))
	keyMaterial = append(keyMaterial, attestKeystreamTag...)
	keyMaterial = append(keyMaterial, nonce...)
	keystream := sha256.Sum256(keyMaterial)

	out := make([]byte, sha256.Size)
	for i := range out {
		out[i] = sum[i] ^ keystream[i]
	}
	return out, nil
}
