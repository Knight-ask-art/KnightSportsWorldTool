package version

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestParse(t *testing.T) {
	cases := []struct {
		in   string
		want []int
		ok   bool
	}{
		{"0.2.2", []int{0, 2, 2}, true},
		{"v0.2.2", []int{0, 2, 2}, true},
		{"v0.1.2+abc", []int{0, 1, 2}, true},
		{" 1.2 ", []int{1, 2}, true},
		{"nightly", nil, false},
		{"", nil, false},
		{"v", nil, false},
		{"99999999999999999999999999999", nil, false},
	}
	for _, tc := range cases {
		got, ok := Parse(tc.in)
		if ok != tc.ok {
			t.Fatalf("Parse(%q) ok=%v, want %v", tc.in, ok, tc.ok)
		}
		if !ok {
			continue
		}
		if len(got) != len(tc.want) {
			t.Fatalf("Parse(%q)=%v, want %v", tc.in, got, tc.want)
		}
		for i := range got {
			if got[i] != tc.want[i] {
				t.Fatalf("Parse(%q)=%v, want %v", tc.in, got, tc.want)
			}
		}
	}
}

func TestCompareAndIsNewer(t *testing.T) {
	if !IsNewer("0.2.0", "0.1.9") {
		t.Fatal("expected 0.2.0 > 0.1.9")
	}
	if !IsNewer("0.2", "0.1.9") {
		t.Fatal("expected 0.2 > 0.1.9")
	}
	if !Same("v0.2.2", "0.2.2") {
		t.Fatal("expected v0.2.2 == 0.2.2")
	}
	if !Same("0.1.2", "0.1.2.0") {
		t.Fatal("expected 0.1.2 == 0.1.2.0")
	}
	if IsNewer("0.2.2", "v0.2.2") {
		t.Fatal("expected equal versions not newer")
	}
	if IsNewer("nightly", "0.2.2") {
		t.Fatal("expected malformed latest -> no update")
	}
	if IsNewer("0.2.2", "nightly") {
		t.Fatal("expected malformed current -> no update")
	}
}

func TestReadBuildInfo(t *testing.T) {
	dir := t.TempDir()
	if _, err := ReadBuildInfo(dir); !errors.Is(err, ErrBuildInfoMissing) {
		t.Fatalf("expected ErrBuildInfoMissing, got %v", err)
	}

	path := filepath.Join(dir, "build_info.json")
	if err := os.WriteFile(path, []byte(`{"version":"0.2.2","commit":"9d7650c","built_at":"2026-11-12T10:00:00Z"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	info, err := ReadBuildInfo(dir)
	if err != nil {
		t.Fatalf("ReadBuildInfo failed: %v", err)
	}
	if info.Version != "0.2.2" || info.Commit != "9d7650c" || info.BuiltAt != "2026-11-12T10:00:00Z" {
		t.Fatalf("unexpected build info: %+v", info)
	}

	if err := os.WriteFile(path, []byte(`{"version":"bad"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadBuildInfo(dir); err == nil {
		t.Fatal("expected invalid version error")
	}
}
