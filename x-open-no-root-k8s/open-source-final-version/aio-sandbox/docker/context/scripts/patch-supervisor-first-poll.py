#!/usr/bin/env python3
from __future__ import annotations

"""Patch Supervisor 4.3.0 during the image build.

Supervisor waits for its first one-second poll before transitioning autostart
programs.  The image can safely skip only that first idle poll while retaining
the normal timeout for every later loop iteration.
"""

import importlib.metadata
import inspect
import os
import tempfile
from pathlib import Path


EXPECTED_VERSION = "4.3.0"
PATCH_MARKER = "# aio-sandbox: skip only the first idle poll"
ORIGINAL_TIMEOUT_LINE = "        timeout = 1 # this cannot be fewer than the smallest TickEvent (5)"
ORIGINAL_POLL_LINE = "            r, w = self.options.poller.poll(timeout)"
PATCHED_TIMEOUT_LINE = f"{ORIGINAL_TIMEOUT_LINE}\n        first_poll = True  {PATCH_MARKER}"
PATCHED_POLL_LINE = (
    "            r, w = self.options.poller.poll(0 if first_poll else timeout)\n"
    "            first_poll = False"
)


def patch_source(source: str) -> tuple[str, bool]:
    """Return the verified patched source and whether it changed."""
    if PATCH_MARKER in source:
        if (
            "first_poll = True" not in source
            or "poller.poll(0 if first_poll else timeout)" not in source
            or "first_poll = False" not in source
        ):
            raise RuntimeError("Supervisor first-poll patch marker is incomplete")
        return source, False

    if source.count(ORIGINAL_TIMEOUT_LINE) != 1:
        raise RuntimeError("Supervisor timeout source does not match the verified 4.3.0 layout")
    if source.count(ORIGINAL_POLL_LINE) != 1:
        raise RuntimeError("Supervisor poll source does not match the verified 4.3.0 layout")

    patched = source.replace(ORIGINAL_TIMEOUT_LINE, PATCHED_TIMEOUT_LINE, 1)
    patched = patched.replace(ORIGINAL_POLL_LINE, PATCHED_POLL_LINE, 1)
    compile(patched, "supervisor/supervisord.py", "exec")
    return patched, True


def write_atomic(path: Path, content: str) -> None:
    source_stat = path.stat()
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, source_stat.st_mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    import supervisor.supervisord

    version = importlib.metadata.version("supervisor")
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"expected Supervisor {EXPECTED_VERSION}, got {version}")

    module_path = Path(inspect.getfile(supervisor.supervisord)).resolve()
    source = module_path.read_text(encoding="utf-8")
    patched, changed = patch_source(source)
    if changed:
        write_atomic(module_path, patched)

    verified = module_path.read_text(encoding="utf-8")
    if PATCH_MARKER not in verified or "poller.poll(0 if first_poll else timeout)" not in verified:
        raise RuntimeError("Supervisor first-poll patch verification failed")
    print(f"Supervisor {version} first-poll patch ready: {module_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
