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
// between the directory renames.  It never deletes the only copy of the app
// binaries or of user data.
func Recover(appDir, dataDir string, logger *logx.Logger) error {
	if logger == nil {
		logger = logx.NewWriter(nil)
	}
	appDir = filepath.Clean(appDir)
	oldDir := appDir + ".old"
	newDir := appDir + ".new"
	appDataDir := filepath.Join(appDir, "data")
	oldDataDir := filepath.Join(oldDir, "data")
	newDataDir := filepath.Join(newDir, "data")

	state, hadState, loadErr := worker.LoadState(worker.StatePath(dataDir))
	if loadErr != nil {
		logger.Warn("recover: state unreadable (%s)", worker.SanitizeErrorString(loadErr.Error()))
		hadState = false
	}

	restoreData := func(src, dst string) bool {
		if !dirExists(src) {
			return true
		}
		if dirExists(dst) {
			return false
		}
		if err := os.Rename(src, dst); err == nil {
			return true
		} else {
			logger.Warn("recover: rename data %s -> %s failed: %s", src, dst, worker.SanitizeErrorString(err.Error()))
		}
		if err := defaultCopyDir(src, dst, nil); err != nil {
			logger.Error("recover: copy data %s -> %s failed: %s", src, dst, worker.SanitizeErrorString(err.Error()))
			return false
		}
		return true
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
			oldExists = false
			if hadState {
				state.Phase = worker.PhaseRolledBack
				state.LastError = "recovered interrupted apply (restored .old)"
				if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
					logger.Warn("recover: save state: %s", worker.SanitizeErrorString(err.Error()))
				}
			}
			logger.Info("recover: restored interrupted apply from .old")
		case newExists:
			if err := os.Rename(newDir, appDir); err != nil {
				return fmt.Errorf("restore .new: %w", err)
			}
			newExists = false
			logger.Info("recover: promoted interrupted .new app directory")
		default:
			return nil
		}
		appExists = true
	}

	// Reverse an interrupted swap: appDir holds the unverified new app, .old
	// holds the known-good copy.  Move the new app back to .new first, then put
	// .old in place, then move user data across.  Nothing is deleted until the
	// old app directory exists again.
	if hadState && state.Phase == worker.PhaseApplying && oldExists {
		if dirExists(newDir) {
			if dirExists(newDataDir) && !dirExists(appDataDir) {
				if !restoreData(newDataDir, appDataDir) {
					return fmt.Errorf("recover: cannot rescue data from stale .new")
				}
			}
			if dirExists(newDataDir) {
				return fmt.Errorf("recover: stale .new contains data that cannot be merged safely")
			}
			if err := os.RemoveAll(newDir); err != nil {
				return fmt.Errorf("recover: remove stale .new: %w", err)
			}
		}
		if err := os.Rename(appDir, newDir); err != nil {
			return fmt.Errorf("recover: move new app aside: %w", err)
		}
		if err := os.Rename(oldDir, appDir); err != nil {
			if backErr := os.Rename(newDir, appDir); backErr != nil {
				return fmt.Errorf("recover: restore .old: %w (and put new app back: %v)", err, backErr)
			}
			return fmt.Errorf("recover: restore .old: %w", err)
		}
		if dirExists(newDataDir) {
			if restoreData(newDataDir, appDataDir) {
				_ = os.RemoveAll(newDir)
			} else {
				logger.Warn("recover: kept %s because data could not be moved", newDir)
			}
		} else {
			_ = os.RemoveAll(newDir)
		}
		state.Phase = worker.PhaseRolledBack
		state.LastError = "recovered interrupted apply (restored .old)"
		if err := worker.SaveState(worker.StatePath(dataDir), state); err != nil {
			logger.Warn("recover: save state: %s", worker.SanitizeErrorString(err.Error()))
		}
		logger.Info("recover: rolled back interrupted apply")
		return nil
	}

	// A crash can leave the only user-data copy inside .new (moved there
	// before the directory swap).  Rescue it before any cleanup.
	if newExists {
		if dirExists(newDataDir) && !dirExists(appDataDir) {
			if !restoreData(newDataDir, appDataDir) {
				logger.Warn("recover: keeping %s; user data is still inside", newDir)
				return nil
			}
		}
		if dirExists(newDataDir) {
			logger.Warn("recover: keeping %s because it still contains data", newDir)
		} else if err := os.RemoveAll(newDir); err != nil {
			logger.Warn("recover: remove stale .new: %s", worker.SanitizeErrorString(err.Error()))
		}
	}

	if hadState && state.Phase == worker.PhaseApplied && oldExists {
		if err := os.RemoveAll(oldDir); err != nil {
			logger.Warn("recover: remove applied .old: %s", worker.SanitizeErrorString(err.Error()))
		} else {
			logger.Info("recover: removed .old after completed apply")
		}
	}

	// Anything still holding user data is left for the next startup; only
	// data-free staging directories are removed.
	if !dirExists(appDataDir) && dirExists(oldDataDir) {
		logger.Warn("recover: %s still contains data; keeping it", oldDir)
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
