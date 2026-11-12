package manifest

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// CanonicalPayload decodes raw manifest JSON, removes top-level "signature",
// and encodes the remaining object identically to Python's:
// json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
func CanonicalPayload(raw []byte) ([]byte, error) {
	obj, err := decodeJSONObject(raw)
	if err != nil {
		return nil, err
	}
	payload := make(map[string]any, len(obj))
	for k, v := range obj {
		if k == "signature" {
			continue
		}
		payload[k] = v
	}
	return CanonicalJSON(payload)
}

func CanonicalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func decodeJSONObject(raw []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()

	var root any
	if err := dec.Decode(&root); err != nil {
		return nil, fmt.Errorf("invalid json: %w", err)
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		return nil, errors.New("unexpected trailing JSON content")
	}
	obj, ok := root.(map[string]any)
	if !ok || obj == nil {
		return nil, errors.New("manifest must be a JSON object")
	}
	return obj, nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if x {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case string:
		return writeCanonicalString(buf, x)
	case json.Number:
		return writeCanonicalNumber(buf, x)
	case int:
		buf.WriteString(strconv.Itoa(x))
	case int64:
		buf.WriteString(strconv.FormatInt(x, 10))
	case uint64:
		buf.WriteString(strconv.FormatUint(x, 10))
	case float64:
		return writeCanonicalFloat(buf, x)
	case []any:
		buf.WriteByte('[')
		for i, elem := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, elem); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		if x == nil {
			buf.WriteString("null")
			return nil
		}
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonicalString(buf, k); err != nil {
				return err
			}
			buf.WriteByte(':')
			if err := writeCanonical(buf, x[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("unsupported JSON type %T", v)
	}
	return nil
}

func writeCanonicalNumber(buf *bytes.Buffer, num json.Number) error {
	s := num.String()
	if s == "" {
		return errors.New("empty json number")
	}
	if !strings.ContainsAny(s, ".eE") {
		if s == "-0" {
			buf.WriteByte('0')
			return nil
		}
		buf.WriteString(s)
		return nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return fmt.Errorf("invalid json number: %w", err)
	}
	return writeCanonicalFloat(buf, f)
}

func writeCanonicalFloat(buf *bytes.Buffer, f float64) error {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return errors.New("NaN and Infinity are not allowed in canonical JSON")
	}
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") {
		s += ".0"
	}
	buf.WriteString(s)
	return nil
}

// writeCanonicalString matches Python json.dumps(..., ensure_ascii=False).
func writeCanonicalString(buf *bytes.Buffer, s string) error {
	buf.WriteByte('"')
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			return errors.New("invalid UTF-8 in string")
		}
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteString(s[i : i+size])
			}
		}
		i += size
	}
	buf.WriteByte('"')
	return nil
}
