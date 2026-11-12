package manifest

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/version"
)

const (
	maxManifestBytes = 4 * 1024 * 1024
	userAgent        = "haoxiangpao-launcher/1"
)

var DefaultManifestURLs = []string{
	"https://raw.githubusercontent.com/Knight-ask-art/KnightSportsWorldTool/main/latest.json",
	"https://cdn.jsdelivr.net/gh/Knight-ask-art/KnightSportsWorldTool@main/latest.json",
}

type Asset struct {
	File    string   `json:"file"`
	URL     string   `json:"url"`
	URLs    []string `json:"urls,omitempty"`
	Size    int64    `json:"size"`
	SHA256  string   `json:"sha256"`
	Format  string   `json:"format"`
	Layout  string   `json:"layout"`
	Entry   string   `json:"entry"`
	Version string   `json:"version"`
}

type Manifest struct {
	Schema     int              `json:"schema"`
	App        string           `json:"app"`
	Name       string           `json:"name"`
	Channel    string           `json:"channel"`
	Version    string           `json:"version"`
	MinVersion string           `json:"min_version"`
	ReleasedAt string           `json:"released_at"`
	Notes      []string         `json:"notes"`
	Assets     map[string]Asset `json:"assets"`
}

type VerifiedResult struct {
	Manifest  Manifest
	Signature SignatureInfo
	Source    string
}

func IsAllowedURL(u *url.URL, allowLocal bool) bool {
	if u == nil {
		return false
	}
	scheme := strings.ToLower(u.Scheme)
	switch scheme {
	case "https":
		return u.Hostname() != ""
	case "http":
		if !allowLocal {
			return false
		}
		host := strings.ToLower(u.Hostname())
		return host == "localhost" || host == "127.0.0.1" || host == "::1"
	case "file":
		return allowLocal
	default:
		return false
	}
}

func ParseAndValidate(raw []byte) (Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return Manifest{}, fmt.Errorf("decode manifest: %w", err)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return Manifest{}, fmt.Errorf("decode manifest: %w", err)
	}
	if schemaRaw, ok := envelope["schema"]; ok {
		var schema int
		if err := json.Unmarshal(schemaRaw, &schema); err != nil {
			return Manifest{}, errors.New("invalid manifest schema")
		}
		m.Schema = schema
	} else {
		m.Schema = 1
	}
	if m.Schema != 1 {
		return Manifest{}, fmt.Errorf("unsupported manifest schema: %d", m.Schema)
	}
	if m.App != "" && m.App != "haoxiangpao" {
		return Manifest{}, fmt.Errorf("manifest app mismatch: %q", m.App)
	}
	m.Version = strings.TrimSpace(m.Version)
	if _, ok := version.Parse(m.Version); !ok {
		return Manifest{}, fmt.Errorf("invalid manifest version: %q", m.Version)
	}
	m.MinVersion = strings.TrimSpace(m.MinVersion)
	return m, nil
}

func Fetch(ctx context.Context, rawURL string, allowLocal bool, timeout time.Duration) ([]byte, error) {
	if timeout <= 0 {
		timeout = 8 * time.Second
	}

	// Plain local filesystem path (only allowed with --allow-local).
	if isPlainLocalPath(rawURL) {
		if !allowLocal {
			return nil, errors.New("local manifest paths require --allow-local")
		}
		return readLocalFile(rawURL)
	}

	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("invalid manifest URL: %w", err)
	}
	if !IsAllowedURL(u, allowLocal) {
		return nil, fmt.Errorf("manifest URL scheme/host not allowed: %s", logx.SanitizeURL(rawURL))
	}

	if strings.EqualFold(u.Scheme, "file") {
		p, err := fileURLToPath(u)
		if err != nil {
			return nil, err
		}
		return readLocalFile(p)
	}

	client := &http.Client{
		Timeout: timeout,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return errors.New("too many redirects")
			}
			if !IsAllowedURL(req.URL, allowLocal) || strings.EqualFold(req.URL.Scheme, "file") {
				return fmt.Errorf("redirect target not allowed: %s", logx.SanitizeURL(req.URL.String()))
			}
			return nil
		},
	}

	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("User-Agent", userAgent)

	resp, err := client.Do(req)
	if err != nil {
		return nil, sanitizeFetchError(err, rawURL)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	data, err := io.ReadAll(io.LimitReader(resp.Body, maxManifestBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	if len(data) > maxManifestBytes {
		return nil, errors.New("manifest exceeds maximum size")
	}
	return data, nil
}

func FetchAndVerify(
	ctx context.Context,
	urls []string,
	keys map[string]ed25519.PublicKey,
	allowLocal bool,
	timeout time.Duration,
) (VerifiedResult, error) {
	if len(keys) == 0 {
		return VerifiedResult{}, ErrSignatureNotConfigured
	}
	if len(urls) == 0 {
		urls = DefaultManifestURLs
	}

	var errs []string
	for _, u := range urls {
		raw, err := Fetch(ctx, u, allowLocal, timeout)
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", logx.SanitizeURL(u), err))
			continue
		}
		sig, err := VerifyManifest(raw, keys)
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", logx.SanitizeURL(u), err))
			continue
		}
		m, err := ParseAndValidate(raw)
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", logx.SanitizeURL(u), err))
			continue
		}
		return VerifiedResult{
			Manifest:  m,
			Signature: sig,
			Source:    logx.SanitizeURL(u),
		}, nil
	}
	if len(errs) == 0 {
		return VerifiedResult{}, errors.New("no manifest URLs configured")
	}
	return VerifiedResult{}, errors.New(strings.Join(errs, "; "))
}

func sanitizeFetchError(err error, rawURL string) error {
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return fmt.Errorf("request %s: %w", logx.SanitizeURL(urlErr.URL), urlErr.Err)
	}
	return fmt.Errorf("request %s: %w", logx.SanitizeURL(rawURL), err)
}

func isPlainLocalPath(raw string) bool {
	if filepath.IsAbs(raw) {
		return true
	}
	lower := strings.ToLower(raw)
	if strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://") || strings.HasPrefix(lower, "file://") {
		return false
	}
	return !strings.Contains(raw, "://")
}

func fileURLToPath(u *url.URL) (string, error) {
	p := u.Path
	if u.Host != "" && !strings.EqualFold(u.Host, "localhost") {
		// UNC path: file://server/share/file -> \\server\share\file
		return `\\` + u.Host + filepath.FromSlash(p), nil
	}
	// On Windows, file:///C:/foo has Path "/C:/foo".
	if len(p) >= 3 && p[0] == '/' && ((p[1] >= 'A' && p[1] <= 'Z') || (p[1] >= 'a' && p[1] <= 'z')) && p[2] == ':' {
		p = p[1:]
	}
	if p == "" {
		return "", errors.New("empty file URL path")
	}
	return filepath.FromSlash(p), nil
}

func readLocalFile(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.Size() > maxManifestBytes {
		return nil, errors.New("manifest exceeds maximum size")
	}
	return os.ReadFile(path)
}
