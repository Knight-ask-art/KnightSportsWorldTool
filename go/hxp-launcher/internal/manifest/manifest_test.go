package manifest

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testdataDir(t *testing.T) string {
	t.Helper()
	return filepath.Join("..", "..", "testdata")
}

func loadTestVector(t *testing.T) (manifestBytes, canonicalBytes []byte, pub ed25519.PublicKey) {
	t.Helper()
	dir := testdataDir(t)
	var err error
	manifestBytes, err = os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		t.Fatalf("read testdata/manifest.json: %v", err)
	}
	canonicalBytes, err = os.ReadFile(filepath.Join(dir, "canonical.bin"))
	if err != nil {
		t.Fatalf("read testdata/canonical.bin: %v", err)
	}
	keys, err := LoadPublicKeyFile(filepath.Join(dir, "pubkey.b64"))
	if err != nil {
		t.Fatalf("load testdata/pubkey.b64: %v", err)
	}
	pub, ok := keys["test"]
	if !ok {
		t.Fatal("expected key_id 'test' in pubkey.b64")
	}
	return manifestBytes, canonicalBytes, pub
}

func TestCanonicalVectorMatchesPython(t *testing.T) {
	manifestBytes, canonicalBytes, _ := loadTestVector(t)
	got, err := CanonicalPayload(manifestBytes)
	if err != nil {
		t.Fatalf("CanonicalPayload error: %v", err)
	}
	if !bytes.Equal(got, canonicalBytes) {
		t.Fatalf("canonical mismatch:\ngot:  %q\nwant: %q", string(got), string(canonicalBytes))
	}
}

func TestCanonicalEdgeCases(t *testing.T) {
	input := map[string]any{
		"z":    "中文",
		"a":    "<script>&amp;\u2028\u2029",
		"ctrl": "\x00\x01\b\f\n\r\t\"\\",
		"arr":  []any{1, true, false, nil},
		"nested": map[string]any{
			"b": 2,
			"a": 1,
		},
	}
	got, err := CanonicalJSON(input)
	if err != nil {
		t.Fatalf("CanonicalJSON error: %v", err)
	}
	want := `{"a":"<script>&amp;` + "\u2028\u2029" + `","arr":[1,true,false,null],"ctrl":"\u0000\u0001\b\f\n\r\t\"\\","nested":{"a":1,"b":2},"z":"中文"}`
	if string(got) != want {
		t.Fatalf("canonical edge case mismatch:\ngot:  %s\nwant: %s", string(got), want)
	}
}

func TestCanonicalFloatMatchesPython(t *testing.T) {
	sum := 0.1
	sum += 0.2
	cases := []struct {
		value float64
		want  string
	}{
		{0, "0.0"},
		{math.Copysign(0, -1), "-0.0"},
		{1, "1.0"},
		{1000000, "1000000.0"},
		{9999999999999998, "9999999999999998.0"},
		{1e16, "1e+16"},
		{1e-4, "0.0001"},
		{1e-5, "1e-05"},
		{1e-7, "1e-07"},
		{1.2345678901234567e20, "1.2345678901234567e+20"},
		{sum, "0.30000000000000004"},
		{1.5e6, "1500000.0"},
	}
	for _, tc := range cases {
		got, err := CanonicalJSON(map[string]any{"x": tc.value})
		if err != nil {
			t.Fatalf("CanonicalJSON(%v) error: %v", tc.value, err)
		}
		want := `{"x":` + tc.want + `}`
		if string(got) != want {
			t.Fatalf("float %v: got %s, want %s", tc.value, got, want)
		}
	}
}

func TestVerifyTestVector(t *testing.T) {
	manifestBytes, _, pub := loadTestVector(t)
	keys := map[string]ed25519.PublicKey{"test": pub}

	sig, err := VerifyManifest(manifestBytes, keys)
	if err != nil {
		t.Fatalf("VerifyManifest failed: %v", err)
	}
	if sig.Alg != "ed25519" || sig.KeyID != "test" {
		t.Fatalf("unexpected sig info: %+v", sig)
	}
}

func TestVerifyFailures(t *testing.T) {
	manifestBytes, _, pub := loadTestVector(t)
	keys := map[string]ed25519.PublicKey{"test": pub}

	// Empty key map -> ErrSignatureNotConfigured
	if _, err := VerifyManifest(manifestBytes, nil); !errors.Is(err, ErrSignatureNotConfigured) {
		t.Fatalf("expected ErrSignatureNotConfigured, got %v", err)
	}

	// Tamper version
	tamperedVersion := bytes.Replace(manifestBytes, []byte(`"version": "9.9.9"`), []byte(`"version": "9.9.8"`), 1)
	if _, err := VerifyManifest(tamperedVersion, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("expected ErrInvalidSignature for tampered version, got %v", err)
	}

	// Tamper assets
	tamperedAssets := bytes.Replace(manifestBytes, []byte(`"size": 123456`), []byte(`"size": 123457`), 1)
	if _, err := VerifyManifest(tamperedAssets, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("expected ErrInvalidSignature for tampered assets, got %v", err)
	}

	// Wrong key
	wrongSeed := make([]byte, ed25519.SeedSize)
	wrongSeed[0] = 99
	wrongPub := ed25519.NewKeyFromSeed(wrongSeed).Public().(ed25519.PublicKey)
	if _, err := VerifyManifest(manifestBytes, map[string]ed25519.PublicKey{"test": wrongPub}); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("expected ErrInvalidSignature for wrong key, got %v", err)
	}

	// Unknown key_id
	if _, err := VerifyManifest(manifestBytes, map[string]ed25519.PublicKey{"other": pub}); !errors.Is(err, ErrUnknownKeyID) {
		t.Fatalf("expected ErrUnknownKeyID, got %v", err)
	}

	// Bad alg
	badAlg := bytes.Replace(manifestBytes, []byte(`"alg": "ed25519"`), []byte(`"alg": "rsa"`), 1)
	if _, err := VerifyManifest(badAlg, keys); !errors.Is(err, ErrUnsupportedAlg) {
		t.Fatalf("expected ErrUnsupportedAlg, got %v", err)
	}

	// Signature value tampering
	var rawObj map[string]any
	if err := json.Unmarshal(manifestBytes, &rawObj); err != nil {
		t.Fatal(err)
	}
	sigObj := rawObj["signature"].(map[string]any)
	sigBytes, _ := base64.StdEncoding.DecodeString(sigObj["value"].(string))
	sigBytes[0] ^= 0xff
	sigObj["value"] = base64.StdEncoding.EncodeToString(sigBytes)
	badSigBytes, _ := json.Marshal(rawObj)
	if _, err := VerifyManifest(badSigBytes, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("expected ErrInvalidSignature for mutated sig bytes, got %v", err)
	}
}

func TestParseAndValidateRejectsNullSchema(t *testing.T) {
	manifestBytes, _, _ := loadTestVector(t)
	raw := bytes.Replace(manifestBytes, []byte(`"schema": 1`), []byte(`"schema": null`), 1)
	if _, err := ParseAndValidate(raw); err == nil {
		t.Fatal("expected schema null to be rejected")
	}
}

func TestFetchErrorRedactsQueryAndFragment(t *testing.T) {
	const secret = "SECRET_TOKEN_12345"
	rawURL := "http://127.0.0.1:1/latest.json?token=" + secret + "#frag"
	_, err := Fetch(context.Background(), rawURL, true, 500*time.Millisecond)
	if err == nil {
		t.Fatal("expected connection error")
	}
	if strings.Contains(err.Error(), secret) {
		t.Fatalf("query token leaked into error: %v", err)
	}
	if strings.Contains(err.Error(), "frag") {
		t.Fatalf("fragment leaked into error: %v", err)
	}
}

func TestVerifyRejectsWhitespaceInSignature(t *testing.T) {
	manifestBytes, _, pub := loadTestVector(t)
	keys := map[string]ed25519.PublicKey{"test": pub}
	var rawObj map[string]any
	if err := json.Unmarshal(manifestBytes, &rawObj); err != nil {
		t.Fatal(err)
	}
	sigObj := rawObj["signature"].(map[string]any)
	value := sigObj["value"].(string)
	sigObj["value"] = value[:8] + "\n" + value[8:]
	badBytes, _ := json.Marshal(rawObj)
	if _, err := VerifyManifest(badBytes, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("expected ErrInvalidSignature for whitespace in signature, got %v", err)
	}
}

func TestIsAllowedURL(t *testing.T) {
	cases := []struct {
		raw        string
		allowLocal bool
		want       bool
	}{
		{"https://example.com/latest.json", false, true},
		{"http://example.com/latest.json", false, false},
		{"http://example.com/latest.json", true, false},
		{"http://localhost:8080/latest.json", false, false},
		{"http://localhost:8080/latest.json", true, true},
		{"http://127.0.0.1:8080/latest.json", false, false},
		{"http://127.0.0.1:8080/latest.json", true, true},
		{"file:///C:/tmp/latest.json", false, false},
		{"file:///C:/tmp/latest.json", true, true},
	}
	for _, tc := range cases {
		u, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %q: %v", tc.raw, err)
		}
		if got := IsAllowedURL(u, tc.allowLocal); got != tc.want {
			t.Fatalf("IsAllowedURL(%q, %v)=%v, want %v", tc.raw, tc.allowLocal, got, tc.want)
		}
	}
}

func TestFetchAndVerifyHTTPTest(t *testing.T) {
	manifestBytes, _, pub := loadTestVector(t)
	keys := map[string]ed25519.PublicKey{"test": pub}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(manifestBytes)
	}))
	defer srv.Close()

	// Rejected without allowLocal
	if _, err := FetchAndVerify(context.Background(), []string{srv.URL}, keys, false, 2*time.Second); err == nil {
		t.Fatal("expected http://127.0.0.1 to fail when allowLocal=false")
	}

	// Succeeds with allowLocal
	res, err := FetchAndVerify(context.Background(), []string{srv.URL}, keys, true, 2*time.Second)
	if err != nil {
		t.Fatalf("FetchAndVerify failed: %v", err)
	}
	if res.Manifest.Version != "9.9.9" || res.Signature.KeyID != "test" {
		t.Fatalf("unexpected verified result: %+v", res)
	}
}

func TestFetchLocalFileURL(t *testing.T) {
	manifestBytes, _, pub := loadTestVector(t)
	keys := map[string]ed25519.PublicKey{"test": pub}

	tmp := filepath.Join(t.TempDir(), "manifest.json")
	if err := os.WriteFile(tmp, manifestBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	fileURL := (&url.URL{Scheme: "file", Path: "/" + strings.TrimPrefix(filepath.ToSlash(tmp), "/")}).String()

	if _, err := FetchAndVerify(context.Background(), []string{fileURL}, keys, false, 2*time.Second); err == nil {
		t.Fatal("expected file:// to fail when allowLocal=false")
	}
	res, err := FetchAndVerify(context.Background(), []string{fileURL}, keys, true, 2*time.Second)
	if err != nil {
		t.Fatalf("file:// FetchAndVerify failed: %v", err)
	}
	if res.Manifest.Version != "9.9.9" {
		t.Fatalf("unexpected version %q", res.Manifest.Version)
	}
}
