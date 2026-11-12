package logx

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSanitizeURL(t *testing.T) {
	got := SanitizeURL("https://user:secret@example.com/path/latest.json?token=abc#frag")
	want := "https://example.com/path/latest.json"
	if got != want {
		t.Fatalf("SanitizeURL=%q, want %q", got, want)
	}
}

func TestOpenAndWrite(t *testing.T) {
	path := filepath.Join(t.TempDir(), "updates", "launcher.log")
	l := Open(path)
	l.Info("hello\nworld")
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(b)
	if !strings.Contains(text, "[INFO] hello world") {
		t.Fatalf("unexpected log text: %q", text)
	}
}
