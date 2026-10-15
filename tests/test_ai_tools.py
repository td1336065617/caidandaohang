"""AI 工具与注入的行为测试（半真实实例，不启动 AstrBot，不需要 pytest-asyncio）。"""

from __future__ import annotations

import asyncio
import json

from conftest import FakeEvent, FakeReq

import main as menu_main


def run(coro):
    return asyncio.run(coro)


def payload(raw: str) -> dict:
    return json.loads(raw)


def test_plugins_menu_summary_lists_commands(make_plugin) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[
            {
                "directory": "acmer_qq_group_bot",
                "menu": "• 最近比赛 ─ 查比赛\n• 每日一题 ─ 每日一题",
                "desc": "竞赛机器人",
            }
        ]
    )
    data = payload(run(plugin.plugins_menu(FakeEvent(), detail="summary")))
    assert data["ok"] is True
    assert "最近比赛" in data["data"]["text"]
    assert data["data"]["count"] >= 2


def test_plugins_menu_full_includes_description(make_plugin) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[
            {"directory": "acmer_qq_group_bot", "menu": "• 最近比赛 ─ 查看未来 N 天比赛", "desc": "竞赛机器人"}
        ]
    )
    data = payload(run(plugin.plugins_menu(FakeEvent(), detail="full")))
    assert data["ok"] is True
    assert "查看未来 N 天比赛" in data["data"]["text"]


def test_plugins_menu_rejects_bad_detail(make_plugin) -> None:
    plugin, _root = make_plugin()
    data = payload(run(plugin.plugins_menu(FakeEvent(), detail="乱写")))
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_argument"
    assert data["error"]["field"] == "detail"


def test_plugins_menu_disabled_by_config(make_plugin) -> None:
    plugin, _root = make_plugin(initial_kv={"menu_ai_settings": {"menu_ai_tools_enabled": False}})
    data = payload(run(plugin.plugins_menu(FakeEvent(), detail="summary")))
    assert data["ok"] is False
    assert data["error"]["code"] == "disabled"


def test_plugins_menu_throttled_after_limit(make_plugin) -> None:
    plugin, _root = make_plugin()
    event = FakeEvent()
    first = payload(run(plugin.plugins_menu(event, detail="summary")))
    second = payload(run(plugin.plugins_menu(event, detail="summary")))
    third = payload(run(plugin.plugins_menu(event, detail="summary")))
    assert first["ok"] is True and second["ok"] is True
    assert third["ok"] is False and third["error"]["code"] == "throttled"
    assert third["error"]["retry_after"] > 0


def test_plugins_intro_matches_display_and_directory(make_plugin) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[{"directory": "acmer_qq_group_bot", "intro": "提供比赛查询与每日一题。", "desc": "竞赛机器人"}]
    )
    by_directory = payload(run(plugin.plugins_intro(FakeEvent(), plugin="acmer_qq_group_bot")))
    assert by_directory["ok"] is True
    assert "比赛查询" in by_directory["data"]["text"]
    by_display = payload(run(plugin.plugins_intro(FakeEvent(), plugin="acmer")))
    assert by_display["ok"] is True


def test_plugins_intro_not_found_returns_candidates(make_plugin) -> None:
    plugin, _root = make_plugin(extra_plugins=[{"directory": "x1", "intro": "甲", "desc": "甲"}])
    data = payload(run(plugin.plugins_intro(FakeEvent(), plugin="不存在的插件")))
    assert data["ok"] is False
    assert data["error"]["code"] == "not_found"
    assert data["error"]["candidates"]


def test_digest_injected_once_and_contains_commands(make_plugin) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[{"directory": "acmer_qq_group_bot", "menu": "• 群排行 ─ 排行", "desc": "竞赛机器人"}]
    )
    req = FakeReq()
    run(plugin.on_llm_request(FakeEvent(), req))
    assert "能力速览" in req.system_prompt
    assert "群排行" in req.system_prompt
    first_len = len(req.system_prompt)
    run(plugin.on_llm_request(FakeEvent(), req))          # 同一请求第二次不应再注入
    assert len(req.system_prompt) == first_len


def test_digest_not_injected_when_disabled(make_plugin) -> None:
    plugin, _root = make_plugin(initial_kv={"menu_ai_settings": {"menu_ai_digest_enabled": False}})
    req = FakeReq()
    run(plugin.on_llm_request(FakeEvent(), req))
    assert req.system_prompt == "你是群助手"


def test_digest_not_injected_for_private_when_scope_group(make_plugin) -> None:
    plugin, _root = make_plugin()
    req = FakeReq()
    run(plugin.on_llm_request(FakeEvent(umo="aiocqhttp:FriendMessage:2000", is_group=False), req))
    assert req.system_prompt == "你是群助手"


def test_digest_scope_all_injects_in_private(make_plugin) -> None:
    plugin, _root = make_plugin(initial_kv={"menu_ai_settings": {"menu_ai_digest_scope": "all"}})
    req = FakeReq()
    run(plugin.on_llm_request(FakeEvent(umo="aiocqhttp:FriendMessage:2000", is_group=False), req))
    assert "能力速览" in req.system_prompt


def test_digest_scope_off_never_injects(make_plugin) -> None:
    plugin, _root = make_plugin(initial_kv={"menu_ai_settings": {"menu_ai_digest_scope": "off"}})
    req = FakeReq()
    run(plugin.on_llm_request(FakeEvent(), req))
    assert req.system_prompt == "你是群助手"


def test_digest_cache_is_used(make_plugin) -> None:
    plugin, _root = make_plugin()
    first = run(plugin._build_ai_digest())
    assert (plugin._cache_dir() / "digest_cache.json").is_file()
    second = run(plugin._build_ai_digest())
    assert first == second and first


def test_digest_excludes_disabled_plugin(make_plugin, monkeypatch) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[{"directory": "dead_plugin", "menu": "• 幽灵指令 ─ 不该出现", "desc": "已停用"}]
    )

    async def fake_disabled() -> set:
        return {"dead_plugin"}

    monkeypatch.setattr(plugin, "_disabled_plugins", fake_disabled)
    digest = run(plugin._build_ai_digest())
    assert "幽灵指令" not in digest


def test_digest_includes_self_even_when_not_selected(make_plugin) -> None:
    plugin, _root = make_plugin(extra_plugins=[{"directory": "other", "menu": "• 别的 ─ x", "desc": "别的"}])
    plugin._enabled_plugins = {"other"}                 # 自己没被勾选
    digest = run(plugin._build_ai_digest())
    assert "菜单" in digest


class FakeRequest:
    """替掉 astrbot.api.web.request（后端 Web API 用它取 JSON）。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self, default=None):
        return self.payload


def test_menu_ai_settings_survive_config_save(make_plugin, monkeypatch) -> None:
    """页面保存必须真的把 6 个 AI 键写进独立 KV（GR-05/GR-06 的可配置性）。"""
    plugin, _root = make_plugin()
    import main as menu_main

    payload = {
        "settings": {"include_all": True, "enabled_plugins": []},
        "ai": {
            "menu_ai_digest_enabled": False,
            "menu_ai_digest_max_chars": 321,
            "menu_ai_digest_scope": "all",
            "menu_ai_tools_enabled": False,
            "menu_ai_tools_max_items": 11,
            "menu_ai_tools_max_chars": 999,
        },
    }
    monkeypatch.setattr(menu_main, "request", FakeRequest(payload))
    run(plugin._web_config_set())
    saved = plugin._kv.data.get(menu_main.AI_SETTINGS_KEY)
    assert isinstance(saved, dict), "AI 配置没写进 KV"
    assert saved["menu_ai_digest_enabled"] is False
    assert saved["menu_ai_digest_max_chars"] == 321
    assert saved["menu_ai_digest_scope"] == "all"
    assert saved["menu_ai_tools_max_items"] == 11
    assert saved["menu_ai_tools_max_chars"] == 999


def test_menu_ai_settings_accept_nested_payload(make_plugin, monkeypatch) -> None:
    """兼容另一种 payload 形态：ai 嵌在 settings 里。"""
    plugin, _root = make_plugin()
    import main as menu_main

    nested = {"menu_ai_tools_enabled": False}
    payload = {"settings": {"include_all": True, "enabled_plugins": [], "ai": nested}}
    monkeypatch.setattr(menu_main, "request", FakeRequest(payload))
    run(plugin._web_config_set())
    assert plugin._kv.data[menu_main.AI_SETTINGS_KEY]["menu_ai_tools_enabled"] is False


def test_snapshot_signature_changes_when_intro_changes(make_plugin) -> None:
    """改了插件介绍（intro.md）必须让摘要指纹变化，否则注入的一直是旧摘要。"""
    plugin, root = make_plugin()
    intro_path = root / menu_main.SELF_DIR_NAME / "intro.md"
    before = plugin._collect_snapshot()[0]
    intro_path.write_text("新的介绍：换了个说法\n", encoding="utf-8")
    after = plugin._collect_snapshot()[0]
    assert before != after, "改 intro 后指纹没变（摘要会一直是旧的）"


def test_throttle_key_isolates_members(make_plugin) -> None:
    """节流维度是每个调用者：同群不同人不该互相连坐。"""
    plugin, _root = make_plugin()
    first = FakeEvent(umo="aiocqhttp:GroupMessage:1000")
    first.get_sender_id = lambda: "user_a"
    second = FakeEvent(umo="aiocqhttp:GroupMessage:1000")
    second.get_sender_id = lambda: "user_b"
    assert plugin._throttle_key(first) != plugin._throttle_key(second)


def test_self_row_kept_when_plugin_dir_differs_from_plugin_name(make_plugin, monkeypatch) -> None:
    """生产实案：插件目录可能不叫 menu_navigation（如 caidandaohang）。

    勾选了部分插件时，自己仍必须参与 AI 摘要（此前用 PLUGIN_NAME 比目录名，生产上恒不相等）。
    """
    monkeypatch.setattr(menu_main, "PLUGIN_NAME", "caidandaohang_renamed")
    plugin, _root = make_plugin()
    plugin._set_enabled_plugins({"other_plugin"})
    rows = asyncio.run(plugin._collect_ai_rows())
    directories = [row["directory"] for row in rows]
    assert menu_main.SELF_DIR_NAME in directories, directories


def test_menu_snapshot_keeps_self_when_plugin_dir_differs(make_plugin, monkeypatch) -> None:
    """同一实案在人类菜单快照上：勾选部分插件时「菜单/插件介绍」入口不能消失。"""
    monkeypatch.setattr(menu_main, "PLUGIN_NAME", "caidandaohang_renamed")
    plugin, _root = make_plugin()
    plugin._set_enabled_plugins({"other_plugin"})
    _signature, text, rows = plugin._collect_snapshot()
    assert "菜单导航" in text
    assert any(
        any("菜单导航" in str(item.get("command") or "") for item in row.get("items") or [])
        for row in rows
    ), rows


def test_plugin_catalog_skips_self_when_plugin_dir_differs(make_plugin, monkeypatch) -> None:
    """WebUI 的插件列表反过来：无论目录叫什么，都不该把自己列进去（避免自己勾自己）。"""
    monkeypatch.setattr(menu_main, "PLUGIN_NAME", "caidandaohang_renamed")
    plugin, _root = make_plugin()
    catalog = plugin._plugin_catalog()
    directories = [row["directory"] for row in catalog]
    assert menu_main.SELF_DIR_NAME not in directories, directories
