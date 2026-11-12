package worker

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"haoxiangpao/hxp-launcher/internal/logx"
)

const (
	StateSchema      = 1
	PhaseDownloading = "downloading"
	PhaseReady       = "ready"
)

type State struct {
	Schema        int    `json:"schema"`
	Version       string `json:"version"`
	File          string `json:"file"`
	SHA256        string `json:"sha256"`
	Size          int64  `json:"size"`
	URLsHash      string `json:"urls_hash"`
	LastGoodIndex int    `json:"last_good_index"`
	NextIndex     int    `json:"next_index"`
	Attempts      int    `json:"attempts"`
	NextRetryAt   int64  `json:"next_retry_at"`
	Phase         string `json:"phase"`
	Package       string `json:"package"`
	LastError     string `json:"last_error"`
	UpdatedAt     int64  `json:"updated_at"`
}

func UpdatesDir(dataDir string) string {
	return filepath.Join(dataDir, "updates")
}

func StatePath(dataDir string) string {
	return filepath.Join(UpdatesDir(dataDir), "download.state.json")
}

func DefaultState() State {
	return State{
		Schema: StateSchema,
		Phase:  PhaseDownloading,
	}
}

func LoadState(path string) (State, bool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return DefaultState(), false, nil
		}
		return DefaultState(), false, err
	}
	var s State
	if err := json.Unmarshal(raw, &s); err != nil {
		return DefaultState(), false, fmt.Errorf("decode state: %w", err)
	}
	if s.Schema != StateSchema {
		return DefaultState(), false, fmt.Errorf("unsupported state schema: %d", s.Schema)
	}
	if s.Phase == "" {
		s.Phase = PhaseDownloading
	}
	return s, true, nil
}

func SaveState(path string, s State) error {
	s.Schema = StateSchema
	if s.Phase == "" {
		s.Phase = PhaseDownloading
	}
	s.LastError = SanitizeErrorString(s.LastError)

	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir state dir: %w", err)
	}

	tmpPath := path + ".tmp"
	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return fmt.Errorf("open temp state: %w", err)
	}

	enc := json.NewEncoder(f)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(s); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("encode state: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("sync state: %w", err)
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("close state: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("rename state: %w", err)
	}
	return nil
}

func ComputeURLsHash(urls []string) string {
	b, _ := json.Marshal(urls)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func reconcileState(s State, hadState bool, target ResolvedAsset, updatesDir string) State {
	artifactChanged := !hadState ||
		s.Version != target.Version ||
		!strings.EqualFold(s.SHA256, target.SHA256) ||
		s.Size != target.Size ||
		s.File != target.File

	if artifactChanged {
		if hadState && s.File != "" && s.File != target.File {
			_ = os.Remove(filepath.Join(updatesDir, s.File+".part"))
		}
		// Never resume a .part file unless state confirms it belongs to the same version/sha/size.
		_ = os.Remove(filepath.Join(updatesDir, target.File+".part"))

		s.Version = target.Version
		s.File = target.File
		s.SHA256 = target.SHA256
		s.Size = target.Size
		s.URLsHash = target.URLsHash
		s.LastGoodIndex = 0
		s.NextIndex = 0
		s.Attempts = 0
		s.NextRetryAt = 0
		s.Phase = PhaseDownloading
		s.Package = ""
		s.LastError = ""
		return s
	}

	if s.URLsHash != target.URLsHash {
		s.URLsHash = target.URLsHash
		s.LastGoodIndex = 0
		s.NextIndex = 0
		s.Attempts = 0
		s.NextRetryAt = 0
	}

	n := len(target.URLs)
	if s.LastGoodIndex < 0 || s.LastGoodIndex >= n {
		s.LastGoodIndex = 0
	}
	if s.NextIndex < 0 || s.NextIndex >= n {
		s.NextIndex = 0
	}
	return s
}

var (
	urlInTextRe   = regexp.MustCompile(`(?i)(?:https?|file)://[^\s"'<>]+`)
	paramSecretRe = regexp.MustCompile(`(?i)\b(token|access_token|auth|secret|password|apikey|api_key)=([^\s&;"']+)`)
)

func SanitizeErrorString(msg string) string {
	if msg == "" {
		return ""
	}
	cleaned := urlInTextRe.ReplaceAllStringFunc(msg, func(raw string) string {
		trimmed := strings.TrimRight(raw, ".,;:)")
		suffix := raw[len(trimmed):]
		return logx.SanitizeURL(trimmed) + suffix
	})
	cleaned = paramSecretRe.ReplaceAllString(cleaned, "$1=<redacted>")
	cleaned = strings.ReplaceAll(cleaned, "\r", " ")
	cleaned = strings.ReplaceAll(cleaned, "\n", " ")
	return cleaned
}
