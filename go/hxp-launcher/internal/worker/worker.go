package worker

import (
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/manifest"
	"haoxiangpao/hxp-launcher/internal/version"
)

var backoffSchedule = []time.Duration{
	15 * time.Second,
	30 * time.Second,
	1 * time.Minute,
	2 * time.Minute,
	5 * time.Minute,
	10 * time.Minute,
	30 * time.Minute,
	1 * time.Hour,
}

var (
	errParentExited = errors.New("parent process exited")
	errQuarantined  = errors.New("update is quarantined after an apply attempt")
)

type Config struct {
	DataDir             string
	ManifestURLs        []string
	Keys                map[string]ed25519.PublicKey
	AllowLocal          bool
	Timeout             time.Duration
	Once                bool
	ParentPID           int
	Logger              *logx.Logger
	ParentCheckInterval time.Duration
	Now                 func() time.Time
	Jitter              func(time.Duration) time.Duration
	IsAlive             func(int) bool
}

func Run(ctx context.Context, cfg Config) error {
	if len(cfg.Keys) == 0 {
		return manifest.ErrSignatureNotConfigured
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 8 * time.Second
	}
	if cfg.ParentCheckInterval <= 0 {
		cfg.ParentCheckInterval = 2 * time.Second
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Jitter == nil {
		cfg.Jitter = defaultJitter
	}
	if cfg.IsAlive == nil {
		cfg.IsAlive = isProcessAlive
	}
	if cfg.Logger == nil {
		cfg.Logger = logx.NewWriter(nil)
	}

	updatesDir := UpdatesDir(cfg.DataDir)
	statePath := StatePath(cfg.DataDir)

	for {
		if cfg.ParentPID > 0 && !cfg.IsAlive(cfg.ParentPID) {
			cfg.Logger.Info("parent process exited (pid=%d); stopping worker", cfg.ParentPID)
			return nil
		}
		if err := ctx.Err(); err != nil {
			return err
		}

		nextRetryAt, ok, err := runPass(ctx, cfg, updatesDir, statePath)
		if ok {
			return nil
		}
		if errors.Is(err, errQuarantined) {
			return nil
		}
		if cfg.Once {
			return err
		}

		if err := sleepUntilRetry(ctx, cfg, nextRetryAt); err != nil {
			if errors.Is(err, errParentExited) {
				cfg.Logger.Info("parent process exited (pid=%d); stopping worker", cfg.ParentPID)
				return nil
			}
			return err
		}
	}
}

func runPass(ctx context.Context, cfg Config, updatesDir, statePath string) (time.Time, bool, error) {
	state, hadState, loadErr := LoadState(statePath)
	if loadErr != nil {
		cfg.Logger.Warn("failed to load worker state (%s); starting fresh", SanitizeErrorString(loadErr.Error()))
	}

	res, err := manifest.FetchAndVerify(ctx, cfg.ManifestURLs, cfg.Keys, cfg.AllowLocal, cfg.Timeout)
	if err != nil {
		return recordFailure(cfg, statePath, state, fmt.Errorf("manifest: %w", err))
	}

	target, err := ResolveAsset(res.Manifest, cfg.AllowLocal)
	if err != nil {
		return recordFailure(cfg, statePath, state, fmt.Errorf("asset: %w", err))
	}

	state = reconcileState(state, hadState, target, updatesDir)

	// A previous apply attempt already failed for this exact artifact.  Do not
	// turn its retained zip back into "ready" on every launch: that creates an
	// apply -> boot-ok failure -> re-download/apply loop.  A new version or a
	// changed sha256 resets the state in reconcileState, so a real fix ships.
	switch state.Phase {
	case PhaseApplying, PhaseApplied, PhaseRolledBack:
		if version.Same(state.Version, target.Version) {
			cfg.Logger.Warn("update %s is in phase %q; not re-promoting the same artifact", target.Version, state.Phase)
			return time.Time{}, false, errQuarantined
		}
	}

	packagePath := filepath.Join(updatesDir, target.File)
	partPath := packagePath + ".part"

	if err := VerifyPackageFile(packagePath, target.Size, target.SHA256); err == nil {
		state.Phase = PhaseReady
		state.Package = packagePath
		state.LastError = ""
		state.NextRetryAt = 0
		state.UpdatedAt = cfg.Now().Unix()
		if saveErr := SaveState(statePath, state); saveErr != nil {
			cfg.Logger.Warn("failed to save ready state: %s", SanitizeErrorString(saveErr.Error()))
		}
		cfg.Logger.Info("update package verified and ready: version=%s file=%s", target.Version, target.File)
		return time.Time{}, true, nil
	}

	if err := os.MkdirAll(updatesDir, 0o755); err != nil {
		return recordFailure(cfg, statePath, state, fmt.Errorf("mkdir updates: %w", err))
	}

	order := mirrorOrder(len(target.URLs), state.LastGoodIndex, state.NextIndex)
	var mirrorErrs []string

	for _, idx := range order {
		if cfg.ParentPID > 0 && !cfg.IsAlive(cfg.ParentPID) {
			return time.Time{}, false, errParentExited
		}
		if err := ctx.Err(); err != nil {
			return time.Time{}, false, err
		}

		mirrorURL := target.URLs[idx]
		safeURL := logx.SanitizeURL(mirrorURL)
		cfg.Logger.Info("downloading update version=%s mirror=%d url=%s", target.Version, idx, safeURL)

		err := downloadFromMirror(ctx, mirrorURL, partPath, packagePath, target, cfg.AllowLocal, cfg.Timeout)
		if err == nil {
			state.LastGoodIndex = idx
			state.NextIndex = idx
			state.Phase = PhaseReady
			state.Package = packagePath
			state.LastError = ""
			state.NextRetryAt = 0
			state.UpdatedAt = cfg.Now().Unix()
			if saveErr := SaveState(statePath, state); saveErr != nil {
				return time.Time{}, false, fmt.Errorf("save ready state: %w", saveErr)
			}
			cfg.Logger.Info("update downloaded and verified: version=%s mirror=%d package=%s", target.Version, idx, packagePath)
			return time.Time{}, true, nil
		}

		sanitized := SanitizeErrorString(err.Error())
		cfg.Logger.Warn("mirror %d (%s) failed: %s", idx, safeURL, sanitized)
		mirrorErrs = append(mirrorErrs, fmt.Sprintf("mirror[%d]: %s", idx, sanitized))
		state.NextIndex = (idx + 1) % len(target.URLs)
		state.LastError = sanitized
		state.UpdatedAt = cfg.Now().Unix()
		if saveErr := SaveState(statePath, state); saveErr != nil {
			cfg.Logger.Warn("failed to persist state after mirror error: %s", SanitizeErrorString(saveErr.Error()))
		}
	}

	combined := errors.New(strings.Join(mirrorErrs, "; "))
	return recordFailure(cfg, statePath, state, combined)
}

func recordFailure(cfg Config, statePath string, state State, err error) (time.Time, bool, error) {
	now := cfg.Now()
	state.Attempts++
	delay := BackoffDelay(state.Attempts, cfg.Jitter)
	retryAt := now.Add(delay)
	if retryAt.Unix() <= now.Unix() {
		retryAt = now.Add(time.Second)
	}

	// A transient manifest/network failure must not erase a completed apply or
	// a quarantine decision.  Only an artifact that is still being downloaded
	// goes back to "downloading"; reconcileState handles real version changes.
	switch state.Phase {
	case PhaseApplying, PhaseApplied, PhaseRolledBack:
		// keep phase/package; the next successful manifest check decides
	default:
		state.Phase = PhaseDownloading
		state.Package = ""
	}
	state.NextRetryAt = retryAt.Unix()
	state.LastError = SanitizeErrorString(err.Error())
	state.UpdatedAt = now.Unix()

	if saveErr := SaveState(statePath, state); saveErr != nil {
		cfg.Logger.Warn("failed to save failure state: %s", SanitizeErrorString(saveErr.Error()))
	}
	cfg.Logger.Warn("update download pass failed (attempts=%d next_retry_in=%s): %s",
		state.Attempts, delay.Round(time.Second), state.LastError)
	return retryAt, false, err
}

func BackoffDelay(attempts int, jitter func(time.Duration) time.Duration) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	idx := attempts - 1
	if idx >= len(backoffSchedule) {
		idx = len(backoffSchedule) - 1
	}
	base := backoffSchedule[idx]
	if jitter == nil {
		jitter = defaultJitter
	}
	d := jitter(base)
	if d < time.Second {
		d = time.Second
	}
	return d
}

func defaultJitter(base time.Duration) time.Duration {
	factor := 0.8 + 0.4*rand.Float64()
	return time.Duration(float64(base) * factor)
}

func sleepUntilRetry(ctx context.Context, cfg Config, retryAt time.Time) error {
	for {
		if cfg.ParentPID > 0 && !cfg.IsAlive(cfg.ParentPID) {
			return errParentExited
		}
		rem := retryAt.Sub(cfg.Now())
		if rem <= 0 {
			return nil
		}
		step := rem
		if cfg.ParentPID > 0 && step > cfg.ParentCheckInterval {
			step = cfg.ParentCheckInterval
		}
		timer := time.NewTimer(step)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}
