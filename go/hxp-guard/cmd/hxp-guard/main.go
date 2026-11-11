// Command hxp-guard is the small Go sidecar behind the "one device, one
// account" rule.  It is called by the Python bridge before a login and after a
// successful one; it is not a daemon.
//
//	check  --account <id>   allowed? (does not write)
//	bind   --account <id>   claim this device for <id>
//	status                   current binding
//	reset  --yes             clear the binding (support action)
//	device                   print this machine's device id
//
// Every command prints one JSON object.  Exit codes: 0 ok, 3 blocked by the
// binding rule, 2 any other failure.
package main

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"haoxiangpao/hxp-guard/guard"
)

const (
	exitOK      = 0
	exitError   = 2
	exitBlocked = 3
	version     = "1.0.0"
)

type response struct {
	OK      bool   `json:"ok"`
	Version string `json:"version,omitempty"`
	guard.Result
	Error string `json:"error,omitempty"`
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		usage()
		return exitError
	}
	cmd := args[0]
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	state := fs.String("state", defaultState(), "path to account_binding.json")
	account := fs.String("account", "", "account id (phone or student id)")
	yes := fs.Bool("yes", false, "confirm reset")
	code := fs.Int("code", -1, "numeric op code")
	showVersion := fs.Bool("version", false, "print the sidecar version")
	if err := fs.Parse(args[1:]); err != nil {
		return exitError
	}
	if *showVersion {
		out := response{OK: true, Version: version,
			Result: guard.Result{Allowed: true}}
		_ = json.NewEncoder(os.Stdout).Encode(out)
		return exitOK
	}

	switch cmd {
	case "check", "bind", "status", "reset", "device", "help", "op":
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", cmd)
		usage()
		return exitError
	}

	if cmd == "help" {
		usage()
		return emit(guard.Result{Allowed: true}, nil)
	}
	if cmd == "op" {
		return runOp(*state, *code, os.Stdin)
	}

	g, err := guard.New(*state)
	if err != nil {
		return emit(guard.Result{Reason: "init_failed", Message: err.Error()}, err)
	}

	switch cmd {
	case "device":
		return emit(guard.Result{Allowed: true, Device: g.DeviceID}, nil)
	case "status":
		res, err := g.Status()
		return emit(res, err)
	case "reset":
		if !*yes {
			return emit(guard.Result{
				Reason:  "confirmation_required",
				Message: "reset removes the account binding; pass --yes to confirm",
			}, errors.New("confirmation required"))
		}
		if err := g.Reset(); err != nil {
			return emit(guard.Result{Reason: "reset_failed", Message: err.Error()}, err)
		}
		return emit(guard.Result{Allowed: true}, nil)
	case "check":
		res, err := g.Check(*account)
		return emit(res, err)
	case "bind":
		res, err := g.Bind(*account)
		return emit(res, err)
	default:
		return exitError
	}
}

// Numeric opcodes keep the sidecar interface non-descriptive.  Only the
// device-attestation op exists today; it reads one hex nonce line from stdin
// and returns a 32-byte opaque blob.  The nonce deliberately never appears in
// argv, which Windows exposes to other same-user processes.
const (
	opAttest = 17
)

type opResponse struct {
	OK    bool   `json:"ok"`
	Blob  string `json:"blob,omitempty"`
	Error string `json:"error,omitempty"`
}

func readNonceHex(in io.Reader) ([]byte, error) {
	reader := bufio.NewReader(in)
	line, err := reader.ReadString('\n')
	if err != nil && line == "" {
		return nil, err
	}
	return hex.DecodeString(strings.TrimSpace(line))
}

func runOp(state string, code int, in io.Reader) int {
	if code != opAttest {
		return emitOp(opResponse{OK: false, Error: "unknown op"})
	}
	nonce, err := readNonceHex(in)
	// The Python orchestrator always sends 16 bytes; anything else is a
	// protocol error, not a short-but-usable nonce.
	if err != nil || len(nonce) != 16 {
		return emitOp(opResponse{OK: false, Error: "bad nonce"})
	}
	g, err := guard.New(state)
	if err != nil {
		return emitOp(opResponse{OK: false, Error: "init failed"})
	}
	blob, err := g.Attest(nonce)
	if err != nil {
		// Not the underlying error: a collector must never leak a fragment.
		return emitOp(opResponse{OK: false, Error: "attest failed"})
	}
	return emitOp(opResponse{OK: true, Blob: hex.EncodeToString(blob)})
}

func emitOp(out opResponse) int {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(out); err != nil {
		return exitError
	}
	if !out.OK {
		return exitError
	}
	return exitOK
}

func emit(res guard.Result, err error) int {
	out := response{Result: res, OK: err == nil && res.Allowed}
	if err != nil {
		out.Error = err.Error()
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if encodeErr := enc.Encode(out); encodeErr != nil {
		fmt.Fprintln(os.Stderr, encodeErr)
		return exitError
	}
	switch {
	case err == nil:
		return exitOK
	case errors.Is(err, guard.ErrAccountMismatch):
		return exitBlocked
	default:
		return exitError
	}
}

func defaultState() string {
	if env := os.Getenv("JHP_ACCOUNT_GUARD_STATE"); env != "" {
		return env
	}
	return filepath.Join("data", guard.FileName)
}

func usage() {
	fmt.Fprintf(os.Stderr, `hxp-guard %s · one device, one account

  hxp-guard check  --state <file> --account <id>
  hxp-guard bind   --state <file> --account <id>
  hxp-guard status --state <file>
  hxp-guard reset  --state <file> --yes
  hxp-guard device --state <file>
  hxp-guard op     --state <file> --code 17   < nonce.hex

Exit codes: 0 ok, 3 blocked, 2 error.
`, version)
}
