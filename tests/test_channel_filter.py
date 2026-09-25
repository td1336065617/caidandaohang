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


def test_scope_detector_keeps_real_headers():
    is_scope = menu_main.MenuNavPlugin._is_scope
    assert is_scope("👥 所有人可用") is True
    assert is_scope("🌙 仅管理员") is True
    assert is_scope("⚙️ 仅 AstrBot 管理员") is True
    assert is_scope("🔑 群主 / 群管理员") is True


def test_scope_detector_ignores_command_and_footer_lines():
    """含「管理员」的指令行不能被当成分组标题吞掉（新指令进不了菜单的根因）。"""
    is_scope = menu_main.MenuNavPlugin._is_scope
    assert is_scope(
        "• @某人 绑定cf/牛客/洛谷/atcoder <账号> ─ 管理员代绑定（跳过验证码，仅后台管理员）"
    ) is False
    # 页脚提示行（带冒号）同样不是标题，旧实现会污染后续指令的分组
    assert is_scope(
        "提示：指令为全匹配，群管类指令需群主、群管或 AstrBot 管理员权限"
    ) is False


def test_extract_menu_items_keeps_admin_command_line():
    """回归：管理员分组里的新指令必须被解析出来，而不是被 continue 吞掉。"""
    line = (
        "• @某人 绑定cf/牛客/洛谷/atcoder <账号> ─ "
        "管理员代绑定（跳过验证码，仅后台管理员）"
    )
    items = menu_main.MenuNavPlugin._extract_menu_items(
        "🌙 仅管理员\n• update / 刷新比赛 ─ 强制刷新全部比赛数据\n" + line + "\n"
    )
    commands = [item["command"] for item in items]
    assert commands == [
        "update / 刷新比赛",
        "@某人 绑定cf/牛客/洛谷/atcoder <账号>",
    ]
    assert items[1]["scope"] == "🌙 仅管理员"
    assert items[1]["description"].startswith("管理员代绑定")
