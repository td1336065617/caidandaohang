import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as menu_main


class _FakeEvent:
    def __init__(self, name):
        self._name = name

    def get_platform_name(self):
        return self._name


def test_resolve_channel_official_ws():
    assert menu_main.resolve_channel(_FakeEvent("qq_official")) == "official"


def test_resolve_channel_official_webhook():
    assert menu_main.resolve_channel(_FakeEvent("qq_official_webhook")) == "official"


def test_resolve_channel_onebot():
    assert menu_main.resolve_channel(_FakeEvent("aiocqhttp")) == "onebot"


def test_resolve_channel_other():
    assert menu_main.resolve_channel(_FakeEvent("telegram")) == ""


def test_resolve_channel_error_is_safe():
    class _Boom:
        def get_platform_name(self):
            raise RuntimeError("boom")

    assert menu_main.resolve_channel(_Boom()) == ""


def test_text_limits_by_channel():
    assert menu_main.TEXT_LIMITS["official"] == menu_main.MAX_CHUNK
    assert menu_main.TEXT_LIMITS["onebot"] > menu_main.TEXT_LIMITS["official"]


def test_text_chunks_respects_limit():
    text = "a" * 5000
    pieces = list(menu_main.MenuNavPlugin._text_chunks(text, 1000))
    assert pieces
    assert all(len(p) <= 1000 for p in pieces)
    joined = "".join(p.replace("……（接上条）\n", "") for p in pieces)
    assert joined == text
