#!/usr/bin/env python3
"""Diagnose why fchmod on container stdout pipes may not take effect, and
whether uid-1000 can then open /dev/stdout."""
import os
import stat

def fmt(st):
    return f"mode={oct(stat.S_IMODE(st.st_mode))} uid={st.st_uid} gid={st.st_gid} ftype={stat.S_IFMT(st.st_mode) & 0xF000:o}"

print("=== BEFORE ===")
for n, fd in (("1", 1), ("2", 2)):
    try:
        print(f"fd{n}: {fmt(os.fstat(fd))}")
    except Exception as e:
        print(f"fd{n}: fstat FAIL: {e}")

print("=== try fchmod fd1/fd2 to 0666 ===")
for n, fd in (("1", 1), ("2", 2)):
    try:
        os.fchmod(fd, 0o666)
        print(f"fd{n}: fchmod OK -> {fmt(os.fstat(fd))}")
    except Exception as e:
        print(f"fd{n}: fchmod FAIL: {type(e).__name__}: {e}")

print("=== try open /dev/stdout and /dev/stderr ===")
for name, path in (("stdout", "/dev/stdout"), ("stderr", "/dev/stderr")):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        print(f"{name}: OPEN_OK fd={fd}")
        os.close(fd)
    except Exception as e:
        print(f"{name}: OPEN_FAIL: {type(e).__name__}: {e}")

print("=== stat /dev/stdout /dev/stderr symlinks ===")
for name, path in (("stdout", "/dev/stdout"), ("stderr", "/dev/stderr")):
    try:
        st = os.lstat(path)
        print(f"{name}: lstat {fmt(st)}")
        st2 = os.stat(path)
        print(f"{name}: stat  {fmt(st2)}")
    except Exception as e:
        print(f"{name}: FAIL: {type(e).__name__}: {e}")
