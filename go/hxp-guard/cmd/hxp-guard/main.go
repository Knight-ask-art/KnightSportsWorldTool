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
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

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
	case "check", "bind", "status", "reset", "device", "help":
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", cmd)
		usage()
		return exitError
	}

	if cmd == "help" {
		usage()
		return emit(guard.Result{Allowed: true}, nil)
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

Exit codes: 0 ok, 3 blocked, 2 error.
`, version)
}
