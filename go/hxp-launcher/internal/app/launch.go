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
	cmd := exec.Command(opts.AppPath)
	cmd.Dir = filepath.Dir(opts.AppPath)
	if err := cmd.Start(); err != nil {
		return 0, fmt.Errorf("start %s: %w", opts.AppPath, err)
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	return pid, nil
}
