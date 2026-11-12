package apply

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/version"
	"haoxiangpao/hxp-launcher/internal/worker"
)

const DefaultBootTimeout = 30 * time.Second

type Options struct {
	AppDir      string
	DataDir     string
	PackagePath string
	Version     string
	BootTimeout time.Duration
	Logger      *logx.Logger
}

type StartAppOptions struct {
	ExePath    string
	AppDir     string
	BootOKPath string
}

type BootOK struct {
	Version string `json:"version"`
	PID     int    `json:"pid"`
	At      int64  `json:"at"`
}

type Deps struct {
	CopyDir   func(src, dst string, excludeTopLevel []string) error
	Rename    func(oldPath, newPath string) error
	StartApp  func(opts StartAppOptions) (int, error)
	WaitBoot  func(bootOKPath, expectedVersion string, timeout time.Duration) error
	KillApp   func(pid int) error
	RemoveAll func(path string) error
	Now       func() time.Time
}

func DefaultDeps() Deps {
	return Deps{
		CopyDir:   defaultCopyDir,
		Rename:    defaultRename,
		StartApp:  defaultStartApp,
		WaitBoot:  defaultWaitBoot,
		KillApp:   defaultKillApp,
		RemoveAll: os.RemoveAll,
		Now:       time.Now,
	}
}

func (d Deps) withDefaults() Deps {
	def := DefaultDeps()
	if d.CopyDir == nil {
		d.CopyDir = def.CopyDir
	}
	if d.Rename == nil {
		d.Rename = def.Rename
	}
	if d.StartApp == nil {
		d.StartApp = def.StartApp
	}
	if d.WaitBoot == nil {
		d.WaitBoot = def.WaitBoot
	}
	if d.KillApp == nil {
		d.KillApp = def.KillApp
	}
	if d.RemoveAll == nil {
		d.RemoveAll = def.RemoveAll
	}
	if d.Now == nil {
		d.Now = def.Now
	}
	return d
}

// WaitForPIDExit polls until pid is no longer alive or timeout elapses.
func WaitForPIDExit(pid int, timeout time.Duration) error {
	if pid <= 0 {
		return nil
	}
	deadline := time.Now().Add(timeout)
	for {
		if !worker.IsProcessAlive(pid) {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("timed out waiting for pid %d to exit", pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// Apply extracts opts.PackagePath, atomically swaps opts.AppDir while preserving
// <appDir>/data, launches the new app with JHP_BOOT_OK_PATH, and rolls back to
// <appDir>.old if boot-ok does not succeed within opts.BootTimeout.
//
// On PhaseApplied, state.Package is cleared ("") to indicate no pending package.
// On PhaseRolledBack, state.Package is kept for inspection.
func Apply(opts Options, deps Deps) error {
	deps = deps.withDefaults()
	logger := opts.Logger
	if logger == nil {
		logger = logx.NewWriter(nil)
	}

	if strings.TrimSpace(opts.AppDir) == "" {
		return errors.New("empty app directory")
	}
	if strings.TrimSpace(opts.DataDir) == "" {
		return errors.New("empty data directory")
	}
	if strings.TrimSpace(opts.PackagePath) == "" {
		return errors.New("empty package path")
	}
	ver := strings.TrimSpace(opts.Version)
	if ver == "" || ver == "." || ver == ".." || strings.ContainsAny(ver, `/\`) {
		return fmt.Errorf("invalid target version: %q", opts.Version)
	}
	if opts.BootTimeout <= 0 {
		opts.BootTimeout = DefaultBootTimeout
	}

	appDir := filepath.Clean(opts.AppDir)
	newDir := appDir + ".new"
	oldDir := appDir + ".old"
	updatesDir := worker.UpdatesDir(opts.DataDir)
	stagingDir := filepath.Join(updatesDir, "staging-"+ver)
	bootOKPath := filepath.Join(updatesDir, "boot-ok.json")
	statePath := worker.StatePath(opts.DataDir)
	exePath := filepath.Join(appDir, AppExeName)
	appDataDir := filepath.Join(appDir, "data")
	newDataDir := filepath.Join(newDir, "data")
	oldDataDir := filepath.Join(oldDir, "data")

	state, hadState, loadErr := worker.LoadState(statePath)
	if loadErr != nil {
		logger.Warn("failed to load state before apply (%s); starting fresh state", worker.SanitizeErrorString(loadErr.Error()))
		state = worker.DefaultState()
		hadState = false
	}
	if !hadState || state.Version == "" {
		state.Version = ver
	}
	if state.Package == "" {
		state.Package = opts.PackagePath
	}

	if state.SHA256 != "" {
		if err := worker.VerifyPackageFile(opts.PackagePath, state.Size, state.SHA256); err != nil {
			state.Phase = worker.PhaseDownloading
			state.Package = ""
			state.LastError = worker.SanitizeErrorString(fmt.Sprintf("package verification: %v", err))
			state.UpdatedAt = deps.Now().Unix()
			if saveErr := worker.SaveState(statePath, state); saveErr != nil {
				logger.Warn("failed to save verification failure state: %s", worker.SanitizeErrorString(saveErr.Error()))
			}
			return fmt.Errorf("package verification: %w", err)
		}
	}

	state.Phase = worker.PhaseApplying
	state.LastError = ""
	state.UpdatedAt = deps.Now().Unix()
	if err := worker.SaveState(statePath, state); err != nil {
		logger.Warn("failed to save applying state: %s", worker.SanitizeErrorString(err.Error()))
	}

	var (
		movedData  bool
		swappedOld bool
		swappedNew bool
		startedPID int
	)

	rollback := func(cause error) error {
		logger.Error("apply failed, rolling back: %s", worker.SanitizeErrorString(cause.Error()))
		if startedPID > 0 {
			if err := deps.KillApp(startedPID); err != nil {
				logger.Warn("kill new app pid=%d: %s", startedPID, worker.SanitizeErrorString(err.Error()))
			}
		}

		switch {
		case swappedNew:
			dataMovedBack := !movedData
			if movedData {
				if err := deps.Rename(filepath.Join(appDir, "data"), oldDataDir); err != nil {
					logger.Warn("restore data from new appDir failed: %s", worker.SanitizeErrorString(err.Error()))
				} else {
					dataMovedBack = true
				}
			}
			if dataMovedBack {
				if err := deps.RemoveAll(appDir); err != nil {
					logger.Warn("remove failed new appDir: %s", worker.SanitizeErrorString(err.Error()))
				}
			} else {
				_ = removeDirExcept(appDir, "data", deps.RemoveAll)
			}
			if err := deps.Rename(oldDir, appDir); err != nil {
				logger.Error("restore .old -> appDir failed: %s", worker.SanitizeErrorString(err.Error()))
			}
		case swappedOld:
			if movedData {
				if err := deps.Rename(newDataDir, oldDataDir); err != nil {
					logger.Warn("restore data from .new -> .old failed: %s", worker.SanitizeErrorString(err.Error()))
				}
			}
			_ = deps.RemoveAll(newDir)
			if err := deps.Rename(oldDir, appDir); err != nil {
				logger.Error("restore .old -> appDir failed: %s", worker.SanitizeErrorString(err.Error()))
			}
		default:
			if movedData {
				if err := deps.Rename(newDataDir, appDataDir); err != nil {
					logger.Warn("restore data from .new -> appDir failed: %s", worker.SanitizeErrorString(err.Error()))
				}
			}
			_ = deps.RemoveAll(newDir)
		}

		if oldPID, startErr := deps.StartApp(StartAppOptions{
			ExePath: exePath,
			AppDir:  appDir,
		}); startErr != nil {
			logger.Warn("failed to restart old app after rollback: %s", worker.SanitizeErrorString(startErr.Error()))
		} else {
			logger.Info("restarted old app after rollback (pid=%d)", oldPID)
		}

		state.Phase = worker.PhaseRolledBack
		if state.Package == "" {
			state.Package = opts.PackagePath
		}
		state.LastError = worker.SanitizeErrorString(cause.Error())
		state.UpdatedAt = deps.Now().Unix()
		if saveErr := worker.SaveState(statePath, state); saveErr != nil {
			logger.Warn("failed to save rolled_back state: %s", worker.SanitizeErrorString(saveErr.Error()))
		}
		return cause
	}

	if err := ExtractZip(opts.PackagePath, stagingDir); err != nil {
		return rollback(fmt.Errorf("extract package: %w", err))
	}

	if !dirExists(appDir) && dirExists(oldDir) {
		if err := deps.Rename(oldDir, appDir); err != nil {
			return rollback(fmt.Errorf("recover pre-existing .old: %w", err))
		}
	}
	_ = deps.RemoveAll(newDir)
	_ = deps.RemoveAll(oldDir)

	if err := deps.CopyDir(stagingDir, newDir, []string{"data"}); err != nil {
		return rollback(fmt.Errorf("copy staging to .new: %w", err))
	}

	if dirExists(appDataDir) {
		if err := deps.Rename(appDataDir, newDataDir); err != nil {
			return rollback(fmt.Errorf("preserve data dir: %w", err))
		}
		movedData = true
	}

	if err := deps.Rename(appDir, oldDir); err != nil {
		return rollback(fmt.Errorf("rename appDir to .old: %w", err))
	}
	swappedOld = true

	if err := deps.Rename(newDir, appDir); err != nil {
		return rollback(fmt.Errorf("rename .new to appDir: %w", err))
	}
	swappedNew = true

	_ = os.Remove(bootOKPath)

	pid, err := deps.StartApp(StartAppOptions{
		ExePath:    exePath,
		AppDir:     appDir,
		BootOKPath: bootOKPath,
	})
	if err != nil {
		return rollback(fmt.Errorf("start new app: %w", err))
	}
	startedPID = pid

	if err := deps.WaitBoot(bootOKPath, ver, opts.BootTimeout); err != nil {
		return rollback(fmt.Errorf("boot-ok check: %w", err))
	}

	if err := deps.RemoveAll(oldDir); err != nil {
		logger.Warn("remove .old dir after apply: %s", worker.SanitizeErrorString(err.Error()))
	}
	_ = deps.RemoveAll(stagingDir)

	state.Phase = worker.PhaseApplied
	state.Version = ver
	state.Package = ""
	state.LastError = ""
	state.UpdatedAt = deps.Now().Unix()
	if err := worker.SaveState(statePath, state); err != nil {
		logger.Warn("failed to save applied state: %s", worker.SanitizeErrorString(err.Error()))
	}
	logger.Info("update applied successfully: version=%s pid=%d", ver, startedPID)
	return nil
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func removeDirExcept(dir, keepName string, removeAll func(string) error) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	var firstErr error
	for _, e := range entries {
		if strings.EqualFold(e.Name(), keepName) {
			continue
		}
		if err := removeAll(filepath.Join(dir, e.Name())); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func defaultCopyDir(src, dst string, excludeTopLevel []string) error {
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		top := rel
		if idx := strings.IndexRune(rel, filepath.Separator); idx >= 0 {
			top = rel[:idx]
		}
		for _, ex := range excludeTopLevel {
			if strings.EqualFold(top, ex) {
				if d.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		return copyRegularFile(path, target, info.Mode().Perm())
	})
}

func copyRegularFile(src, dst string, mode fs.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	if mode == 0 {
		mode = 0o644
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(out, in)
	closeErr := out.Close()
	if copyErr != nil {
		return copyErr
	}
	return closeErr
}

func defaultRename(oldPath, newPath string) error {
	var err error
	for attempt := 0; attempt < 5; attempt++ {
		err = os.Rename(oldPath, newPath)
		if err == nil || errors.Is(err, os.ErrNotExist) {
			return err
		}
		time.Sleep(50 * time.Millisecond)
	}
	return err
}

func defaultStartApp(opts StartAppOptions) (int, error) {
	if strings.TrimSpace(opts.ExePath) == "" {
		return 0, errors.New("empty exe path")
	}
	cmd := exec.Command(opts.ExePath)
	if opts.AppDir != "" {
		cmd.Dir = opts.AppDir
	} else {
		cmd.Dir = filepath.Dir(opts.ExePath)
	}
	if opts.BootOKPath != "" {
		cmd.Env = withEnv(os.Environ(), "JHP_BOOT_OK_PATH", opts.BootOKPath)
	}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	return pid, nil
}

func withEnv(env []string, key, value string) []string {
	out := make([]string, 0, len(env)+1)
	for _, entry := range env {
		k, _, ok := strings.Cut(entry, "=")
		if ok && strings.EqualFold(k, key) {
			continue
		}
		out = append(out, entry)
	}
	out = append(out, key+"="+value)
	return out
}

func defaultWaitBoot(bootOKPath, expectedVersion string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		raw, err := os.ReadFile(bootOKPath)
		if err == nil {
			var marker BootOK
			if jsonErr := json.Unmarshal(raw, &marker); jsonErr == nil && strings.TrimSpace(marker.Version) != "" {
				if version.Same(marker.Version, expectedVersion) || strings.TrimSpace(marker.Version) == strings.TrimSpace(expectedVersion) {
					return nil
				}
				return fmt.Errorf("boot-ok version mismatch: got %q, want %q", marker.Version, expectedVersion)
			}
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("boot-ok timeout after %s", timeout)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func defaultKillApp(pid int) error {
	if pid <= 0 {
		return nil
	}
	p, err := os.FindProcess(pid)
	if err != nil {
		return nil
	}
	if err := p.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	_ = WaitForPIDExit(pid, 2*time.Second)
	return nil
}
