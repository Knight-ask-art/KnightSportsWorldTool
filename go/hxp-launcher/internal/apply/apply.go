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

	startAppAt := func(dir string) {
		exe := filepath.Join(dir, AppExeName)
		if _, err := os.Stat(exe); err != nil {
			logger.Error("cannot start app from %s: %s", dir, worker.SanitizeErrorString(err.Error()))
			return
		}
		pid, err := deps.StartApp(StartAppOptions{ExePath: exe, AppDir: dir})
		if err != nil {
			logger.Warn("failed to start app from %s after apply failure: %s", dir, worker.SanitizeErrorString(err.Error()))
			return
		}
		logger.Info("started app from %s (pid=%d)", dir, pid)
	}

	state, hadState, loadErr := worker.LoadState(statePath)
	if loadErr != nil {
		startAppAt(appDir)
		return fmt.Errorf("load download state: %w", loadErr)
	}
	if !hadState {
		startAppAt(appDir)
		return errors.New("download state missing; refusing to apply")
	}

	invalidState := func(reason string, err error) error {
		state.Phase = worker.PhaseDownloading
		state.Package = ""
		state.LastError = worker.SanitizeErrorString(reason)
		state.UpdatedAt = deps.Now().Unix()
		if saveErr := worker.SaveState(statePath, state); saveErr != nil {
			logger.Warn("failed to save invalid state: %s", worker.SanitizeErrorString(saveErr.Error()))
		}
		startAppAt(appDir)
		return err
	}

	if state.Phase != worker.PhaseReady {
		startAppAt(appDir)
		return fmt.Errorf("download state phase %q is not ready", state.Phase)
	}
	if !version.Same(state.Version, ver) {
		return invalidState(
			fmt.Sprintf("download state version %q does not match %q", state.Version, ver),
			fmt.Errorf("download state version %q does not match requested %q", state.Version, ver),
		)
	}
	if state.Size <= 0 || len(state.SHA256) != 64 {
		return invalidState(
			"download state is missing sha256/size",
			errors.New("download state is missing sha256/size; refusing to apply an unverified package"),
		)
	}
	if state.File != "" && filepath.Base(opts.PackagePath) != state.File {
		return invalidState(
			fmt.Sprintf("package file %q does not match state file %q", filepath.Base(opts.PackagePath), state.File),
			fmt.Errorf("package file %q does not match download state file %q", filepath.Base(opts.PackagePath), state.File),
		)
	}
	if state.Package != "" && filepath.Clean(state.Package) != filepath.Clean(opts.PackagePath) {
		return invalidState(
			fmt.Sprintf("package path %q does not match state path %q", opts.PackagePath, state.Package),
			fmt.Errorf("package path %q does not match download state path %q", opts.PackagePath, state.Package),
		)
	}
	if state.Package == "" {
		state.Package = opts.PackagePath
	}
	if err := worker.VerifyPackageFile(opts.PackagePath, state.Size, state.SHA256); err != nil {
		return invalidState(
			fmt.Sprintf("package verification: %v", err),
			fmt.Errorf("package verification: %w", err),
		)
	}

	state.Phase = worker.PhaseApplying
	state.LastError = ""
	state.UpdatedAt = deps.Now().Unix()
	if err := worker.SaveState(statePath, state); err != nil {
		startAppAt(appDir)
		return fmt.Errorf("save applying state: %w", err)
	}

	var (
		movedData  bool
		swappedOld bool
		swappedNew bool
		startedPID int
	)

	// restoreData moves src to dst, falling back to a copy when a handle still
	// holds a file open.  It never deletes src; callers decide cleanup only
	// after this returns true.
	restoreData := func(src, dst string) bool {
		if !dirExists(src) {
			return true
		}
		if dirExists(dst) {
			logger.Warn("both %s and %s exist; keeping both", src, dst)
			return false
		}
		if err := deps.Rename(src, dst); err == nil {
			return true
		} else {
			logger.Warn("rename data %s -> %s failed: %s", src, dst, worker.SanitizeErrorString(err.Error()))
		}
		if err := deps.CopyDir(src, dst, nil); err != nil {
			logger.Error("copy data %s -> %s failed: %s", src, dst, worker.SanitizeErrorString(err.Error()))
			return false
		}
		return true
	}

	rollback := func(cause error) error {
		logger.Error("apply failed, rolling back: %s", worker.SanitizeErrorString(cause.Error()))
		if startedPID > 0 {
			if err := deps.KillApp(startedPID); err != nil {
				logger.Warn("kill new app pid=%d: %s", startedPID, worker.SanitizeErrorString(err.Error()))
			}
		}

		oldAppRestored := false
		switch {
		case swappedNew:
			// Reverse the swap without deleting either app directory.  The new
			// app (and any user data) moves back to .new first; if that rename
			// fails we leave it in place and start it rather than destroying it.
			if err := deps.Rename(appDir, newDir); err != nil {
				logger.Error("cannot move failed new app aside: %s", worker.SanitizeErrorString(err.Error()))
				break
			}
			if err := deps.Rename(oldDir, appDir); err != nil {
				logger.Error("restore .old -> appDir failed: %s", worker.SanitizeErrorString(err.Error()))
				if backErr := deps.Rename(newDir, appDir); backErr != nil {
					logger.Error("cannot put failed new app back: %s", worker.SanitizeErrorString(backErr.Error()))
				}
				break
			}
			oldAppRestored = true
			if movedData || dirExists(newDataDir) {
				if restoreData(newDataDir, appDataDir) {
					_ = deps.RemoveAll(newDir)
				}
			} else {
				_ = deps.RemoveAll(newDir)
			}
		case swappedOld:
			if err := deps.Rename(oldDir, appDir); err != nil {
				logger.Error("restore .old -> appDir failed: %s", worker.SanitizeErrorString(err.Error()))
				break
			}
			oldAppRestored = true
			if movedData || dirExists(newDataDir) {
				if restoreData(newDataDir, appDataDir) {
					_ = deps.RemoveAll(newDir)
				}
			} else {
				_ = deps.RemoveAll(newDir)
			}
		default:
			if movedData || dirExists(newDataDir) {
				if restoreData(newDataDir, appDataDir) {
					_ = deps.RemoveAll(newDir)
				}
			} else {
				_ = deps.RemoveAll(newDir)
			}
			oldAppRestored = dirExists(appDir)
		}

		switch {
		case oldAppRestored:
			startAppAt(appDir)
		case dirExists(filepath.Join(appDir, AppExeName)):
			// Rollback could not complete; keep the new app usable and leave
			// the failed directory for the next startup to recover.
			logger.Warn("rollback incomplete; starting current app so the install is not left dead")
			startAppAt(appDir)
		case dirExists(filepath.Join(oldDir, AppExeName)):
			logger.Warn("restoring old app from %s", oldDir)
			startAppAt(oldDir)
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
	// Clean stale staging directories without ever deleting the last copy of
	// user data.  A crash can leave data inside .new while appDir is missing.
	if dirExists(newDir) {
		if dirExists(newDataDir) {
			if dirExists(appDataDir) || !restoreData(newDataDir, appDataDir) {
				return rollback(errors.New("stale .new contains data that cannot be merged safely"))
			}
		}
		if dirExists(newDataDir) {
			return rollback(errors.New("stale .new still contains data"))
		}
		if err := deps.RemoveAll(newDir); err != nil {
			return rollback(fmt.Errorf("remove stale .new: %w", err))
		}
	}
	if dirExists(oldDir) {
		if !dirExists(appDataDir) && dirExists(oldDataDir) {
			if !restoreData(oldDataDir, appDataDir) {
				return rollback(errors.New("stale .old data cannot be restored safely"))
			}
		}
		if dirExists(oldDataDir) {
			return rollback(errors.New("stale .old still contains data"))
		}
		if err := deps.RemoveAll(oldDir); err != nil {
			return rollback(fmt.Errorf("remove stale .old: %w", err))
		}
	}
	if !dirExists(appDir) {
		return fmt.Errorf("app directory %s is missing", appDir)
	}

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
