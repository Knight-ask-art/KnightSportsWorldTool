//go:build windows

package guard

import (
	"os/exec"
	"strings"
)

// machineID reads the Windows MachineGuid.  It survives reboots, app updates
// and device-fingerprint rotation, which is what the binding needs.  reg.exe
// is used instead of x/sys/windows so the module stays dependency-free and
// builds offline.
func machineID() string {
	out, err := exec.Command(
		"reg", "query", `HKLM\SOFTWARE\Microsoft\Cryptography`,
		"/v", "MachineGuid",
	).Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && strings.EqualFold(fields[0], "MachineGuid") {
			return fields[len(fields)-1]
		}
	}
	return ""
}
