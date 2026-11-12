//go:build windows

package worker

import (
	"errors"
	"syscall"
)

const (
	detachedProcess        = 0x00000008
	createNewProcessGroup  = 0x00000200
	processSynchronize     = 0x00100000
	processQueryLimitedInf = 0x00001000
)

func DetachedSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		CreationFlags: detachedProcess | createNewProcessGroup,
		HideWindow:    true,
	}
}

func IsProcessAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	h, err := syscall.OpenProcess(processSynchronize|processQueryLimitedInf, false, uint32(pid))
	if err != nil {
		var errno syscall.Errno
		if errors.As(err, &errno) && errno == syscall.ERROR_ACCESS_DENIED {
			return true
		}
		return false
	}
	defer syscall.CloseHandle(h)
	event, err := syscall.WaitForSingleObject(h, 0)
	if err != nil {
		return false
	}
	return event == syscall.WAIT_TIMEOUT
}

func isProcessAlive(pid int) bool {
	return IsProcessAlive(pid)
}
