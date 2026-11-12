package main

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/app"
	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/manifest"
	"haoxiangpao/hxp-launcher/internal/version"
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

func main() {
	exe, err := os.Executable()
	launcherDir := "."
	if err == nil {
		launcherDir = filepath.Dir(exe)
	}
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, launcherDir))
}

func run(args []string, stdout, stderr io.Writer, launcherDir string) int {
	var (
		appPath     string
		dataDir     string
		manifestRaw string
		checkMode   bool
		noUpdate    bool
		dryRun      bool
		allowLocal  bool
		pubkeyPath  string
		timeoutSec  int
		logPath     string
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
	if pubkeyPath != "" && !allowLocal {
		fmt.Fprintln(stderr, "--pubkey requires --allow-local")
		return 2
	}
	if checkMode && noUpdate {
		fmt.Fprintln(stderr, "--check and --no-update cannot be used together")
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

	logger := logx.Open(logPath)
	defer logger.Close()

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

	if noUpdate {
		logger.Info("update check skipped (--no-update)")
		return launchApp(logger, stderr, appPath, dryRun)
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

	if checkMode {
		return runCheck(stdout, logger, appDir, manifestURLs, keys, keyLoadErr, allowLocal, timeout)
	}

	info, err := version.ReadBuildInfo(appDir)
	if err != nil {
		if errors.Is(err, version.ErrBuildInfoMissing) {
			logger.Info("build_info.json missing; launching without update check")
		} else {
			logger.Warn("build_info.json unreadable (%v); launching without update check", err)
		}
		return launchApp(logger, stderr, appPath, dryRun)
	}

	if keyLoadErr != nil {
		logger.Warn("manifest check skipped (fail-open): %v", keyLoadErr)
		return launchApp(logger, stderr, appPath, dryRun)
	}

	if len(keys) == 0 {
		logger.Info("signature not configured")
		return launchApp(logger, stderr, appPath, dryRun)
	}

	res, err := manifest.FetchAndVerify(context.Background(), manifestURLs, keys, allowLocal, timeout)
	if err != nil {
		logger.Warn("manifest check failed (fail-open): %v", err)
		return launchApp(logger, stderr, appPath, dryRun)
	}

	state := compareState(res.Manifest.Version, info.Version)
	logger.Info(
		"manifest verified (key_id=%s source=%s current=%s latest=%s state=%s)",
		res.Signature.KeyID, res.Source, info.Version, res.Manifest.Version, state,
	)

	return launchApp(logger, stderr, appPath, dryRun)
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

func launchApp(logger *logx.Logger, stderr io.Writer, appPath string, dryRun bool) int {
	pid, err := app.Launch(app.LaunchOptions{AppPath: appPath, DryRun: dryRun})
	if err != nil {
		logger.Error("failed to launch %s: %v", appPath, err)
		fmt.Fprintf(stderr, "fatal: %v\n", err)
		return 1
	}
	if dryRun {
		logger.Info("dry-run: would launch %s", appPath)
	} else {
		logger.Info("launched %s (pid=%d)", appPath, pid)
	}
	return 0
}

func writeJSON(w io.Writer, v any) {
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
