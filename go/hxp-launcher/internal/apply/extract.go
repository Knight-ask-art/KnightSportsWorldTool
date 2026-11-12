package apply

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
)

const (
	MaxZipEntries        = 20000
	MaxUncompressedBytes = int64(2) << 30 // 2 GiB
	AppExeName           = "好想跑.exe"
)

// ExtractZip validates and unpacks packagePath into a clean stagingDir.
// It rejects >20k entries, >2 GiB uncompressed payload, zip-slip/absolute
// paths, symlinks, any top-level data/ entry, and packages missing 好想跑.exe.
func ExtractZip(packagePath, stagingDir string) error {
	if strings.TrimSpace(packagePath) == "" {
		return errors.New("empty package path")
	}
	if strings.TrimSpace(stagingDir) == "" {
		return errors.New("empty staging directory")
	}

	r, err := zip.OpenReader(packagePath)
	if err != nil {
		return fmt.Errorf("open zip %s: %w", packagePath, err)
	}
	defer r.Close()

	if len(r.File) > MaxZipEntries {
		return fmt.Errorf("zip entry count %d exceeds max %d", len(r.File), MaxZipEntries)
	}

	var totalDeclared uint64
	hasExe := false
	cleanedNames := make([]string, len(r.File))

	for i, f := range r.File {
		if f.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("zip entry %q is a symlink (forbidden)", f.Name)
		}
		clean, err := validateZipEntryPath(f.Name)
		if err != nil {
			return err
		}
		if clean == "." && !f.FileInfo().IsDir() {
			return fmt.Errorf("invalid non-directory entry %q", f.Name)
		}
		cleanedNames[i] = clean

		if !f.FileInfo().IsDir() {
			if f.UncompressedSize64 > uint64(MaxUncompressedBytes)-totalDeclared {
				return fmt.Errorf("zip uncompressed size exceeds max %d bytes", MaxUncompressedBytes)
			}
			totalDeclared += f.UncompressedSize64
			if clean == AppExeName {
				hasExe = true
			}
		}
	}

	if !hasExe {
		return fmt.Errorf("package missing required %s", AppExeName)
	}

	if err := os.RemoveAll(stagingDir); err != nil {
		return fmt.Errorf("clean staging dir: %w", err)
	}
	if err := os.MkdirAll(stagingDir, 0o755); err != nil {
		return fmt.Errorf("create staging dir: %w", err)
	}

	stagingAbs, err := filepath.Abs(stagingDir)
	if err != nil {
		_ = os.RemoveAll(stagingDir)
		return fmt.Errorf("resolve staging dir: %w", err)
	}

	var totalWritten int64
	for i, f := range r.File {
		clean := cleanedNames[i]
		if clean == "." {
			continue
		}
		dest := filepath.Join(stagingAbs, filepath.FromSlash(clean))
		if !isWithinDir(stagingAbs, dest) {
			_ = os.RemoveAll(stagingDir)
			return fmt.Errorf("zip entry escapes staging dir: %q", f.Name)
		}

		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(dest, 0o755); err != nil {
				_ = os.RemoveAll(stagingDir)
				return fmt.Errorf("create dir %s: %w", clean, err)
			}
			continue
		}

		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			_ = os.RemoveAll(stagingDir)
			return fmt.Errorf("create parent dir for %s: %w", clean, err)
		}

		written, err := extractZipFile(f, dest, MaxUncompressedBytes-totalWritten)
		if err != nil {
			_ = os.RemoveAll(stagingDir)
			return fmt.Errorf("extract %s: %w", clean, err)
		}
		totalWritten += written
		if totalWritten > MaxUncompressedBytes {
			_ = os.RemoveAll(stagingDir)
			return fmt.Errorf("zip uncompressed bytes exceed max %d", MaxUncompressedBytes)
		}
	}

	exeInfo, err := os.Stat(filepath.Join(stagingAbs, AppExeName))
	if err != nil || !exeInfo.Mode().IsRegular() {
		_ = os.RemoveAll(stagingDir)
		return fmt.Errorf("extracted staging missing regular %s", AppExeName)
	}
	return nil
}

func extractZipFile(f *zip.File, dest string, remaining int64) (int64, error) {
	rc, err := f.Open()
	if err != nil {
		return 0, err
	}
	defer rc.Close()

	mode := f.Mode().Perm()
	if mode == 0 {
		mode = 0o644
	}
	out, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return 0, err
	}

	limited := io.LimitReader(rc, remaining+1)
	n, copyErr := io.Copy(out, limited)
	closeErr := out.Close()
	if copyErr != nil {
		return n, copyErr
	}
	if closeErr != nil {
		return n, closeErr
	}
	if n > remaining {
		return n, fmt.Errorf("uncompressed data exceeds limit")
	}
	return n, nil
}

func validateZipEntryPath(raw string) (string, error) {
	if raw == "" {
		return "", errors.New("zip contains empty entry path")
	}
	if strings.HasPrefix(raw, "/") || strings.HasPrefix(raw, `\`) || filepath.IsAbs(raw) {
		return "", fmt.Errorf("zip entry uses absolute path: %q", raw)
	}
	if len(raw) >= 2 && raw[1] == ':' {
		return "", fmt.Errorf("zip entry uses drive path: %q", raw)
	}
	norm := strings.ReplaceAll(raw, `\`, "/")
	for _, seg := range strings.Split(strings.TrimSuffix(norm, "/"), "/") {
		if seg == ".." {
			return "", fmt.Errorf("zip entry contains parent traversal: %q", raw)
		}
	}
	clean := path.Clean(norm)
	if clean == ".." || strings.HasPrefix(clean, "../") || strings.HasPrefix(clean, "/") {
		return "", fmt.Errorf("zip entry escapes root: %q", raw)
	}
	top := clean
	if idx := strings.IndexByte(clean, '/'); idx >= 0 {
		top = clean[:idx]
	}
	if strings.EqualFold(top, "data") {
		return "", fmt.Errorf("zip entry under top-level data/ is forbidden: %q", raw)
	}
	return clean, nil
}

func isWithinDir(baseAbs, targetAbs string) bool {
	rel, err := filepath.Rel(baseAbs, targetAbs)
	if err != nil {
		return false
	}
	if rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	return true
}
