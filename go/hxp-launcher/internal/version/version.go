package version

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var ErrBuildInfoMissing = errors.New("build_info.json missing")

var versionPattern = regexp.MustCompile(`^\s*v?(\d+(?:\.\d+)*)`)

type BuildInfo struct {
	Version string `json:"version"`
	Commit  string `json:"commit"`
	BuiltAt string `json:"built_at"`
}

func ReadBuildInfo(appDir string) (BuildInfo, error) {
	path := filepath.Join(appDir, "build_info.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return BuildInfo{}, ErrBuildInfoMissing
		}
		return BuildInfo{}, fmt.Errorf("read build_info.json: %w", err)
	}
	var info BuildInfo
	if err := json.Unmarshal(data, &info); err != nil {
		return BuildInfo{}, fmt.Errorf("parse build_info.json: %w", err)
	}
	info.Version = strings.TrimSpace(info.Version)
	if _, ok := Parse(info.Version); !ok {
		return BuildInfo{}, fmt.Errorf("invalid build_info.json version: %q", info.Version)
	}
	return info, nil
}

// Parse matches updater.parse_version: optional leading whitespace and 'v',
// dot-separated integers, ignoring trailing metadata (no prerelease semantics).
func Parse(text string) ([]int, bool) {
	m := versionPattern.FindStringSubmatch(text)
	if len(m) < 2 {
		return nil, false
	}
	rawParts := strings.Split(m[1], ".")
	parts := make([]int, len(rawParts))
	for i, p := range rawParts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return nil, false
		}
		parts[i] = n
	}
	return parts, true
}

func Compare(a, b string) (int, bool) {
	pa, okA := Parse(a)
	pb, okB := Parse(b)
	if !okA || !okB {
		return 0, false
	}
	width := len(pa)
	if len(pb) > width {
		width = len(pb)
	}
	for i := 0; i < width; i++ {
		var va, vb int
		if i < len(pa) {
			va = pa[i]
		}
		if i < len(pb) {
			vb = pb[i]
		}
		if va < vb {
			return -1, true
		}
		if va > vb {
			return 1, true
		}
	}
	return 0, true
}

func Same(left, right string) bool {
	cmp, ok := Compare(left, right)
	return ok && cmp == 0
}

func IsNewer(latest, current string) bool {
	cmp, ok := Compare(latest, current)
	return ok && cmp > 0
}
