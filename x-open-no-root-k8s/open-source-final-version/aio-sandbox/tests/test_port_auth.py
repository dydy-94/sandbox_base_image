from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOTFS_GEM = Path(__file__).parents[1] / "docker/context/rootfs/opt/gem"
sys.path.insert(0, str(ROOTFS_GEM))

from port_auth.environment import SandboxContext, SandboxContextLoader  # noqa: E402
from port_auth.policy import authorize  # noqa: E402


def _segment(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token(payload: dict[str, object]) -> str:
    return f"{_segment({'alg': 'RS256'})}.{_segment(payload)}.signature"


class AuthorizationPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.user = SandboxContext(sandbox_type="USER", user_id="AbC123")

    def test_non_user_sandboxes_bypass_token_checks(self) -> None:
        for sandbox_type in ("EXPERT", "SESSION", "ADMIN"):
            with self.subTest(sandbox_type=sandbox_type):
                context = SandboxContext(sandbox_type=sandbox_type, user_id="")
                self.assertEqual(authorize(context, None).status, 204)

    def test_user_requires_well_formed_unexpired_token(self) -> None:
        cases = (
            (None, "missing_token"),
            ("not-a-jwt", "malformed_token"),
            (_token({"sap_id": "abc123"}), "invalid_exp"),
            (_token({"exp": "9999999999", "sap_id": "abc123"}), "invalid_exp"),
            (_token({"exp": 99, "sap_id": "abc123"}), "expired_token"),
        )
        for token, reason in cases:
            with self.subTest(reason=reason):
                result = authorize(self.user, token, now=100)
                self.assertEqual(result.status, 401)
                self.assertEqual(result.reason, reason)

    def test_native_subject_bypasses_user_comparison(self) -> None:
        token = _token({"exp": 200, "sub": " System@Native ", "sap_id": "other"})
        self.assertEqual(authorize(self.user, token, now=100).status, 204)

    def test_sap_id_comparison_trims_and_lowercases(self) -> None:
        token = _token({"exp": 200, "sap_id": " abc123 "})
        self.assertEqual(authorize(self.user, token, now=100).status, 204)

    def test_sap_id_mismatch_does_not_fall_back_to_rtc_id(self) -> None:
        token = _token({"exp": 200, "sap_id": "other", "rtc_id": "abc123"})
        result = authorize(self.user, token, now=100)
        self.assertEqual(result.status, 403)
        self.assertEqual(result.reason, "user_mismatch")

    def test_rtc_id_is_used_only_when_sap_id_is_absent(self) -> None:
        token = _token({"exp": 200, "rtc_id": "ABC123"})
        self.assertEqual(authorize(self.user, token, now=100).status, 204)

    def test_missing_both_user_claims_is_allowed(self) -> None:
        token = _token({"exp": 200, "sub": "person@example.com"})
        self.assertEqual(authorize(self.user, token, now=100).status, 204)

    def test_empty_sap_id_falls_back_to_rtc_id(self) -> None:
        token = _token({"exp": 200, "sap_id": "  ", "rtc_id": "abc123"})
        self.assertEqual(authorize(self.user, token, now=100).status, 204)


class SandboxContextLoaderTest(unittest.TestCase):
    def test_reads_sources_in_priority_order_and_caches_complete_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service_env = Path(td) / "service_env.json"
            bashrc = Path(td) / ".bashrc"
            service_env.write_text(json.dumps({"X_SANDBOX_TYPE": "user"}), encoding="utf-8")
            bashrc.write_text(
                "ignored=$(unsafe)\nexport X_SANDBOX_USER_ID='From-Bashrc'\n",
                encoding="utf-8",
            )
            loader = SandboxContextLoader(
                service_env_path=service_env,
                bashrc_path=bashrc,
                process_env={"X_SANDBOX_USER_ID": "From-Process"},
            )

            context = loader.get()
            self.assertEqual(context, SandboxContext("USER", "From-Bashrc"))

            service_env.unlink()
            bashrc.unlink()
            self.assertIs(loader.get(), context)

    def test_process_environment_is_last_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            loader = SandboxContextLoader(
                service_env_path=Path(td) / "missing.json",
                bashrc_path=Path(td) / "missing-bashrc",
                process_env={
                    "X_SANDBOX_TYPE": "SESSION",
                    "X_SANDBOX_USER_ID": "unused",
                },
            )
            self.assertEqual(loader.get(), SandboxContext("SESSION", "unused"))

    def test_incomplete_user_context_is_not_cached_and_retries_later(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service_env = Path(td) / "service_env.json"
            service_env.write_text(json.dumps({"X_SANDBOX_TYPE": "USER"}), encoding="utf-8")
            loader = SandboxContextLoader(
                service_env_path=service_env,
                bashrc_path=Path(td) / "missing-bashrc",
                process_env={},
                retry_seconds=1.0,
            )

            with patch(
                "port_auth.environment.time.monotonic",
                side_effect=[10.0, 10.0, 10.5, 11.1],
            ):
                self.assertIsNone(loader.get())
                service_env.write_text(
                    json.dumps(
                        {"X_SANDBOX_TYPE": "USER", "X_SANDBOX_USER_ID": "new-user"}
                    ),
                    encoding="utf-8",
                )
                self.assertIsNone(loader.get())
                self.assertEqual(loader.get(), SandboxContext("USER", "new-user"))


if __name__ == "__main__":
    unittest.main()
