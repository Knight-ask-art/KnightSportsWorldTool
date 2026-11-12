package apply

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/worker"
)

// Recover repairs an interrupted apply before the launcher starts the app.
// The helper normally rolls back itself; this covers a crash/power loss
// between the directory renames.  It never deletes the only app directory.
func Recover(appDir, dataDir string, logger *logx.Logger) error {
	if logger == nil {
		logger = logx.NewWriter(nil)
	}
	appDir = filepath.Clean(appDir)
	oldDir := appDir + ".old"
	newDir := appDir + ".new"

	state, hadState, loadErr := worker.LoadState(worker.StatePath(dataDir))
	if loadErr != nil {
		logger.Warn("recover: state unreadable (%s)", worker.SanitizeErrorString(loadErr.Error()))
		hadState = false
	}

	appExists := dirExists(appDir)
	oldExists := dirExists(oldDir)
	newExists := dirExists(newDir)

	if !appExists {
		switch {
		case oldExists:
			if err := os.Rename(oldDir, appDir); err != nil {
				return fmt.Errorf("restore .old: %w", err)
			}
			if hadState {
				state.Phase = worker.PhaseRolledBack
				state.LastError = "recovered interrupted apply (restored .old)"
				if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
					logger.Warn("recover: save state: %s", worker.SanitizeErrorString(err.Error()))
				}
			}
			logger.Info("recover: restored interrupted apply from .old")
			return nil
		case newExists:
			if err := os.Rename(newDir, appDir); err != nil {
				return fmt.Errorf("restore .new: %w", err)
			}
			logger.Info("recover: promoted interrupted .new app directory")
			return nil
		default:
			return nil
		}
	}

	if hadState && state.Phase == worker.PhaseApplying && oldExists {
		// The helper may have died after the swap but before boot-ok.  Keep the
		// user data from the new directory, then restore the known-good old one.
		if dirExists(filepath.Join(appDir, "data")) &&
			!dirExists(filepath.Join(oldDir, "data")) {
			if err := os.Rename(filepath.Join(appDir, "data"),
				filepath.Join(oldDir, "data")); err != nil {
				return fmt.Errorf("preserve data during recovery: %w", err)
			}
		}
		if err := os.RemoveAll(appDir); err != nil {
			return fmt.Errorf("remove interrupted new app: %w", err)
		}
		if err := os.Rename(oldDir, appDir); err != nil {
			return fmt.Errorf("restore .old after interrupted apply: %w", err)
		}
		state.Phase = worker.PhaseRolledBack
		state.LastError = "recovered interrupted apply (restored .old)"
		if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
			logger.Warn("recover: save state: %s", worker.SanitizeErrorString(err.Error()))
		}
		logger.Info("recover: rolled back interrupted apply")
		return nil
	}

	if hadState && state.Phase == worker.PhaseApplied && oldExists {
		if err := os.RemoveAll(oldDir); err != nil {
			logger.Warn("recover: remove applied .old: %s", worker.SanitizeErrorString(err.Error()))
		} else {
			logger.Info("recover: removed .old after completed apply")
		}
	}

	if newExists {
		if err := os.RemoveAll(newDir); err != nil {
			logger.Warn("recover: remove stale .new: %s", worker.SanitizeErrorString(err.Error()))
		}
	}

	// A leftover staging directory is never needed after a completed apply.
	updates := worker.UpdatesDir(dataDir)
	entries, err := os.ReadDir(updates)
	if err == nil {
		for _, entry := range entries {
			if entry.IsDir() && strings.HasPrefix(entry.Name(), "staging-") {
				_ = os.RemoveAll(filepath.Join(updates, entry.Name()))
			}
		}
	}
	return nil
}
