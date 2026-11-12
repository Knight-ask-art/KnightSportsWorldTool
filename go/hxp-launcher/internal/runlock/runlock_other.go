//go:build !windows

package runlock

type noopLock struct{}

// TryAcquire is a no-op outside Windows; the launcher targets Windows.
func TryAcquire(string) (Lock, bool, error) {
	return noopLock{}, true, nil
}

func (noopLock) Release() error { return nil }
