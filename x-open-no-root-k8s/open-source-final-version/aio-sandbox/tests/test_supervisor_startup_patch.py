from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "docker/context/scripts/patch-supervisor-first-poll.py"
SPEC = importlib.util.spec_from_file_location("patch_supervisor_first_poll", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SupervisorStartupPatchTest(unittest.TestCase):
    def original_source(self) -> str:
        return (
            "def runforever(self):\n"
            f"{MODULE.ORIGINAL_TIMEOUT_LINE}\n"
            "        while True:\n"
            f"{MODULE.ORIGINAL_POLL_LINE}\n"
            "            group.transition()\n"
        )

    def test_patches_only_first_poll(self) -> None:
        patched, changed = MODULE.patch_source(self.original_source())

        self.assertTrue(changed)
        self.assertIn("poller.poll(0 if first_poll else timeout)", patched)
        self.assertIn("first_poll = False", patched)
        self.assertEqual(patched.count("timeout = 1"), 1)

    def test_is_idempotent(self) -> None:
        patched, _ = MODULE.patch_source(self.original_source())
        second, changed = MODULE.patch_source(patched)

        self.assertFalse(changed)
        self.assertEqual(second, patched)

    def test_rejects_unknown_source_layout(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timeout source"):
            MODULE.patch_source("def runforever(self):\n    pass\n")


if __name__ == "__main__":
    unittest.main()
