package main

import (
	"path/filepath"
	"strings"
	"testing"
)

func testState(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), "account_binding.json")
}

func TestRunOpRejectsNonSixteenByteNonces(t *testing.T) {
	state := testState(t)
	cases := []string{
		"",
		"0011223344556677",                   // 8 bytes, the old minimum
		"00112233445566778899aabbccddeeff00", // 17 bytes
		"zz",
	}
	for _, input := range cases {
		if code := runOp(state, opAttest, strings.NewReader(input+"\n")); code != exitError {
			t.Fatalf("nonce %q: exit %d, want %d", input, code, exitError)
		}
	}
}

func TestRunOpRejectsUnknownCodes(t *testing.T) {
	state := testState(t)
	nonce := "00112233445566778899aabbccddeeff\n"
	if code := runOp(state, 18, strings.NewReader(nonce)); code != exitError {
		t.Fatalf("unknown op: exit %d, want %d", code, exitError)
	}
}
