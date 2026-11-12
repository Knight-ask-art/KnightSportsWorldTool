package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"haoxiangpao/hxp-launcher/internal/logx"
	"haoxiangpao/hxp-launcher/internal/manifest"
)

const (
	MaxPackageBytes int64 = 400 * 1024 * 1024
	workerUserAgent       = "haoxiangpao-launcher/1"
)

var zipMagic = []byte{'P', 'K', 0x03, 0x04}

type ResolvedAsset struct {
	Version  string
	File     string
	URLs     []string
	URLsHash string
	Size     int64
	SHA256   string
}

func ResolveAsset(m manifest.Manifest, allowLocal bool) (ResolvedAsset, error) {
	asset, ok := m.Assets["windows-x64"]
	if !ok {
		return ResolvedAsset{}, errors.New("manifest missing assets.windows-x64")
	}
	if asset.Format != "" && !strings.EqualFold(strings.TrimSpace(asset.Format), "zip") {
		return ResolvedAsset{}, fmt.Errorf("unsupported windows-x64 format: %q", asset.Format)
	}
	file := strings.TrimSpace(asset.File)
	if file == "" || file == "." || file == ".." || filepath.Base(file) != file || strings.ContainsAny(file, `/\`) {
		return ResolvedAsset{}, fmt.Errorf("invalid asset file name: %q", asset.File)
	}
	if asset.Size <= 0 {
		return ResolvedAsset{}, errors.New("asset size must be positive")
	}
	if asset.Size > MaxPackageBytes {
		return ResolvedAsset{}, fmt.Errorf("asset size %d exceeds max %d bytes", asset.Size, MaxPackageBytes)
	}
	sha := strings.ToLower(strings.TrimSpace(asset.SHA256))
	if len(sha) != 64 {
		return ResolvedAsset{}, errors.New("invalid asset sha256 length")
	}
	if _, err := hex.DecodeString(sha); err != nil {
		return ResolvedAsset{}, errors.New("invalid asset sha256 hex")
	}

	rawCandidates := make([]string, 0, 1+len(asset.URLs))
	if u := strings.TrimSpace(asset.URL); u != "" {
		rawCandidates = append(rawCandidates, u)
	}
	for _, u := range asset.URLs {
		if trimmed := strings.TrimSpace(u); trimmed != "" {
			rawCandidates = append(rawCandidates, trimmed)
		}
	}
	if len(rawCandidates) == 0 {
		return ResolvedAsset{}, errors.New("asset missing download url")
	}

	seen := make(map[string]struct{}, len(rawCandidates))
	urls := make([]string, 0, len(rawCandidates))
	for _, raw := range rawCandidates {
		if _, dup := seen[raw]; dup {
			continue
		}
		parsed, err := url.Parse(raw)
		if err != nil {
			return ResolvedAsset{}, fmt.Errorf("invalid asset url: %s", logx.SanitizeURL(raw))
		}
		if !manifest.IsAllowedURL(parsed, allowLocal) || strings.EqualFold(parsed.Scheme, "file") {
			return ResolvedAsset{}, fmt.Errorf("asset url not allowed: %s", logx.SanitizeURL(raw))
		}
		seen[raw] = struct{}{}
		urls = append(urls, raw)
	}

	return ResolvedAsset{
		Version:  m.Version,
		File:     file,
		URLs:     urls,
		URLsHash: ComputeURLsHash(urls),
		Size:     asset.Size,
		SHA256:   sha,
	}, nil
}

func mirrorOrder(n, lastGood, nextIdx int) []int {
	if n <= 0 {
		return nil
	}
	seen := make([]bool, n)
	order := make([]int, 0, n)
	add := func(i int) {
		if i >= 0 && i < n && !seen[i] {
			seen[i] = true
			order = append(order, i)
		}
	}
	add(lastGood)
	add(0)
	if nextIdx < 0 || nextIdx >= n {
		nextIdx = 0
	}
	for k := 0; k < n; k++ {
		add((nextIdx + k) % n)
	}
	return order
}

func VerifyPackageFile(path string, expectedSize int64, expectedSHA string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return errors.New("package is not a regular file")
	}
	if info.Size() != expectedSize {
		return fmt.Errorf("size mismatch: got %d, want %d", info.Size(), expectedSize)
	}
	if info.Size() > MaxPackageBytes {
		return fmt.Errorf("size %d exceeds max %d", info.Size(), MaxPackageBytes)
	}

	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	var magic [4]byte
	if _, err := io.ReadFull(f, magic[:]); err != nil {
		return fmt.Errorf("read zip magic: %w", err)
	}
	if !bytes.Equal(magic[:], zipMagic) {
		return errors.New("invalid zip magic (expected PK\\x03\\x04)")
	}

	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return fmt.Errorf("hash package: %w", err)
	}
	gotSHA := hex.EncodeToString(h.Sum(nil))
	if !strings.EqualFold(gotSHA, expectedSHA) {
		return fmt.Errorf("sha256 mismatch: got %s, want %s", gotSHA, expectedSHA)
	}
	return nil
}

func downloadFromMirror(
	ctx context.Context,
	mirrorURL string,
	partPath string,
	packagePath string,
	target ResolvedAsset,
	allowLocal bool,
	timeout time.Duration,
) error {
	if timeout <= 0 {
		timeout = 8 * time.Second
	}

	var partSize int64
	sendRange := false
	if info, err := os.Stat(partPath); err == nil && info.Mode().IsRegular() {
		sz := info.Size()
		switch {
		case sz > target.Size || sz > MaxPackageBytes:
			_ = os.Remove(partPath)
		case sz == target.Size:
			if VerifyPackageFile(partPath, target.Size, target.SHA256) == nil {
				return os.Rename(partPath, packagePath)
			}
			// A full-length .part that failed verification must be overwritten from byte 0.
			sendRange = false
		case sz > 0:
			partSize = sz
			sendRange = true
		}
	}

	reqCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	client := &http.Client{
		Transport: &http.Transport{
			Proxy: http.ProxyFromEnvironment,
			DialContext: (&net.Dialer{
				Timeout:   timeout,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			TLSHandshakeTimeout:   timeout,
			ResponseHeaderTimeout: timeout,
		},
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return errors.New("too many redirects")
			}
			if !manifest.IsAllowedURL(req.URL, allowLocal) || strings.EqualFold(req.URL.Scheme, "file") {
				return fmt.Errorf("redirect target not allowed: %s", logx.SanitizeURL(req.URL.String()))
			}
			return nil
		},
	}

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, mirrorURL, nil)
	if err != nil {
		return fmt.Errorf("build request %s: %w", logx.SanitizeURL(mirrorURL), err)
	}
	req.Header.Set("User-Agent", workerUserAgent)
	if sendRange {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", partSize))
	}

	resp, err := client.Do(req)
	if err != nil {
		return sanitizeMirrorError(err, mirrorURL)
	}
	defer resp.Body.Close()

	var (
		openFlags int
		written   int64
	)
	switch resp.StatusCode {
	case http.StatusPartialContent:
		if !sendRange {
			return errors.New("unexpected HTTP 206 without Range request")
		}
		if err := validateContentRange(resp.Header.Get("Content-Range"), partSize, target.Size); err != nil {
			return err
		}
		if resp.ContentLength > 0 && partSize+resp.ContentLength > target.Size {
			return fmt.Errorf("content length %d exceeds remaining %d bytes", resp.ContentLength, target.Size-partSize)
		}
		openFlags = os.O_CREATE | os.O_WRONLY | os.O_APPEND
		written = partSize
	case http.StatusOK:
		if resp.ContentLength > MaxPackageBytes || (resp.ContentLength > 0 && resp.ContentLength > target.Size) {
			return fmt.Errorf("content length %d exceeds expected %d bytes", resp.ContentLength, target.Size)
		}
		openFlags = os.O_CREATE | os.O_WRONLY | os.O_TRUNC
		written = 0
	default:
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	f, err := os.OpenFile(partPath, openFlags, 0o644)
	if err != nil {
		return fmt.Errorf("open part file: %w", err)
	}

	maxRemaining := target.Size - written
	if maxRemaining < 0 {
		_ = f.Close()
		return errors.New("part file exceeds target size")
	}

	idleReader := &idleTimeoutReader{
		r:       io.LimitReader(resp.Body, maxRemaining+1),
		timeout: timeout,
		cancel:  cancel,
	}
	buf := make([]byte, 32*1024)
	var copyErr error
	for {
		n, readErr := idleReader.Read(buf)
		if n > 0 {
			if written+int64(n) > target.Size {
				copyErr = fmt.Errorf("download exceeded expected size %d", target.Size)
				break
			}
			if _, wErr := f.Write(buf[:n]); wErr != nil {
				copyErr = fmt.Errorf("write part file: %w", wErr)
				break
			}
			written += int64(n)
		}
		if readErr != nil {
			if !errors.Is(readErr, io.EOF) {
				copyErr = sanitizeMirrorError(readErr, mirrorURL)
			}
			break
		}
	}
	if closeErr := f.Close(); closeErr != nil && copyErr == nil {
		copyErr = closeErr
	}
	if copyErr != nil {
		return copyErr
	}
	if written != target.Size {
		return fmt.Errorf("incomplete download: got %d of %d bytes", written, target.Size)
	}

	if err := VerifyPackageFile(partPath, target.Size, target.SHA256); err != nil {
		return err
	}
	if err := os.Rename(partPath, packagePath); err != nil {
		return fmt.Errorf("rename package: %w", err)
	}
	return nil
}

func validateContentRange(header string, expectedStart, expectedTotal int64) error {
	header = strings.TrimSpace(header)
	if header == "" {
		return nil
	}
	unit, rest, ok := strings.Cut(header, " ")
	if !ok || !strings.EqualFold(unit, "bytes") {
		return fmt.Errorf("invalid Content-Range unit: %q", header)
	}
	rangePart, totalPart, ok := strings.Cut(strings.TrimSpace(rest), "/")
	if !ok {
		return fmt.Errorf("invalid Content-Range: %q", header)
	}
	startStr, endStr, ok := strings.Cut(rangePart, "-")
	if !ok {
		return fmt.Errorf("invalid Content-Range span: %q", header)
	}
	start, err := strconv.ParseInt(startStr, 10, 64)
	if err != nil {
		return fmt.Errorf("invalid Content-Range start: %q", header)
	}
	end, err := strconv.ParseInt(endStr, 10, 64)
	if err != nil {
		return fmt.Errorf("invalid Content-Range end: %q", header)
	}
	if start != expectedStart || end < start || end >= expectedTotal {
		return fmt.Errorf("unexpected Content-Range %d-%d (expected start %d, size %d)", start, end, expectedStart, expectedTotal)
	}
	if totalPart != "*" {
		total, err := strconv.ParseInt(totalPart, 10, 64)
		if err != nil || total != expectedTotal {
			return fmt.Errorf("Content-Range total mismatch: %q", header)
		}
	}
	return nil
}

func sanitizeMirrorError(err error, mirrorURL string) error {
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return fmt.Errorf("mirror %s: %s", logx.SanitizeURL(urlErr.URL), SanitizeErrorString(urlErr.Err.Error()))
	}
	return fmt.Errorf("mirror %s: %s", logx.SanitizeURL(mirrorURL), SanitizeErrorString(err.Error()))
}

type idleTimeoutReader struct {
	r       io.Reader
	timeout time.Duration
	cancel  context.CancelFunc
}

func (ir *idleTimeoutReader) Read(p []byte) (int, error) {
	timer := time.AfterFunc(ir.timeout, ir.cancel)
	n, err := ir.r.Read(p)
	timer.Stop()
	return n, err
}
