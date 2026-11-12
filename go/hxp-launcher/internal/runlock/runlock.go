// Package runlock serializes launcher and apply-helper runs so two clicks
// cannot run Recover or swap app directories at the same time.
package runlock

import "time"

const (
	// LauncherName is a machine-local (per-session) named mutex.  It is
	// deliberately different from the app's own single-instance mutex.
	LauncherName = `Local\Haoxiangpao.Launcher.v1`
)

// Lock is an acquired launcher lock.  Release is safe to call exactly once.
type Lock interface {
	Release() error
}

// Acquire waits up to timeout for the named lock.
func Acquire(name string, timeout time.Duration) (Lock, bool, error) {
	deadline := time.Now().Add(timeout)
	for {
		lock, ok, err := TryAcquire(name)
		if err != nil || ok {
			return lock, ok, err
		}
		if !time.Now().Before(deadline) {
			return nil, false, nil
		}
		time.Sleep(100 * time.Millisecond)
	}
}
