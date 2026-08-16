"""菜单导航插件：把各插件菜单自动聚合到“菜单”指令下。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List, Optional

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

PLUGIN_NAME = "menu_navigation"
# 触发指令（全匹配，避免误伤聊天内容）
MENU_COMMANDS = ("菜单", "菜单导航")
# 单条消息最大长度，超长自动分片
MAX_CHUNK = 1500
# 聚合结果缓存时间（秒）
CACHE_TTL = 60


class MenuNavPlugin(Star):
    """菜单导航：扫描插件目录的 menu.md / metadata.menu 并聚合展示。"""

    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context, config)
        self._cache_ts: float = 0.0
        self._cache_text: Optional[str] = None

    async def initialize(self) -> None:
        logger.info("菜单导航插件 已启动")

    async def terminate(self) -> None:
        pass

    # ------------------------------------------------------------------
    def _plugin_store_path(self) -> Path:
        return Path(get_astrbot_plugin_path())

    def _read_metadata(self, plugin_dir: Path) -> dict:
        for filename in ("metadata.yaml", "metadata.yml"):
            path = plugin_dir / filename
            if path.is_file():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}
        return {}

    def _read_menu(self, plugin_dir: Path) -> Optional[str]:
        """读取插件菜单：优先 menu.md，其次 metadata 的 menu 字段。"""
        menu_file = plugin_dir / "menu.md"
        if menu_file.is_file():
            try:
                text = menu_file.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                pass
        meta = self._read_metadata(plugin_dir)
        menu = meta.get("menu")
        if isinstance(menu, str) and menu.strip():
            return menu.strip()
        if isinstance(menu, list):
            lines = [str(item).strip() for item in menu if str(item).strip()]
            return "\n".join(lines) if lines else None
        return None

    def _collect(self) -> str:
        root = self._plugin_store_path()
        if not root.is_dir():
            return "📋 菜单导航\n（未找到插件目录）"
        parts: List[str] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name == PLUGIN_NAME:
                continue
            text = self._read_menu(child)
            display_name = child.name
            if text is None:
                meta = self._read_metadata(child)
                if meta:
                    display_name = str(
                        meta.get("display_name") or meta.get("name") or child.name
                    )
                    desc = str(meta.get("desc") or "").strip().replace("\n", " ")
                    if desc:
                        text = f"{desc}\n（该插件未提供 menu.md）"
                if text is None:
                    continue
            else:
                meta = self._read_metadata(child)
                if meta:
                    display_name = str(
                        meta.get("display_name") or meta.get("name") or display_name
                    )
            parts.append(f"【{display_name}】\n{text}")
        if not parts:
            return "📋 菜单导航\n（暂未发现提供菜单的插件）"
        return "📋 菜单导航\n" + "\n\n".join(parts)

    def _menu_text(self) -> str:
        now = time.time()
        if self._cache_text is None or now - self._cache_ts > CACHE_TTL:
            self._cache_text = self._collect()
            self._cache_ts = now
        return self._cache_text

    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
        if message_str not in MENU_COMMANDS:
            return
        text = self._menu_text()
        if len(text) <= MAX_CHUNK:
            yield event.plain_result(text)
            return
        start = 0
        while start < len(text):
            end = min(start + MAX_CHUNK, len(text))
            if end < len(text):
                newline = text.rfind("\n", start, end)
                if newline > start + 100:
                    end = newline
            piece = text[start:end].strip()
            if piece:
                yield event.plain_result(piece)
            start = end
