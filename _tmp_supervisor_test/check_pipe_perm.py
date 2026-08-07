#!/usr/bin/env python3
"""Check whether uid can open /dev/stdout and /dev/stderr, and their mode."""
import os
import sys

print(f"running as euid={os.geteuid()} uid={os.getuid()}")

for name, path in (("stdout", "/dev/stdout"), ("stderr", "/dev/stderr")):
    try:
        st = os.stat(path)
        print(f"{name}: {path} mode={oct(st.st_mode)} uid={st.st_uid} gid={st.st_gid}")
    except Exception as e:
        print(f"{name}: stat failed: {e}")

for name, path in (("stdout", "/dev/stdout"), ("stderr", "/dev/stderr")):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        os.write(fd, b"OPEN_OK\n")
        os.close(fd)
        print(f"{name}: OPEN_OK")
    except Exception as e:
        print(f"{name}: OPEN_FAIL: {type(e).__name__}: {e}")
