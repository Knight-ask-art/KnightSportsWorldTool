//go:build windows

package runlock

import (
	"fmt"
	"os"
	"testing"
	"time"
)

func TestTryAcquireIsExclusive(t *testing.T) {
	name := fmt.Sprintf(`Local\hxp-runlock-test-%d-%d`, os.Getpid(), time.Now().UnixNano())
	first, ok, err := TryAcquire(name)
	if err != nil || !ok {
		t.Fatalf("first acquire: ok=%v err=%v", ok, err)
	}
	defer first.Release()

	second, ok, err := TryAcquire(name)
	if err != nil {
		t.Fatalf("second acquire error: %v", err)
	}
	if ok {
		second.Release()
		t.Fatal("second acquire unexpectedly succeeded")
	}

	if err := first.Release(); err != nil {
		t.Fatalf("release: %v", err)
	}
	third, ok, err := TryAcquire(name)
	if err != nil || !ok {
		t.Fatalf("reacquire after release: ok=%v err=%v", ok, err)
	}
	_ = third.Release()
}

func TestAcquireTimesOutWhileHeld(t *testing.T) {
	name := fmt.Sprintf(`Local\hxp-runlock-wait-%d-%d`, os.Getpid(), time.Now().UnixNano())
	first, ok, err := TryAcquire(name)
	if err != nil || !ok {
		t.Fatalf("first acquire: ok=%v err=%v", ok, err)
	}
	defer first.Release()

	start := time.Now()
	second, ok, err := Acquire(name, 150*time.Millisecond)
	if err != nil {
		t.Fatalf("Acquire error: %v", err)
	}
	if ok || second != nil {
		t.Fatal("Acquire should time out while the lock is held")
	}
	if elapsed := time.Since(start); elapsed < 100*time.Millisecond {
		t.Fatalf("Acquire returned too early: %s", elapsed)
	}
}
