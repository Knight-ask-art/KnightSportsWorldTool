package logx

import (
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type Logger struct {
	mu     sync.Mutex
	file   *os.File
	writer io.Writer
}

func Open(path string) *Logger {
	if strings.TrimSpace(path) == "" {
		return &Logger{writer: io.Discard}
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return &Logger{writer: io.Discard}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return &Logger{writer: io.Discard}
	}
	return &Logger{file: f, writer: f}
}

func NewWriter(w io.Writer) *Logger {
	if w == nil {
		w = io.Discard
	}
	return &Logger{writer: w}
}

func (l *Logger) Close() error {
	if l == nil || l.file == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	err := l.file.Close()
	l.file = nil
	l.writer = io.Discard
	return err
}

func (l *Logger) Info(format string, args ...any) {
	l.write("INFO", format, args...)
}

func (l *Logger) Warn(format string, args ...any) {
	l.write("WARN", format, args...)
}

func (l *Logger) Error(format string, args ...any) {
	l.write("ERROR", format, args...)
}

func (l *Logger) write(level, format string, args ...any) {
	if l == nil || l.writer == nil {
		return
	}
	msg := fmt.Sprintf(format, args...)
	msg = strings.ReplaceAll(msg, "\r", " ")
	msg = strings.ReplaceAll(msg, "\n", " ")
	ts := time.Now().UTC().Format(time.RFC3339)
	l.mu.Lock()
	defer l.mu.Unlock()
	_, _ = fmt.Fprintf(l.writer, "%s [%s] %s\n", ts, level, msg)
}

// SanitizeURL strips userinfo and query/fragment so tokens in URLs are never logged.
func SanitizeURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "<invalid-url>"
	}
	u.User = nil
	u.RawQuery = ""
	u.Fragment = ""
	return u.String()
}
