package guard

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testGuard(t *testing.T, path, secret string) *Guard {
	t.Helper()
	g := newWithSecret(path, secret)
	g.Now = func() time.Time { return time.Unix(1700000000, 0).UTC() }
	return g
}

func tempPath(t *testing.T) string {
	t.Helper()
	return filepath.Join(t.TempDir(), FileName)
}

func TestFirstCheckAllowsWithoutBinding(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	res, err := g.Check("13800001234")
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if !res.Allowed || res.Bound {
		t.Fatalf("want allowed/unbound, got %+v", res)
	}
	if _, statErr := os.Stat(g.Path); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("Check must not create the state file: %v", statErr)
	}
}

func TestBindThenSameAccountIsAllowed(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	res, err := g.Check("138 0000 1234")
	if err != nil {
		t.Fatalf("Check after Bind: %v", err)
	}
	if !res.Allowed || !res.Bound {
		t.Fatalf("want allowed/bound, got %+v", res)
	}
	if res.Hint != "138****1234" {
		t.Fatalf("unexpected hint %q", res.Hint)
	}
}

func TestSecondAccountIsBlockedWithTheExactMessage(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	res, err := g.Check("13900005678")
	if !errors.Is(err, ErrAccountMismatch) {
		t.Fatalf("want ErrAccountMismatch, got %v", err)
	}
	if res.Allowed || !res.Bound {
		t.Fatalf("want blocked/bound, got %+v", res)
	}
	if res.Message != Message {
		t.Fatalf("message = %q, want %q", res.Message, Message)
	}
	if res.Reason != "account_mismatch" {
		t.Fatalf("reason = %q", res.Reason)
	}
}

func TestBindRefusesSecondAccountEvenWithoutCheck(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("first Bind: %v", err)
	}
	if _, err := g.Bind("13900005678"); !errors.Is(err, ErrAccountMismatch) {
		t.Fatalf("second Bind: want ErrAccountMismatch, got %v", err)
	}
	// The original binding is untouched.
	res, err := g.Check("13800001234")
	if err != nil || !res.Allowed {
		t.Fatalf("original account should still work: %+v %v", res, err)
	}
}

func TestSameAccountKeepsTheFirstBindTime(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	first, err := g.Bind("13800001234")
	if err != nil {
		t.Fatalf("Bind: %v", err)
	}
	later := func() time.Time { return time.Unix(1800000000, 0).UTC() }
	g.Now = later
	again, err := g.Bind("13800001234")
	if err != nil {
		t.Fatalf("second Bind: %v", err)
	}
	if again.BoundAt != first.BoundAt {
		t.Fatalf("bound_at changed: %d -> %d", first.BoundAt, again.BoundAt)
	}
}

func TestMovedDataDirectoryAllowsAFreshBinding(t *testing.T) {
	path := tempPath(t)
	a := testGuard(t, path, "secret-a")
	if _, err := a.Bind("13800001234"); err != nil {
		t.Fatalf("Bind on A: %v", err)
	}
	// Same file, different machine secret: the binding must not follow.
	b := testGuard(t, path, "secret-b")
	res, err := b.Check("13900005678")
	if err != nil {
		t.Fatalf("Check on B: %v", err)
	}
	if !res.Allowed || res.Bound {
		t.Fatalf("want allowed/unbound on a new machine, got %+v", res)
	}
	if _, err := b.Bind("13900005678"); err != nil {
		t.Fatalf("Bind on B: %v", err)
	}
	res, err = b.Check("13900005678")
	if err != nil || !res.Allowed || !res.Bound {
		t.Fatalf("new binding should work: %+v %v", res, err)
	}
}

func TestTamperedStateFailsClosed(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	raw, err := os.ReadFile(g.Path)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	doc["account"] = strings.Repeat("0", 64)
	edited, _ := json.Marshal(doc)
	if err := os.WriteFile(g.Path, edited, 0o600); err != nil {
		t.Fatal(err)
	}
	res, err := g.Check("13800001234")
	if !errors.Is(err, ErrTampered) {
		t.Fatalf("want ErrTampered, got %v", err)
	}
	if res.Allowed {
		t.Fatalf("tampered state must not allow a login: %+v", res)
	}
	if res.Message != SupportMessage {
		t.Fatalf("message = %q", res.Message)
	}
}

func TestCorruptStateFailsClosed(t *testing.T) {
	path := tempPath(t)
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	g := testGuard(t, path, "secret-a")
	res, err := g.Check("13800001234")
	if !errors.Is(err, ErrCorrupt) || res.Allowed {
		t.Fatalf("want corrupt/blocked, got %+v %v", res, err)
	}
}

func TestUnknownSchemaFailsClosed(t *testing.T) {
	path := tempPath(t)
	raw := `{"version":99,"device":"x","account":"y","mac":"z"}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	g := testGuard(t, path, "secret-a")
	if _, err := g.Check("13800001234"); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("want ErrCorrupt, got %v", err)
	}
}

func TestEmptyAccountIsRejected(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	for _, account := range []string{"", "   ", "\t\n"} {
		if _, err := g.Check(account); !errors.Is(err, ErrEmptyAccount) {
			t.Fatalf("Check(%q): want ErrEmptyAccount, got %v", account, err)
		}
		if _, err := g.Bind(account); !errors.Is(err, ErrEmptyAccount) {
			t.Fatalf("Bind(%q): want ErrEmptyAccount, got %v", account, err)
		}
	}
}

func TestResetClearsTheBinding(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	if err := g.Reset(); err != nil {
		t.Fatalf("Reset: %v", err)
	}
	res, err := g.Check("13900005678")
	if err != nil || !res.Allowed || res.Bound {
		t.Fatalf("after Reset: %+v %v", res, err)
	}
	if err := g.Reset(); err != nil {
		t.Fatalf("Reset must be idempotent: %v", err)
	}
}

func TestStatusDoesNotNeedAnAccount(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	res, err := g.Status()
	if err != nil || !res.Allowed || !res.Bound {
		t.Fatalf("Status: %+v %v", res, err)
	}
	if res.Hint != "138****1234" {
		t.Fatalf("hint = %q", res.Hint)
	}
}

func TestMaskAccount(t *testing.T) {
	cases := map[string]string{
		"":              "",
		"u":             "*",
		"ab":            "**",
		"abc":           "a*c",
		"2023123456":    "202****3456",
		"13800001234":   "138****1234",
		" 138 00001234": "138****1234",
	}
	for in, want := range cases {
		if got := MaskAccount(in); got != want {
			t.Errorf("MaskAccount(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestNormalizeAccount(t *testing.T) {
	cases := map[string]string{
		" 138 0000 1234 ": "13800001234",
		"AbC":             "abc",
		"2023\t123456":    "2023123456",
	}
	for in, want := range cases {
		if got := NormalizeAccount(in); got != want {
			t.Errorf("NormalizeAccount(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestStateDoesNotStoreTheRawAccount(t *testing.T) {
	g := testGuard(t, tempPath(t), "secret-a")
	if _, err := g.Bind("13800001234"); err != nil {
		t.Fatalf("Bind: %v", err)
	}
	raw, err := os.ReadFile(g.Path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "13800001234") {
		t.Fatalf("state file leaked the raw account: %s", raw)
	}
}
