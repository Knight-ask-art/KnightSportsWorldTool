package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/app"
	"haoxiangpao/hxp-launcher/internal/apply"
	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/manifest"
	"haoxiangpao/hxp-launcher/internal/runlock"
	"haoxiangpao/hxp-launcher/internal/version"
	"haoxiangpao/hxp-launcher/internal/worker"
)

type CheckSummary struct {
	State      string   `json:"state"`
	Verified   bool     `json:"verified"`
	Current    string   `json:"current"`
	Latest     string   `json:"latest,omitempty"`
	MinVersion string   `json:"min_version,omitempty"`
	Required   bool     `json:"required"`
	Source     string   `json:"source,omitempty"`
	KeyID      string   `json:"key_id,omitempty"`
	Notes      []string `json:"notes,omitempty"`
	Message    string   `json:"message"`
	Error      string   `json:"error,omitempty"`
}

type WorkerSpawnOptions struct {
	ExePath      string
	AppPath      string
	DataDir      string
	ManifestURLs []string
	TimeoutSec   int
	LogPath      string
	ParentPID    int
	AllowLocal   bool
	PubkeyPath   string
}

type ApplySpawnOptions struct {
	ExePath        string
	AppPath        string
	DataDir        string
	PackagePath    string
	Version        string
	WaitPID        int
	BootTimeoutSec int
	LogPath        string
}

type launcherDeps struct {
	launch      func(app.LaunchOptions) (int, error)
	spawnWorker func(WorkerSpawnOptions) (int, error)
	spawnApply  func(ApplySpawnOptions) (int, error)
	runApply    func(apply.Options) error
	currentPID  func() int
}

func defaultLauncherDeps() launcherDeps {
	return launcherDeps{
		launch:      app.Launch,
		spawnWorker: spawnWorker,
		spawnApply:  spawnApplyHelper,
		runApply: func(opts apply.Options) error {
			return apply.Apply(opts, apply.DefaultDeps())
		},
		currentPID: os.Getpid,
	}
}

func main() {
	exe, err := os.Executable()
	launcherDir := "."
	if err == nil {
		launcherDir = filepath.Dir(exe)
	}
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, launcherDir))
}

func run(args []string, stdout, stderr io.Writer, launcherDir string) int {
	return runWithDeps(args, stdout, stderr, launcherDir, defaultLauncherDeps())
}

func runWithDeps(args []string, stdout, stderr io.Writer, launcherDir string, deps launcherDeps) int {
	if deps.launch == nil {
		deps.launch = app.Launch
	}
	if deps.spawnWorker == nil {
		deps.spawnWorker = spawnWorker
	}
	if deps.spawnApply == nil {
		deps.spawnApply = spawnApplyHelper
	}
	if deps.runApply == nil {
		deps.runApply = func(opts apply.Options) error {
			return apply.Apply(opts, apply.DefaultDeps())
		}
	}
	if deps.currentPID == nil {
		deps.currentPID = os.Getpid
	}

	var (
		appPath             string
		dataDir             string
		manifestRaw         string
		checkMode           bool
		noUpdate            bool
		dryRun              bool
		allowLocal          bool
		pubkeyPath          string
		timeoutSec          int
		logPath             string
		updateWorker        bool
		workerOnce          bool
		parentPID           int
		applyHelper         bool
		applyPackage        string
		applyVersion        string
		applyWaitPID        int
		applyBootTimeoutSec int
	)

	fs := flag.NewFlagSet("hxp-launcher", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(&appPath, "app", "", "path to 好想跑.exe")
	fs.StringVar(&dataDir, "data", "", "path to data directory")
	fs.StringVar(&manifestRaw, "manifest", "", "manifest URL (or comma-separated URLs)")
	fs.BoolVar(&checkMode, "check", false, "verify manifest, print JSON summary, do not launch")
	fs.BoolVar(&noUpdate, "no-update", false, "skip manifest check, launch immediately")
	fs.BoolVar(&dryRun, "dry-run", false, "do everything except StartProcess")
	fs.BoolVar(&allowLocal, "allow-local", false, "allow file:// and http://localhost manifests")
	fs.StringVar(&pubkeyPath, "pubkey", "", "public key file path for --allow-local tests")
	fs.IntVar(&timeoutSec, "timeout", 8, "manifest fetch timeout in seconds")
	fs.StringVar(&logPath, "log", "", "log file path")
	fs.BoolVar(&updateWorker, "update-worker", false, "run background update download worker only")
	fs.BoolVar(&workerOnce, "once", false, "worker: run one pass over mirrors and exit")
	fs.IntVar(&parentPID, "parent-pid", 0, "worker: exit when parent process disappears")
	fs.BoolVar(&applyHelper, "apply-helper", false, "run update apply helper and exit")
	fs.StringVar(&applyPackage, "apply-package", "", "apply helper: path to verified update zip")
	fs.StringVar(&applyVersion, "apply-version", "", "apply helper: target version")
	fs.IntVar(&applyWaitPID, "apply-wait-pid", 0, "apply helper: wait for launcher PID to exit")
	fs.IntVar(&applyBootTimeoutSec, "apply-boot-timeout", int(apply.DefaultBootTimeout/time.Second), "apply helper: boot-ok wait timeout in seconds")

	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "unexpected arguments: %s\n", strings.Join(fs.Args(), " "))
		return 2
	}
	if timeoutSec <= 0 {
		fmt.Fprintln(stderr, "--timeout must be positive")
		return 2
	}
	if parentPID < 0 {
		fmt.Fprintln(stderr, "--parent-pid must be non-negative")
		return 2
	}
	if applyWaitPID < 0 {
		fmt.Fprintln(stderr, "--apply-wait-pid must be non-negative")
		return 2
	}
	if applyBootTimeoutSec <= 0 {
		fmt.Fprintln(stderr, "--apply-boot-timeout must be positive")
		return 2
	}
	if pubkeyPath != "" && !allowLocal {
		fmt.Fprintln(stderr, "--pubkey requires --allow-local")
		return 2
	}
	if checkMode && noUpdate {
		fmt.Fprintln(stderr, "--check and --no-update cannot be used together")
		return 2
	}
	if updateWorker && (checkMode || noUpdate || dryRun || applyHelper) {
		fmt.Fprintln(stderr, "--update-worker cannot be combined with --check, --no-update, --dry-run, or --apply-helper")
		return 2
	}
	if !updateWorker && workerOnce {
		fmt.Fprintln(stderr, "--once requires --update-worker")
		return 2
	}
	if !updateWorker && parentPID != 0 {
		fmt.Fprintln(stderr, "--parent-pid requires --update-worker")
		return 2
	}
	if applyHelper {
		if checkMode || noUpdate || dryRun {
			fmt.Fprintln(stderr, "--apply-helper cannot be combined with --check, --no-update, or --dry-run")
			return 2
		}
		if strings.TrimSpace(applyPackage) == "" || strings.TrimSpace(applyVersion) == "" {
			fmt.Fprintln(stderr, "--apply-helper requires --apply-package and --apply-version")
			return 2
		}
	} else if applyPackage != "" || applyVersion != "" || applyWaitPID != 0 {
		fmt.Fprintln(stderr, "--apply-package, --apply-version, and --apply-wait-pid require --apply-helper")
		return 2
	}

	if appPath == "" {
		appPath = filepath.Join(launcherDir, "好想跑.exe")
	}
	appDir := filepath.Dir(appPath)
	if dataDir == "" {
		dataDir = filepath.Join(appDir, "data")
	}
	if logPath == "" {
		logPath = filepath.Join(dataDir, "updates", "launcher.log")
	}

	if applyHelper {
		lock, ok, err := runlock.Acquire(runlock.LauncherName, 30*time.Second)
		if err != nil {
			fmt.Fprintf(stderr, "fatal: acquire launcher lock: %v\n", err)
			return 1
		}
		if !ok {
			fmt.Fprintln(stderr, "fatal: another launcher instance is still running")
			return 1
		}
		defer lock.Release()
		return runApplyHelper(stderr, appDir, dataDir, logPath, applyPackage, applyVersion, applyWaitPID, applyBootTimeoutSec, deps.runApply)
	}

	logger := logx.Open(logPath)
	defer logger.Close()

	if !updateWorker && !checkMode && !dryRun {
		lock, ok, err := runlock.TryAcquire(runlock.LauncherName)
		if err != nil {
			logger.Warn("launcher lock unavailable (continuing): %s", worker.SanitizeErrorString(err.Error()))
		} else if !ok {
			logger.Info("another launcher instance is running; exiting")
			return 0
		} else {
			defer lock.Release()
		}
	}

	// --check/--dry-run are read-only: never move app directories or state.
	if !updateWorker && !checkMode && !dryRun {
		if err := apply.Recover(appDir, dataDir, logger); err != nil {
			logger.Warn("apply recovery failed: %s", worker.SanitizeErrorString(err.Error()))
		}
	}

	var manifestURLs []string
	if strings.TrimSpace(manifestRaw) != "" {
		for _, part := range strings.Split(manifestRaw, ",") {
			if u := strings.TrimSpace(part); u != "" {
				manifestURLs = append(manifestURLs, u)
			}
		}
		if len(manifestURLs) == 0 {
			fmt.Fprintln(stderr, "--manifest cannot be empty")
			return 2
		}
	} else {
		manifestURLs = manifest.DefaultManifestURLs
	}

	keys := manifest.PUBLIC_KEYS
	var keyLoadErr error
	if pubkeyPath != "" {
		keys, keyLoadErr = manifest.LoadPublicKeyFile(pubkeyPath)
		if keyLoadErr != nil {
			logger.Warn("failed to load public key file: %v", keyLoadErr)
		}
	}

	timeout := time.Duration(timeoutSec) * time.Second

	if updateWorker {
		if keyLoadErr != nil {
			logger.Error("worker cannot load public key: %v", keyLoadErr)
			fmt.Fprintf(stderr, "fatal: %v\n", keyLoadErr)
			return 1
		}
		if len(keys) == 0 {
			logger.Error("signature not configured")
			fmt.Fprintf(stderr, "fatal: %v\n", manifest.ErrSignatureNotConfigured)
			return 1
		}
		err := worker.Run(context.Background(), worker.Config{
			DataDir:      dataDir,
			ManifestURLs: manifestURLs,
			Keys:         keys,
			AllowLocal:   allowLocal,
			Timeout:      timeout,
			Once:         workerOnce,
			ParentPID:    parentPID,
			Logger:       logger,
		})
		if err != nil {
			logger.Error("update worker failed: %s", worker.SanitizeErrorString(err.Error()))
			return 1
		}
		return 0
	}

	if noUpdate {
		logger.Info("update check skipped (--no-update)")
		_, code := launchApp(deps.launch, logger, stderr, appPath, dryRun)
		return code
	}

	if checkMode {
		return runCheck(stdout, logger, appDir, manifestURLs, keys, keyLoadErr, allowLocal, timeout)
	}

	if !dryRun && maybeApply(logger, appPath, appDir, dataDir, logPath, applyBootTimeoutSec, deps) {
		return 0
	}

	appPID, code := launchApp(deps.launch, logger, stderr, appPath, dryRun)
	if code != 0 {
		return code
	}

	info, err := version.ReadBuildInfo(appDir)
	if err != nil {
		if errors.Is(err, version.ErrBuildInfoMissing) {
			logger.Info("build_info.json missing; skipping update check")
		} else {
			logger.Warn("build_info.json unreadable (%v); skipping update check", err)
		}
		return 0
	}

	if keyLoadErr != nil {
		logger.Warn("manifest check skipped (fail-open): %v", keyLoadErr)
		return 0
	}

	if len(keys) == 0 {
		logger.Info("signature not configured")
		return 0
	}

	res, err := manifest.FetchAndVerify(context.Background(), manifestURLs, keys, allowLocal, timeout)
	if err != nil {
		logger.Warn("manifest check failed (fail-open): %v", err)
		return 0
	}

	state := compareState(res.Manifest.Version, info.Version)
	logger.Info(
		"manifest verified (key_id=%s source=%s current=%s latest=%s state=%s)",
		res.Signature.KeyID, res.Source, info.Version, res.Manifest.Version, state,
	)

	if state == "available" {
		if dryRun {
			logger.Info("dry-run: would spawn update worker")
			return 0
		}
		spawnOpts := WorkerSpawnOptions{
			AppPath:      absOrRaw(appPath),
			DataDir:      absOrRaw(dataDir),
			ManifestURLs: manifestURLs,
			TimeoutSec:   timeoutSec,
			LogPath:      absOrRaw(logPath),
			ParentPID:    appPID,
			AllowLocal:   allowLocal,
			PubkeyPath:   absOrRaw(pubkeyPath),
		}
		wpid, err := deps.spawnWorker(spawnOpts)
		if err != nil {
			logger.Warn("failed to spawn update worker (fail-open): %s", worker.SanitizeErrorString(err.Error()))
		} else {
			logger.Info("spawned update worker (pid=%d)", wpid)
		}
	}

	return 0
}

func runCheck(
	stdout io.Writer,
	logger *logx.Logger,
	appDir string,
	manifestURLs []string,
	keys map[string]ed25519.PublicKey,
	keyLoadErr error,
	allowLocal bool,
	timeout time.Duration,
) int {
	current := ""
	if info, err := version.ReadBuildInfo(appDir); err == nil {
		current = info.Version
	} else if errors.Is(err, version.ErrBuildInfoMissing) {
		logger.Info("build_info.json missing during --check")
	} else {
		logger.Warn("build_info.json unreadable during --check: %v", err)
	}

	if keyLoadErr != nil {
		summary := CheckSummary{
			State:    "failed",
			Verified: false,
			Current:  current,
			Message:  keyLoadErr.Error(),
			Error:    keyLoadErr.Error(),
		}
		writeJSON(stdout, summary)
		return 1
	}

	if len(keys) == 0 {
		logger.Info("signature not configured")
		summary := CheckSummary{
			State:    "failed",
			Verified: false,
			Current:  current,
			Message:  manifest.ErrSignatureNotConfigured.Error(),
			Error:    manifest.ErrSignatureNotConfigured.Error(),
		}
		writeJSON(stdout, summary)
		return 1
	}

	res, err := manifest.FetchAndVerify(context.Background(), manifestURLs, keys, allowLocal, timeout)
	if err != nil {
		logger.Warn("manifest check failed: %v", err)
		summary := CheckSummary{
			State:    "failed",
			Verified: false,
			Current:  current,
			Message:  err.Error(),
			Error:    err.Error(),
		}
		writeJSON(stdout, summary)
		return 1
	}

	state := "verified"
	if current != "" {
		state = compareState(res.Manifest.Version, current)
	}
	required := false
	if res.Manifest.MinVersion != "" && current != "" {
		required = version.IsNewer(res.Manifest.MinVersion, current)
	}

	msg := fmt.Sprintf("manifest verified: %s (%s)", res.Manifest.Version, state)
	logger.Info("check succeeded (key_id=%s source=%s current=%s latest=%s state=%s)",
		res.Signature.KeyID, res.Source, current, res.Manifest.Version, state)

	summary := CheckSummary{
		State:      state,
		Verified:   true,
		Current:    current,
		Latest:     res.Manifest.Version,
		MinVersion: res.Manifest.MinVersion,
		Required:   required,
		Source:     res.Source,
		KeyID:      res.Signature.KeyID,
		Notes:      res.Manifest.Notes,
		Message:    msg,
	}
	writeJSON(stdout, summary)
	return 0
}

func compareState(latest, current string) string {
	if version.IsNewer(latest, current) {
		return "available"
	}
	if version.Same(latest, current) {
		return "current"
	}
	if _, ok := version.Compare(latest, current); ok {
		return "ahead"
	}
	return "unknown"
}

func launchApp(
	launchFn func(app.LaunchOptions) (int, error),
	logger *logx.Logger,
	stderr io.Writer,
	appPath string,
	dryRun bool,
) (int, int) {
	pid, err := launchFn(app.LaunchOptions{AppPath: appPath, DryRun: dryRun})
	if err != nil {
		logger.Error("failed to launch %s: %v", appPath, err)
		fmt.Fprintf(stderr, "fatal: %v\n", err)
		return 0, 1
	}
	if dryRun {
		logger.Info("dry-run: would launch %s", appPath)
	} else {
		logger.Info("launched %s (pid=%d)", appPath, pid)
	}
	return pid, 0
}

func buildWorkerArgs(opts WorkerSpawnOptions) []string {
	args := []string{"--update-worker"}
	if opts.AppPath != "" {
		args = append(args, "--app", opts.AppPath)
	}
	if opts.DataDir != "" {
		args = append(args, "--data", opts.DataDir)
	}
	if len(opts.ManifestURLs) > 0 {
		args = append(args, "--manifest", strings.Join(opts.ManifestURLs, ","))
	}
	if opts.TimeoutSec > 0 {
		args = append(args, "--timeout", strconv.Itoa(opts.TimeoutSec))
	}
	if opts.LogPath != "" {
		args = append(args, "--log", opts.LogPath)
	}
	if opts.ParentPID > 0 {
		args = append(args, "--parent-pid", strconv.Itoa(opts.ParentPID))
	}
	if opts.AllowLocal {
		args = append(args, "--allow-local")
		if opts.PubkeyPath != "" {
			args = append(args, "--pubkey", opts.PubkeyPath)
		}
	}
	return args
}

func buildWorkerCommand(opts WorkerSpawnOptions) (*exec.Cmd, error) {
	exe := opts.ExePath
	if exe == "" {
		var err error
		exe, err = os.Executable()
		if err != nil {
			return nil, fmt.Errorf("resolve launcher executable: %w", err)
		}
	}
	cmd := exec.Command(exe, buildWorkerArgs(opts)...)
	if opts.AppPath != "" {
		cmd.Dir = filepath.Dir(opts.AppPath)
	} else {
		cmd.Dir = filepath.Dir(exe)
	}
	cmd.SysProcAttr = worker.DetachedSysProcAttr()
	return cmd, nil
}

func spawnWorker(opts WorkerSpawnOptions) (int, error) {
	cmd, err := buildWorkerCommand(opts)
	if err != nil {
		return 0, err
	}
	if err := cmd.Start(); err != nil {
		return 0, fmt.Errorf("start update worker: %w", err)
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	return pid, nil
}

func runApplyHelper(
	stderr io.Writer,
	appDir string,
	dataDir string,
	logPath string,
	packagePath string,
	targetVersion string,
	waitPID int,
	bootTimeoutSec int,
	runApplyFn func(apply.Options) error,
) int {
	var logBuf bytes.Buffer
	logger := logx.NewWriter(&logBuf)

	if waitPID > 0 {
		if err := apply.WaitForPIDExit(waitPID, 15*time.Second); err != nil {
			logger.Warn("wait for launcher exit: %s", worker.SanitizeErrorString(err.Error()))
		}
	}

	err := runApplyFn(apply.Options{
		AppDir:      appDir,
		DataDir:     dataDir,
		PackagePath: packagePath,
		Version:     targetVersion,
		BootTimeout: time.Duration(bootTimeoutSec) * time.Second,
		Logger:      logger,
	})
	appendLogBytes(logPath, logBuf.Bytes())

	if err != nil {
		fmt.Fprintf(stderr, "fatal: %v\n", err)
		return 1
	}
	return 0
}

func appendLogBytes(logPath string, data []byte) {
	if strings.TrimSpace(logPath) == "" || len(data) == 0 {
		return
	}
	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil {
		return
	}
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	_, _ = f.Write(data)
	_ = f.Close()
}

func maybeApply(
	logger *logx.Logger,
	appPath string,
	appDir string,
	dataDir string,
	logPath string,
	bootTimeoutSec int,
	deps launcherDeps,
) bool {
	info, err := version.ReadBuildInfo(appDir)
	if err != nil {
		return false
	}
	st, ok, err := worker.LoadState(worker.StatePath(dataDir))
	if err != nil || !ok {
		return false
	}
	if st.Phase != worker.PhaseReady {
		return false
	}
	if strings.TrimSpace(st.Version) == "" || st.Size <= 0 || len(strings.TrimSpace(st.SHA256)) != 64 {
		logger.Warn("download state is missing version/sha256/size; skipping apply")
		return false
	}
	pkgPath := strings.TrimSpace(st.Package)
	if pkgPath == "" {
		return false
	}
	pkgInfo, err := os.Stat(pkgPath)
	if err != nil || !pkgInfo.Mode().IsRegular() {
		return false
	}
	if !version.IsNewer(st.Version, info.Version) {
		return false
	}

	opts := ApplySpawnOptions{
		AppPath:        absOrRaw(appPath),
		DataDir:        absOrRaw(dataDir),
		PackagePath:    absOrRaw(pkgPath),
		Version:        st.Version,
		WaitPID:        deps.currentPID(),
		BootTimeoutSec: bootTimeoutSec,
		LogPath:        absOrRaw(logPath),
	}
	helperPID, err := deps.spawnApply(opts)
	if err != nil {
		logger.Warn("failed to spawn apply helper (fail-open): %s", worker.SanitizeErrorString(err.Error()))
		return false
	}
	logger.Info("spawned apply helper (pid=%d version=%s)", helperPID, st.Version)
	return true
}

func buildApplyArgs(opts ApplySpawnOptions) []string {
	bootTimeout := opts.BootTimeoutSec
	if bootTimeout <= 0 {
		bootTimeout = int(apply.DefaultBootTimeout / time.Second)
	}
	args := []string{
		"--apply-helper",
		"--apply-package", opts.PackagePath,
		"--apply-version", opts.Version,
		"--apply-wait-pid", strconv.Itoa(opts.WaitPID),
		"--apply-boot-timeout", strconv.Itoa(bootTimeout),
	}
	if opts.AppPath != "" {
		args = append(args, "--app", opts.AppPath)
	}
	if opts.DataDir != "" {
		args = append(args, "--data", opts.DataDir)
	}
	if opts.LogPath != "" {
		args = append(args, "--log", opts.LogPath)
	}
	return args
}

func buildApplyCommand(tempExe string, opts ApplySpawnOptions) *exec.Cmd {
	cmd := exec.Command(tempExe, buildApplyArgs(opts)...)
	cmd.Dir = os.TempDir()
	cmd.SysProcAttr = worker.DetachedSysProcAttr()
	return cmd
}

func spawnApplyHelper(opts ApplySpawnOptions) (int, error) {
	srcExe := opts.ExePath
	if srcExe == "" {
		var err error
		srcExe, err = os.Executable()
		if err != nil {
			return 0, fmt.Errorf("resolve launcher executable: %w", err)
		}
	}
	waitPID := opts.WaitPID
	if waitPID <= 0 {
		waitPID = os.Getpid()
	}
	tempExe := filepath.Join(os.TempDir(), fmt.Sprintf("hxp-apply-%d.exe", waitPID))
	if err := copyExecutable(srcExe, tempExe); err != nil {
		return 0, fmt.Errorf("copy launcher to temp: %w", err)
	}
	cmd := buildApplyCommand(tempExe, opts)
	if err := cmd.Start(); err != nil {
		_ = os.Remove(tempExe)
		return 0, fmt.Errorf("start apply helper: %w", err)
	}
	pid := cmd.Process.Pid
	_ = cmd.Process.Release()
	return pid, nil
}

func copyExecutable(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(out, in)
	closeErr := out.Close()
	if copyErr != nil {
		_ = os.Remove(dst)
		return copyErr
	}
	if closeErr != nil {
		_ = os.Remove(dst)
		return closeErr
	}
	return nil
}

func absOrRaw(p string) string {
	if p == "" || filepath.IsAbs(p) {
		return p
	}
	abs, err := filepath.Abs(p)
	if err != nil {
		return p
	}
	return abs
}

func writeJSON(w io.Writer, v any) {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
