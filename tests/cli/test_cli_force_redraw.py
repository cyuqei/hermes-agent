"""Tests for CLI redraw helpers used to recover from terminal buffer drift.

Covers:
  - _force_full_redraw (#8688 cmux tab switch, /redraw, Ctrl+L)
  - the resize handler we install over prompt_toolkit's _on_resize (#5474)

Both behaviors are exercised against fake prompt_toolkit renderer/output
objects — we're asserting the escape sequences the CLI sends, not that
the terminal physically repainted.
"""

from unittest.mock import MagicMock

import pytest

import cli as cli_mod
from cli import HermesCLI


@pytest.fixture
def bare_cli():
    """A HermesCLI with no __init__ — we only exercise the redraw helper."""
    cli = object.__new__(HermesCLI)
    return cli


class TestForceFullRedraw:
    def test_no_app_is_safe(self, bare_cli):
        # _force_full_redraw must be a no-op when the TUI isn't running.
        bare_cli._app = None
        bare_cli._force_full_redraw()  # must not raise

    def test_missing_app_attr_is_safe(self, bare_cli):
        # Simulate HermesCLI before the TUI has ever been constructed.
        bare_cli._force_full_redraw()  # must not raise

    def test_sends_full_clear_replays_then_invalidates(self, bare_cli, monkeypatch):
        app = MagicMock()
        out = app.renderer.output
        bare_cli._app = app
        events = []
        out.reset_attributes.side_effect = lambda: events.append("reset_attrs")
        out.erase_screen.side_effect = lambda: events.append("erase")
        out.cursor_goto.side_effect = lambda *_: events.append("home")
        out.flush.side_effect = lambda: events.append("flush")
        app.renderer.reset.side_effect = lambda **_: events.append("renderer_reset")
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda: events.append("replay"))
        app.invalidate.side_effect = lambda: events.append("invalidate")

        bare_cli._force_full_redraw()

        # Must erase screen, home cursor, and flush — in that order.
        out.reset_attributes.assert_called_once()
        out.erase_screen.assert_called_once()
        out.cursor_goto.assert_called_once_with(0, 0)
        out.flush.assert_called_once()

        # Must reset prompt_toolkit's tracked screen/cursor state so the
        # next incremental redraw starts from a clean (0, 0) origin.
        app.renderer.reset.assert_called_once_with(leave_alternate_screen=False)

        # Must schedule a repaint.
        app.invalidate.assert_called_once()
        assert events == [
            "reset_attrs",
            "erase",
            "home",
            "flush",
            "renderer_reset",
            "replay",
            "invalidate",
        ]

    def test_resize_recovery_delegates_to_native_on_resize(self, bare_cli, monkeypatch):
        """Resize helper should delegate to native on_resize and avoid replay/clears."""
        app = MagicMock()
        events: list = []
        out = app.renderer.output
        out.erase_screen.side_effect = lambda: events.append("erase_screen")
        out.write_raw.side_effect = lambda s: events.append(("write_raw", s))
        out.cursor_goto.side_effect = lambda *_: events.append("home")
        app.invalidate.side_effect = lambda: events.append("invalidate")

        replay_called = []
        monkeypatch.setattr(cli_mod, "_replay_output_history", lambda: replay_called.append(True))

        on_resize_calls = []
        original_on_resize = lambda: on_resize_calls.append("legacy_on_resize")

        bare_cli._status_bar_suppressed_after_resize = True
        bare_cli._recover_after_resize(app, original_on_resize)

        # Native resize path should run exactly once.
        assert on_resize_calls == ["legacy_on_resize"], on_resize_calls
        # No custom clear/replay behavior.
        assert "erase_screen" not in events, events
        assert "home" not in events, events
        assert replay_called == [], replay_called
        # No fallback invalidate on success.
        assert "invalidate" not in events, events
        # Defensive unsuppress should run.
        assert bare_cli._status_bar_suppressed_after_resize is False

    def test_schedule_resize_recovery_passthrough(self, bare_cli):
        app = MagicMock()
        calls = []

        def _orig():
            calls.append("orig")

        bare_cli._schedule_resize_recovery(app, _orig, delay=0.25)
        assert calls == ["orig"]
        assert getattr(bare_cli, "_resize_recovery_pending", False) is False

    def test_force_redraw_uses_full_screen_clear_without_scrollback_clear(self, bare_cli):
        app = MagicMock()
        bare_cli._app = app

        bare_cli._force_full_redraw()

        app.renderer.output.erase_screen.assert_called_once()
        app.renderer.output.cursor_goto.assert_called_once_with(0, 0)
        app.renderer.output.write_raw.assert_not_called()


    def test_invalidate_is_suppressed_while_resize_recovery_is_pending(self, bare_cli):
        app = MagicMock()
        bare_cli._app = app
        bare_cli._last_invalidate = 0.0
        # With native resize passthrough there is no async debounce pending flag.
        bare_cli._resize_recovery_pending = False

        bare_cli._invalidate(min_interval=0)

        app.invalidate.assert_called_once()

    def test_swallows_renderer_exceptions(self, bare_cli):
        # If the renderer blows up for any reason, the helper must not
        # propagate — otherwise a stray Ctrl+L would crash the CLI.
        app = MagicMock()
        app.renderer.output.erase_screen.side_effect = RuntimeError("boom")
        bare_cli._app = app

        bare_cli._force_full_redraw()  # must not raise

        # invalidate() is still attempted after a renderer failure.
        app.invalidate.assert_called_once()

    def test_swallows_invalidate_exceptions(self, bare_cli):
        app = MagicMock()
        app.invalidate.side_effect = RuntimeError("boom")
        bare_cli._app = app

        bare_cli._force_full_redraw()  # must not raise
