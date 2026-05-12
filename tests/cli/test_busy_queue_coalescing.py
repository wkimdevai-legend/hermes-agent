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


class TestIntegratedBusyMode(unittest.TestCase):
    def _make_cli(self, items, *, busy_input_mode="integrated"):
        pending = queue.Queue()
        for item in items:
            pending.put(item)
        return SimpleNamespace(_pending_input=pending, busy_input_mode=busy_input_mode)

    # -- payload tagging helpers -------------------------------------------

    def test_make_and_unwrap_integrated_busy_payload_roundtrip(self):
        cli_mod = _import_cli()
        payload = cli_mod.HermesCLI._make_integrated_busy_payload("hello")
        self.assertTrue(cli_mod.HermesCLI._is_integrated_busy_payload(payload))
        self.assertEqual(cli_mod.HermesCLI._unwrap_integrated_busy_payload(payload), "hello")
        # Non-tagged values pass through unchanged.
        self.assertFalse(cli_mod.HermesCLI._is_integrated_busy_payload("hello"))
        self.assertEqual(cli_mod.HermesCLI._unwrap_integrated_busy_payload("hello"), "hello")
        self.assertFalse(cli_mod.HermesCLI._is_integrated_busy_payload(("text", ["/tmp/a.png"])))
        self.assertFalse(cli_mod.HermesCLI._is_integrated_busy_payload(("integrated_busy", ["/tmp/a.png"])))

    def test_busy_payload_for_mode_tags_text_in_integrated(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="integrated")
        payload, mode = cli_mod.HermesCLI._busy_payload_for_mode(stub, "hello", [])
        self.assertTrue(cli_mod.HermesCLI._is_integrated_busy_payload(payload))
        self.assertEqual(mode, "integrated")

    def test_busy_payload_for_mode_does_not_tag_image_payload(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="integrated")
        payload, mode = cli_mod.HermesCLI._busy_payload_for_mode(stub, "see image", ["/tmp/a.png"])
        self.assertEqual(payload, ("see image", ["/tmp/a.png"]))
        self.assertEqual(mode, "integrated")

    def test_busy_payload_for_mode_passthrough_for_queue(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="queue")
        payload, mode = cli_mod.HermesCLI._busy_payload_for_mode(stub, "hello", [])
        self.assertEqual(payload, "hello")
        self.assertEqual(mode, "queue")

    # -- drain-time preparation -------------------------------------------

    def test_integrated_busy_payload_is_wrapped_after_coalescing(self):
        cli_mod = _import_cli()
        stub = self._make_cli([
            cli_mod.HermesCLI._make_integrated_busy_payload("두번째"),
            cli_mod.HermesCLI._make_integrated_busy_payload("세번째"),
        ])

        first = cli_mod.HermesCLI._make_integrated_busy_payload("첫번째")
        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, first)

        self.assertIn("Additional user input arrived while Hermes was working", prepared)
        self.assertIn("첫번째", prepared)
        self.assertIn("두번째", prepared)
        self.assertIn("세번째", prepared)
        # Order is preserved.
        self.assertLess(prepared.index("첫번째"), prepared.index("두번째"))
        self.assertLess(prepared.index("두번째"), prepared.index("세번째"))
        self.assertTrue(stub._pending_input.empty())

    def test_integrated_mode_does_not_wrap_normal_idle_message(self):
        cli_mod = _import_cli()
        stub = self._make_cli([])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, "정상 입력")

        self.assertEqual(prepared, "정상 입력")

    def test_integrated_single_fragment_is_still_wrapped(self):
        cli_mod = _import_cli()
        stub = self._make_cli([])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
            stub,
            cli_mod.HermesCLI._make_integrated_busy_payload("하나만"),
        )

        self.assertIn("Additional user input arrived while Hermes was working", prepared)
        self.assertIn("하나만", prepared)

    def test_integrated_stops_before_slash_command_payload(self):
        cli_mod = _import_cli()
        stub = self._make_cli([
            cli_mod.HermesCLI._make_integrated_busy_payload("추가"),
            "/busy status",
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        ])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
            stub,
            cli_mod.HermesCLI._make_integrated_busy_payload("처음"),
        )

        self.assertIn("처음", prepared)
        self.assertIn("추가", prepared)
        self.assertNotIn("/busy status", prepared)
        self.assertEqual(stub._pending_input.get_nowait(), "/busy status")
        self.assertEqual(
            stub._pending_input.get_nowait(),
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        )

    def test_integrated_stops_before_image_payload(self):
        cli_mod = _import_cli()
        image_payload = ("이미지도 봐줘", ["/tmp/a.png"])
        stub = self._make_cli([
            cli_mod.HermesCLI._make_integrated_busy_payload("추가"),
            image_payload,
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        ])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
            stub,
            cli_mod.HermesCLI._make_integrated_busy_payload("처음"),
        )

        self.assertIn("처음", prepared)
        self.assertIn("추가", prepared)
        self.assertEqual(stub._pending_input.get_nowait(), image_payload)
        self.assertEqual(
            stub._pending_input.get_nowait(),
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        )

    def test_integrated_slash_command_first_is_not_wrapped(self):
        cli_mod = _import_cli()
        stub = self._make_cli([])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
            stub,
            cli_mod.HermesCLI._make_integrated_busy_payload("/busy status"),
        )

        self.assertEqual(prepared, "/busy status")

    def test_integrated_stops_before_image_payload_with_tag_like_caption(self):
        cli_mod = _import_cli()
        image_payload = ("integrated_busy", ["/tmp/a.png"])
        stub = self._make_cli([
            cli_mod.HermesCLI._make_integrated_busy_payload("추가"),
            image_payload,
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        ])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
            stub,
            cli_mod.HermesCLI._make_integrated_busy_payload("처음"),
        )

        self.assertIn("처음", prepared)
        self.assertIn("추가", prepared)
        self.assertEqual(stub._pending_input.get_nowait(), image_payload)
        self.assertEqual(
            stub._pending_input.get_nowait(),
            cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
        )

    def test_image_payload_with_tag_like_caption_first_is_not_unwrapped(self):
        cli_mod = _import_cli()
        image_payload = ("integrated_busy", ["/tmp/a.png"])
        stub = self._make_cli([])

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, image_payload)

        self.assertEqual(prepared, image_payload)

    def test_non_integrated_payload_keeps_queue_coalescing(self):
        cli_mod = _import_cli()
        stub = self._make_cli(["이렇게", "자꾸"], busy_input_mode="queue")

        prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, "왜")

        self.assertEqual(prepared, "왜\n\n이렇게\n\n자꾸")

    # -- /busy command ----------------------------------------------------

    def test_busy_command_accepts_integrated(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="interrupt")

        calls = []
        old_save = cli_mod.save_config_value
        old_cprint = cli_mod._cprint
        try:
            cli_mod.save_config_value = lambda key, value: calls.append((key, value)) or True
            cli_mod._cprint = lambda *args, **kwargs: None
            cli_mod.HermesCLI._handle_busy_command(stub, "/busy integrated")
        finally:
            cli_mod.save_config_value = old_save
            cli_mod._cprint = old_cprint

        self.assertEqual(stub.busy_input_mode, "integrated")
        self.assertIn(("display.busy_input_mode", "integrated"), calls)

    def test_busy_command_rejects_unknown_mode(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="interrupt")

        old_cprint = cli_mod._cprint
        try:
            cli_mod._cprint = lambda *args, **kwargs: None
            cli_mod.HermesCLI._handle_busy_command(stub, "/busy bogus")
        finally:
            cli_mod._cprint = old_cprint

        self.assertEqual(stub.busy_input_mode, "interrupt")

    def test_busy_status_reports_integrated(self):
        cli_mod = _import_cli()
        stub = SimpleNamespace(busy_input_mode="integrated")

        lines = []
        old_cprint = cli_mod._cprint
        try:
            cli_mod._cprint = lambda text="", *a, **k: lines.append(str(text))
            cli_mod.HermesCLI._handle_busy_command(stub, "/busy status")
        finally:
            cli_mod._cprint = old_cprint

        joined = "\n".join(lines)
        self.assertIn("integrated", joined)
        self.assertIn("integrate", joined.lower())

    def test_busy_command_registry_lists_integrated(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        busy = next(c for c in COMMAND_REGISTRY if c.name == "busy")
        self.assertIn("integrated", busy.subcommands)
        self.assertIn("integrated", busy.args_hint)
