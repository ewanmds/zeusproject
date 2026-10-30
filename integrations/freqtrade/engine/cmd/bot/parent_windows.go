//go:build windows

package main

import "syscall"

func parentAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, err := syscall.OpenProcess(0x1000, false, uint32(pid))
	if err != nil {
		return err == syscall.ERROR_ACCESS_DENIED
	}
	defer syscall.CloseHandle(handle)
	var code uint32
	if syscall.GetExitCodeProcess(handle, &code) != nil {
		return false
	}
	return code == 259
}
