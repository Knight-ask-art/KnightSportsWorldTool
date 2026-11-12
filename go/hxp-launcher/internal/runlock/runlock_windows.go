//go:build windows

package runlock

import (
	"syscall"
	"unsafe"
)

const errorAlreadyExists = syscall.Errno(183)

var (
	kernel32         = syscall.NewLazyDLL("kernel32.dll")
	procCreateMutexW = kernel32.NewProc("CreateMutexW")
)

type windowsLock struct {
	handle syscall.Handle
}

func TryAcquire(name string) (Lock, bool, error) {
	namePtr, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return nil, false, err
	}
	handle, _, callErr := procCreateMutexW.Call(0, 0, uintptr(unsafe.Pointer(namePtr)))
	if handle == 0 {
		return nil, false, callErr
	}
	if callErr == errorAlreadyExists {
		_ = syscall.CloseHandle(syscall.Handle(handle))
		return nil, false, nil
	}
	return &windowsLock{handle: syscall.Handle(handle)}, true, nil
}

func (l *windowsLock) Release() error {
	if l == nil || l.handle == 0 {
		return nil
	}
	err := syscall.CloseHandle(l.handle)
	l.handle = 0
	return err
}
