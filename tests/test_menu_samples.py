"""菜单提取回归：项目符号 / Markdown 表格 / 混合（BUG-030）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as menu_main

DATA = Path(__file__).resolve().parent / "fixtures"


def _items(name: str):
    text = (DATA / name).read_text(encoding="utf-8")
    return menu_main.MenuNavPlugin._extract_menu_items(text)


def test_bullet_style_still_works():
    items = _items("menu_bullet.md")
    commands = [i["command"] for i in items]
    assert commands == ["查QQ", "档案馆帮助", "全量同步"]
    assert items[0]["scope"] == "👥 所有人可用"
    assert items[2]["scope"] == "🌙 仅管理员"


def test_table_style_extracts_without_artifact():
    items = _items("menu_table.md")
    commands = [i["command"] for i in items]
    assert commands == ["登录守护状态", "登录守护二维码", "登录守护测试"]
    assert all("|" not in c for c in commands)
    assert items[0]["scope"] == "👥 所有人可用"
    assert items[1]["scope"] == "🌙 仅管理员"
    assert "重发当前二维码" in items[1]["description"]


def test_mixed_style_keeps_order_and_scope():
    items = _items("menu_mixed.md")
    commands = [i["command"] for i in items]
    assert commands == ["表格指令A", "项目符号指令B", "项目符号指令C"]
    assert items[0]["scope"] == "👥 所有人可用"
    assert items[1]["scope"] == "🌙 仅管理员"