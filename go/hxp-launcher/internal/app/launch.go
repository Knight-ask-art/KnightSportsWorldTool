package app

import (
	"fmt"
	"os/exec"
	"path/filepath"
)

type LaunchOptions struct {
	AppPath string
	DryRun  bool
}

func Launch(opts LaunchOptions) (int, error) {
	if opts.AppPath == "" {
		return 0, fmt.Errorf("empty app path")
	}
	if opts.DryRun {
		return 0, nil
	}
	appPath, err := resolveAppPath(opts.AppPath)
	if err != nil {
		return 0, err
	}
	cmd := exec.Command(appPath)
	cmd.Dir = filepath.Dir(appPath)
	if err := cmd.Start(); err != nil {
		return 0, fmt.Errorf("start %s: %w", appPath, err)
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	return pid, nil
}

// resolveAppPath makes a bare "app.exe" resolve against the current directory
// instead of letting exec.Command search only %PATH%.
func resolveAppPath(path string) (string, error) {
	if filepath.IsAbs(path) {
		return path, nil
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("resolve app path %s: %w", path, err)
	}
	return abs, nil
}
