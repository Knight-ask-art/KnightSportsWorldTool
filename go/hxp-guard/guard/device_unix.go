//go:build !windows

package guard

import (
	"os"
	"strings"
)

// machineID reads the usual Linux machine id.  On platforms without one the
// caller falls back to a random install id kept next to the binding file.
func machineID() string {
	for _, path := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
		if raw, err := os.ReadFile(path); err == nil {
			if id := strings.TrimSpace(string(raw)); id != "" {
				return id
			}
		}
	}
	return ""
}
