"""隔离用例：本插件单独安装也要完全可用，且不依赖其它插件。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import FakeEvent


def run(coro):
    return asyncio.run(coro)


def test_tools_work_with_no_other_plugin(make_plugin) -> None:
    plugin, _root = make_plugin()                       # 只有 menu_navigation 自己
    data = json.loads(run(plugin.plugins_menu(FakeEvent(), detail="summary")))
    assert data["ok"] is True
    assert data["data"]["count"] >= 1


def test_missing_menu_and_intro_does_not_crash(make_plugin) -> None:
    plugin, root = make_plugin()
    bare = root / "bare_plugin"
    bare.mkdir(parents=True, exist_ok=True)
    (bare / "metadata.yaml").write_text("name: bare\nshort_desc: 只有简述\n", encoding="utf-8")
    data = json.loads(run(plugin.plugins_menu(FakeEvent(), detail="summary")))
    assert data["ok"] is True
    assert "bare" in data["data"]["text"] or "只有简述" in data["data"]["text"]


def test_zero_cross_plugin_imports() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_no_cross_plugin_imports.py"), str(ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_disabled_plugin_excluded_from_digest(make_plugin, monkeypatch) -> None:
    plugin, _root = make_plugin(
        extra_plugins=[{"directory": "dead_one", "menu": "• 幽灵 ─ x", "desc": "停用"}]
    )

    async def fake_disabled() -> set:
        return {"dead_one"}

    monkeypatch.setattr(plugin, "_disabled_plugins", fake_disabled)
    digest = run(plugin._build_ai_digest())
    assert "幽灵" not in digest
