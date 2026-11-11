// Package guard enforces the product rule "one device, one account".
//
// The binding is local: the first account that logs in on a machine owns that
// machine.  Logging out keeps the binding, so signing into a different account
// on the same install is refused with a fixed, user-facing message.
//
// The file is not a security boundary (a local user can delete it); it stops
// casual account switching and makes the intent explicit.  A server-side check
// can replace Check/Bind later without changing the caller.
package guard

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode"
)

const (
	// FileName is the default state file inside the app's data directory.
	FileName = "account_binding.json"
	// Version is the on-disk schema.  Bump it when the JSON changes.
	Version = 1
	// Message is shown verbatim in the login form when a second account is
	// refused.  Keep it identical to the wording promised in the docs.
	Message = "为防止恶意使用软件，软件请一号一设备绑定"
	// SupportMessage is used when the state is corrupt or was edited.  It
	// fails closed: a broken binding file must not silently allow a switch.
	SupportMessage = "账号绑定数据校验失败，请联系维护者重置绑定"
)

var (
	// ErrAccountMismatch means this device is bound to another account.
	ErrAccountMismatch = errors.New("account_mismatch")
	// ErrTampered means the file exists for this device but its MAC is wrong.
	ErrTampered = errors.New("tampered")
	// ErrCorrupt means the file is unreadable or has an unknown schema.
	ErrCorrupt = errors.New("corrupt")
	// ErrEmptyAccount means the caller passed an empty username.
	ErrEmptyAccount = errors.New("empty_account")
)

// State is the persisted binding.  Account is a hash, never the raw phone
// number; Hint is a masked form safe to show in a diagnostic.
type State struct {
	Version int    `json:"version"`
	Device  string `json:"device"`
	Account string `json:"account"`
	Hint    string `json:"hint,omitempty"`
	BoundAt int64  `json:"bound_at"`
	MAC     string `json:"mac"`
}

// Result is the outcome of Check, Bind or Status.
type Result struct {
	Allowed bool   `json:"allowed"`
	Bound   bool   `json:"bound"`
	Device  string `json:"device,omitempty"`
	Hint    string `json:"hint,omitempty"`
	BoundAt int64  `json:"bound_at,omitempty"`
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
}

// Guard reads and writes one binding file.
type Guard struct {
	Path     string
	DeviceID string
	secret   string
	// Now is injectable so tests do not depend on the wall clock.
	Now func() time.Time
}

// New opens (or prepares) a guard for path.  The device identity comes from
// the OS when available, otherwise from a random install id stored next to
// the binding file.
func New(path string) (*Guard, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("empty state path")
	}
	secret := machineID()
	if secret == "" {
		var err error
		secret, err = installID(path)
		if err != nil {
			return nil, err
		}
	}
	return newWithSecret(path, secret), nil
}

func newWithSecret(path, secret string) *Guard {
	sum := sha256.Sum256([]byte("hxp.device.v1|" + secret))
	return &Guard{
		Path:     path,
		DeviceID: hex.EncodeToString(sum[:16]),
		secret:   secret,
		Now:      time.Now,
	}
}

// Check reports whether account may log in on this device.  It does not
// write anything: Bind is only called after a successful server login.
func (g *Guard) Check(account string) (Result, error) {
	res := Result{Device: g.DeviceID}
	norm := NormalizeAccount(account)
	if norm == "" {
		res.Reason = Reason(ErrEmptyAccount)
		return res, ErrEmptyAccount
	}
	st, err := g.load()
	if err != nil {
		res.Reason = Reason(err)
		res.Message = SupportMessage
		return res, err
	}
	if st == nil {
		res.Allowed = true
		return res, nil
	}
	if st.Device != g.DeviceID {
		// The data directory was copied from another machine.  A local
		// device→account binding cannot travel with it, so treat this
		// machine as unbound instead of locking the user out.
		res.Allowed = true
		return res, nil
	}
	res.Bound = true
	res.Hint = st.Hint
	res.BoundAt = st.BoundAt
	if !hmac.Equal([]byte(st.Account), []byte(accountHash(norm))) {
		res.Reason = Reason(ErrAccountMismatch)
		res.Message = Message
		return res, ErrAccountMismatch
	}
	res.Allowed = true
	return res, nil
}

// Bind records account as the owner of this device.  It is idempotent for the
// same account and refuses a different one, even if a caller skips Check.
func (g *Guard) Bind(account string) (Result, error) {
	res := Result{Device: g.DeviceID}
	norm := NormalizeAccount(account)
	if norm == "" {
		res.Reason = Reason(ErrEmptyAccount)
		return res, ErrEmptyAccount
	}
	st, err := g.load()
	if err != nil {
		res.Reason = Reason(err)
		res.Message = SupportMessage
		return res, err
	}
	hash := accountHash(norm)
	if st != nil && st.Device == g.DeviceID && st.Account != hash {
		res.Bound = true
		res.Hint = st.Hint
		res.BoundAt = st.BoundAt
		res.Reason = Reason(ErrAccountMismatch)
		res.Message = Message
		return res, ErrAccountMismatch
	}
	next := &State{
		Version: Version,
		Device:  g.DeviceID,
		Account: hash,
		Hint:    MaskAccount(norm),
		BoundAt: g.now().Unix(),
	}
	if st != nil && st.Device == g.DeviceID && st.Account == hash {
		// Keep the original timestamp when the same account logs in again.
		next.BoundAt = st.BoundAt
	}
	if err := g.save(next); err != nil {
		res.Reason = "write_failed"
		res.Message = "账号绑定写入失败: " + err.Error()
		return res, err
	}
	res.Allowed = true
	res.Bound = true
	res.Hint = next.Hint
	res.BoundAt = next.BoundAt
	return res, nil
}

// Status describes the current binding without needing an account.
func (g *Guard) Status() (Result, error) {
	res := Result{Device: g.DeviceID}
	st, err := g.load()
	if err != nil {
		res.Reason = Reason(err)
		res.Message = SupportMessage
		return res, err
	}
	if st == nil || st.Device != g.DeviceID {
		res.Allowed = true
		return res, nil
	}
	res.Allowed = true
	res.Bound = true
	res.Hint = st.Hint
	res.BoundAt = st.BoundAt
	return res, nil
}

// Reset removes the binding.  It is a support action, not exposed in the UI.
func (g *Guard) Reset() error {
	err := os.Remove(g.Path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func (g *Guard) now() time.Time {
	if g.Now == nil {
		return time.Now()
	}
	return g.Now()
}

func (g *Guard) load() (*State, error) {
	raw, err := os.ReadFile(g.Path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var st State
	if err := json.Unmarshal(raw, &st); err != nil {
		return nil, ErrCorrupt
	}
	if st.Version != Version || st.Device == "" || st.Account == "" {
		return nil, ErrCorrupt
	}
	// A MAC mismatch is only meaningful on the machine that wrote the file.
	// On a different machine the secret is different too; Check treats that
	// as "moved data" and allows a fresh binding.
	if st.Device == g.DeviceID && !g.macOK(&st) {
		return nil, ErrTampered
	}
	return &st, nil
}

func (g *Guard) save(st *State) error {
	st.MAC = g.mac(st)
	raw, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	dir := filepath.Dir(g.Path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".account-binding-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, g.Path)
}

func (g *Guard) mac(st *State) string {
	h := hmac.New(sha256.New, g.macKey())
	fmt.Fprintf(h, "v1|%s|%s|%s|%d", st.Device, st.Account, st.Hint, st.BoundAt)
	return hex.EncodeToString(h.Sum(nil))
}

func (g *Guard) macOK(st *State) bool {
	return hmac.Equal([]byte(st.MAC), []byte(g.mac(st)))
}

func (g *Guard) macKey() []byte {
	sum := sha256.Sum256([]byte("hxp.account-guard.mac.v1|" + g.secret))
	return sum[:]
}

// NormalizeAccount trims the identifier and removes internal whitespace so a
// pasted "138 0000 0000" matches "13800000000".  Case is folded for the same
// reason; phone numbers and student ids are not case-sensitive.
func NormalizeAccount(account string) string {
	var b strings.Builder
	for _, r := range strings.TrimSpace(account) {
		if unicode.IsSpace(r) {
			continue
		}
		b.WriteRune(unicode.ToLower(r))
	}
	return b.String()
}

// MaskAccount keeps enough of the account to be recognisable in a warning
// without printing the whole phone number.
func MaskAccount(account string) string {
	r := []rune(NormalizeAccount(account))
	n := len(r)
	switch {
	case n == 0:
		return ""
	case n <= 2:
		return strings.Repeat("*", n)
	case n <= 6:
		return string(r[0]) + strings.Repeat("*", n-2) + string(r[n-1])
	default:
		return string(r[:3]) + "****" + string(r[n-4:])
	}
}

// Reason maps a sentinel error to the stable string used in JSON output.
func Reason(err error) string {
	switch {
	case errors.Is(err, ErrAccountMismatch):
		return "account_mismatch"
	case errors.Is(err, ErrTampered):
		return "tampered"
	case errors.Is(err, ErrCorrupt):
		return "corrupt"
	case errors.Is(err, ErrEmptyAccount):
		return "empty_account"
	case err == nil:
		return ""
	default:
		return "error"
	}
}

func accountHash(norm string) string {
	sum := sha256.Sum256([]byte("hxp.account.v1|" + norm))
	return hex.EncodeToString(sum[:])
}

func installID(statePath string) (string, error) {
	dir := filepath.Dir(statePath)
	path := filepath.Join(dir, "install_id")
	if raw, err := os.ReadFile(path); err == nil {
		if id := strings.TrimSpace(string(raw)); len(id) >= 16 {
			return id, nil
		}
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	id := hex.EncodeToString(buf)
	// O_EXCL keeps two racing first runs from overwriting each other; the
	// loser re-reads the winner's id.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, os.ErrExist) {
		raw, rerr := os.ReadFile(path)
		if rerr == nil {
			if existing := strings.TrimSpace(string(raw)); len(existing) >= 16 {
				return existing, nil
			}
		}
		return "", err
	}
	if err != nil {
		return "", err
	}
	if _, err := f.WriteString(id + "\n"); err != nil {
		f.Close()
		return "", err
	}
	if err := f.Close(); err != nil {
		return "", err
	}
	return id, nil
}
