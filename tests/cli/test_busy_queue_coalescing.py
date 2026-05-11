"""Tests for coalescing queued busy-mode CLI messages."""

import queue
import unittest
from types import SimpleNamespace


def _import_cli():
    import hermes_cli.config as config_mod

    if not hasattr(config_mod, "save_env_value_secure"):
        config_mod.save_env_value_secure = lambda key, value: {
            "success": True,
            "stored_as": key,
            "validated": False,
        }

    import cli as cli_mod

    return cli_mod


class TestBusyQueueCoalescing(unittest.TestCase):
    def _make_cli(self, items):
        pending = queue.Queue()
        for item in items:
            pending.put(item)
        return SimpleNamespace(_pending_input=pending)

    def test_coalesces_consecutive_plain_text_messages(self):
        cli_mod = _import_cli()
        stub = self._make_cli(["이렇게", "자꾸", "compression이", "실패해?"])

        combined = cli_mod.HermesCLI._coalesce_pending_busy_queue(stub, "왜")

        self.assertEqual(combined, "왜\n\n이렇게\n\n자꾸\n\ncompression이\n\n실패해?")
        self.assertTrue(stub._pending_input.empty())

    def test_stops_before_slash_command_and_preserves_it(self):
        cli_mod = _import_cli()
        stub = self._make_cli(["이렇게", "/busy", "다음 일반 메시지"])

        combined = cli_mod.HermesCLI._coalesce_pending_busy_queue(stub, "왜")

        self.assertEqual(combined, "왜\n\n이렇게")
        self.assertEqual(stub._pending_input.get_nowait(), "/busy")
        self.assertEqual(stub._pending_input.get_nowait(), "다음 일반 메시지")

    def test_does_not_coalesce_image_payloads(self):
        cli_mod = _import_cli()
        image_payload = ("이미지도 봐줘", ["/tmp/a.png"])
        stub = self._make_cli(["이렇게", image_payload, "다음"])

        combined = cli_mod.HermesCLI._coalesce_pending_busy_queue(stub, "왜")

        self.assertEqual(combined, "왜\n\n이렇게")
        self.assertEqual(stub._pending_input.get_nowait(), image_payload)
        self.assertEqual(stub._pending_input.get_nowait(), "다음")

    def test_slash_command_first_is_not_coalesced(self):
        cli_mod = _import_cli()
        stub = self._make_cli(["뒤의 메시지"])

        combined = cli_mod.HermesCLI._coalesce_pending_busy_queue(stub, "/busy")

        self.assertEqual(combined, "/busy")
        self.assertEqual(stub._pending_input.get_nowait(), "뒤의 메시지")
