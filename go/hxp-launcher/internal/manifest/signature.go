package manifest

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
)

// PUBLIC_KEYS holds trusted Ed25519 release public keys by key_id.
// Release builds inject them through injectedPublicKeys; source builds keep
// the map empty so the launcher fails open instead of trusting a test key.
var PUBLIC_KEYS = map[string]ed25519.PublicKey{}

// injectedPublicKeys is set at link time:
//
//	-ldflags "-X haoxiangpao/hxp-launcher/internal/manifest.injectedPublicKeys=release:<base64>"
var injectedPublicKeys string

func init() {
	for id, key := range parseInjectedPublicKeys(injectedPublicKeys) {
		PUBLIC_KEYS[id] = key
	}
}

func parseInjectedPublicKeys(spec string) map[string]ed25519.PublicKey {
	out := make(map[string]ed25519.PublicKey)
	entries := strings.FieldsFunc(spec, func(r rune) bool {
		return r == ',' || r == ';' || r == '\n' || r == '\r'
	})
	for _, entry := range entries {
		id, b64, ok := strings.Cut(entry, ":")
		id = strings.TrimSpace(id)
		b64 = strings.TrimSpace(b64)
		if !ok || id == "" || b64 == "" {
			continue
		}
		pk, err := decodePublicKey(b64)
		if err != nil {
			continue
		}
		out[id] = pk
	}
	return out
}

var (
	ErrSignatureNotConfigured = errors.New("signature not configured")
	ErrMissingSignature       = errors.New("manifest missing signature")
	ErrUnsupportedAlg         = errors.New("unsupported signature algorithm")
	ErrUnknownKeyID           = errors.New("unknown signature key_id")
	ErrInvalidSignature       = errors.New("invalid manifest signature")
)

type SignatureInfo struct {
	Alg   string `json:"alg"`
	KeyID string `json:"key_id"`
}

func VerifyManifest(raw []byte, keys map[string]ed25519.PublicKey) (SignatureInfo, error) {
	if len(keys) == 0 {
		return SignatureInfo{}, ErrSignatureNotConfigured
	}

	obj, err := decodeJSONObject(raw)
	if err != nil {
		return SignatureInfo{}, err
	}

	sigRaw, ok := obj["signature"]
	if !ok || sigRaw == nil {
		return SignatureInfo{}, ErrMissingSignature
	}
	sigObj, ok := sigRaw.(map[string]any)
	if !ok {
		return SignatureInfo{}, ErrMissingSignature
	}

	alg, _ := sigObj["alg"].(string)
	if alg != "ed25519" {
		return SignatureInfo{}, fmt.Errorf("%w: %q", ErrUnsupportedAlg, alg)
	}

	keyID, _ := sigObj["key_id"].(string)
	if strings.TrimSpace(keyID) == "" || strings.ContainsRune(keyID, 0) {
		return SignatureInfo{}, ErrMissingSignature
	}

	valueB64, _ := sigObj["value"].(string)
	if valueB64 == "" || strings.TrimSpace(valueB64) != valueB64 ||
		strings.ContainsAny(valueB64, " \t\r\n\v\f") {
		return SignatureInfo{}, ErrInvalidSignature
	}
	sigBytes, err := base64.StdEncoding.DecodeString(valueB64)
	if err != nil || len(sigBytes) != ed25519.SignatureSize {
		return SignatureInfo{}, ErrInvalidSignature
	}

	pubKey, exists := keys[keyID]
	if !exists || len(pubKey) != ed25519.PublicKeySize {
		return SignatureInfo{}, fmt.Errorf("%w: %q", ErrUnknownKeyID, keyID)
	}

	payload := make(map[string]any, len(obj))
	for k, v := range obj {
		if k == "signature" {
			continue
		}
		payload[k] = v
	}
	canonical, err := CanonicalJSON(payload)
	if err != nil {
		return SignatureInfo{}, err
	}

	msg := make([]byte, 0, len(keyID)+1+len(canonical))
	msg = append(msg, keyID...)
	msg = append(msg, 0)
	msg = append(msg, canonical...)

	if !ed25519.Verify(pubKey, msg, sigBytes) {
		return SignatureInfo{}, ErrInvalidSignature
	}

	return SignatureInfo{Alg: alg, KeyID: keyID}, nil
}

// LoadPublicKeyFile loads an Ed25519 public key file for --allow-local tests.
// Supported formats:
// 1. JSON map: {"key_id": "<base64>"}
// 2. Lines of key_id:base64
// 3. Single base64-encoded 32-byte key (mapped to key_id "test", matching testdata/pubkey.b64)
func LoadPublicKeyFile(path string) (map[string]ed25519.PublicKey, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read pubkey file: %w", err)
	}
	text := strings.TrimSpace(string(raw))
	if text == "" {
		return nil, errors.New("empty pubkey file")
	}

	if strings.HasPrefix(text, "{") {
		var m map[string]string
		if err := json.Unmarshal([]byte(text), &m); err != nil {
			return nil, errors.New("invalid pubkey JSON")
		}
		out := make(map[string]ed25519.PublicKey, len(m))
		for id, b64 := range m {
			if strings.TrimSpace(id) == "" {
				return nil, errors.New("empty key_id in pubkey file")
			}
			pk, err := decodePublicKey(b64)
			if err != nil {
				return nil, err
			}
			out[id] = pk
		}
		if len(out) == 0 {
			return nil, errors.New("no keys in pubkey JSON")
		}
		return out, nil
	}

	if strings.Contains(text, ":") {
		out := make(map[string]ed25519.PublicKey)
		for _, line := range strings.Split(text, "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			id, b64, ok := strings.Cut(line, ":")
			id = strings.TrimSpace(id)
			if !ok || id == "" {
				return nil, errors.New("invalid key_id:base64 entry in pubkey file")
			}
			pk, err := decodePublicKey(b64)
			if err != nil {
				return nil, err
			}
			out[id] = pk
		}
		if len(out) == 0 {
			return nil, errors.New("no keys in pubkey file")
		}
		return out, nil
	}

	pk, err := decodePublicKey(text)
	if err != nil {
		return nil, err
	}
	return map[string]ed25519.PublicKey{"test": pk}, nil
}

func decodePublicKey(b64 string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("invalid ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}
