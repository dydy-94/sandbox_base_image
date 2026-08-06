#!/usr/bin/env python3
"""Wrapper around zipfile extraction that side-steps docker-desktop
overlayfs Errno 22 by writing each member to /tmp first, then renaming
to the final destination. Used by /opt/application/post-inst.sh to
re-install the C-extension .so files that pip's normal install silently
dropped on Windows docker-desktop (the overlay filesystem can not
handle the `os.replace` syscall that pip uses to atomically rename
intermediate files into the site-packages tree).

Usage (both forms supported for backwards compatibility):

  # old: positional <wheel> <destination>
  sweep-so.py <wheel-file> <dest-site-packages>

  # new: argparse-style flags
  sweep-so.py --wheel <wheel-file> --site <dest-site-packages>
  sweep-so.py --wheels-dir <dir-of-wheel-files> --site <dest-site-packages>  # legacy
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def extract_wheel(wheel: Path, dest: Path) -> int:
    """Extract `wheel` into `dest` member-by-member using tmp+rename.

    Returns the number of `.so` files written (for diagnostics).
    """
    n_so = 0
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            out_path = dest / info.filename
            if info.is_dir():
                out_path.mkdir(parents=True, exist_ok=True)
                continue
            data = z.read(info)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix='ext_', dir='/tmp')
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            shutil.move(tmp_path, out_path)
            if info.filename.endswith('.so'):
                n_so += 1
                try:
                    os.chmod(out_path, 0o755)  # readable by `gem`
                except OSError:
                    pass
                print(f'  wrote {info.filename} ({len(data)} bytes)')
    return n_so


def main(argv: list[str]) -> int:
    # First: legacy positional form `sweep-so.py <wheel> <dest>`.
    if len(argv) >= 3 and not argv[1].startswith('-'):
        wheel = Path(argv[1])
        dest = Path(argv[2])
    else:
        p = argparse.ArgumentParser()
        p.add_argument('--wheel', help='a single .whl file to extract')
        p.add_argument('--wheels-dir', help='directory containing .whl files (legacy)')
        p.add_argument('--site', required=True, help='destination site-packages directory')
        args = p.parse_args(argv[1:])
        if args.wheel:
            wheel = Path(args.wheel)
        elif args.wheels_dir:
            whls = sorted(Path(args.wheels_dir).glob('*.whl'))
            if not whls:
                print(f'no wheels in {args.wheels_dir}', file=sys.stderr)
                return 1
            total = 0
            for w in whls:
                total += extract_wheel(w, Path(args.site))
            print(f'extracted {len(whls)} wheels, {total} .so files written')
            return 0
        else:
            p.error('either --wheel or --wheels-dir is required')

    dest.mkdir(parents=True, exist_ok=True)
    n = extract_wheel(wheel, dest)
    print(f'extracted {wheel.name}, {n} .so files written')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
