"""菜单导航插件：按需聚合各插件的菜单指令并渲染为图片。"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

PLUGIN_NAME = "menu_navigation"
# 触发指令（全匹配，避免误伤聊天内容）
MENU_COMMANDS = ("菜单", "菜单导航")
# 纯文本兜底时的单条消息最大长度
MAX_CHUNK = 1500
# 渲染失败后的冷却窗口：坏渲染环境下不要每次触发都重试几十秒。
RENDER_FAILURE_COOLDOWN = 300
# PNG 魔数，用于识别损坏/截断图片。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 缓存格式变化时，强制重新生成 HTML 和图片
CACHE_FORMAT_VERSION = 8
# 图片尺寸：使用固定宽度，按内容估算高度，避免 QQ 文本长度限制
RENDER_WIDTH = 1200
MIN_RENDER_HEIGHT = 760
MAX_RENDER_HEIGHT = 12000
SETTINGS_KEY = "settings"
EMOJI_FONT_SIZE = 109
BUNDLED_EMOJI_FONT = (
    Path(__file__).resolve().parent
    / "assets"
    / "fonts"
    / "NotoColorEmoji.ttf"
)

_BULLET_RE = re.compile(r"^\s*(?:[-*+•●▪◦]\s+)")
_SEPARATOR_RE = re.compile(r"\s+(?:[─—–-]{1,3})\s+|\s*[：:]\s*")
_DIVIDER_RE = re.compile(r"^[\s\-_=─—–━]{3,}$")


def _is_valid_png(path: Path) -> bool:
    """基础 PNG 校验：文件存在且以 PNG 魔数开头，排除损坏/空白图。"""
    try:
        if not path.is_file() or path.stat().st_size <= 8:
            return False
        with path.open("rb") as handle:
            return handle.read(8) == PNG_MAGIC
    except OSError:
        return False

# HTML 渲染器和 Pillow 回退统一使用简体中文字体。Pillow 读取 TTC
# 时必须显式指定 SC face（NotoSansCJK 的 index=2），否则默认会加载
# 日文字库面，菜单中文会出现方框或字形错乱。
MENU_FONT_FAMILY = (
    '"Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", '
    '"WenQuanYi Zen Hei", "Microsoft YaHei", sans-serif'
)
EMOJI_FONT_FAMILY = (
    '"Menu Bundled Emoji", "Noto Color Emoji", '
    "sans-serif"
)

EMOJI_FALLBACKS = {
    "👥": "◆",
    "🔑": "◆",
    "🌙": "◆",
    "⚙️": "⚙",
    "🌸": "✦",
    "🎴": "▣",
    "📢": "◆",
    "🔗": "↗",
}

EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2300, 0x23FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
)


def _text_units(value: str) -> List[str]:
    """按近似字素切分，避免把 emoji 的变体选择符拆开。"""
    units: List[str] = []
    for char in str(value):
        if units and (
            unicodedata.combining(char)
            or "\ufe00" <= char <= "\ufe0f"
            or char == "\u20e3"
            or char == "\u200d"
            or units[-1].endswith("\u200d")
        ):
            units[-1] += char
        else:
            units.append(char)
    return units


def _is_emoji_unit(value: str) -> bool:
    """判断一个 grapheme 单元是否需要交给 Emoji 字体渲染。"""
    text = str(value or "")
    if not text:
        return False
    if "\ufe0f" in text or "\u20e3" in text or "\u200d" in text:
        return True
    return any(
        start <= ord(char) <= end
        for char in text
        for start, end in EMOJI_RANGES
    )


def _html_text(value: object) -> str:
    """转义菜单文字，并把 Emoji 单独交给 Emoji 字体。"""
    pieces = []
    for unit in _text_units(str(value or "")):
        escaped = html.escape(unit, quote=True)
        if _is_emoji_unit(unit):
            pieces.append(f'<span class="emoji">{escaped}</span>')
        else:
            pieces.append(escaped)
    return "".join(pieces)


def _tracked_width(draw, value: str, font, tracking: float) -> float:
    units = _text_units(value)
    width = sum(
        draw.textbbox((0, 0), unit, font=font)[2]
        - draw.textbbox((0, 0), unit, font=font)[0]
        for unit in units
    )
    return width + max(0, len(units) - 1) * tracking


def _draw_tracked(draw, xy, value: str, font, fill, tracking: float) -> None:
    cursor = float(xy[0])
    y = xy[1]
    for unit in _text_units(value):
        draw.text((round(cursor), y), unit, font=font, fill=fill)
        box = draw.textbbox((0, 0), unit, font=font)
        cursor += box[2] - box[0] + tracking


class MenuNavPlugin(Star):
    """菜单导航：扫描插件菜单摘要，按需生成并发送菜单图片。"""

    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context, config)
        self._cache_lock = threading.Lock()
        self._cache_signature: Optional[str] = None
        self._cache_text: Optional[str] = None
        self._cache_image: Optional[Path] = None
        # 上次渲染失败的签名与时间：冷却窗口内直接回文本，避免反复重试。
        self._cache_failed_signature: Optional[str] = None
        self._cache_failed_at: Optional[float] = None
        # None 表示整合全部插件；集合表示仅整合指定目录。
        self._enabled_plugins: Optional[set[str]] = None
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_get,
                ["GET"],
                "获取菜单导航配置",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_set,
                ["POST"],
                "保存菜单导航配置",
            )
        except Exception as exc:
            logger.error("菜单导航注册 Web API 失败：%s", exc)

    async def initialize(self) -> None:
        await self._load_settings()
        # 不在启动阶段扫描插件或渲染图片，避免启动变慢；首次触发菜单时再按需处理。
        logger.info("菜单导航插件 已启动（菜单图片按需生成）")

    async def terminate(self) -> None:
        pass

    # ------------------------------------------------------------------
    def _plugin_store_path(self) -> Path:
        return Path(get_astrbot_plugin_path())

    def _cache_dir(self) -> Path:
        # 不额外依赖特定版本的路径 API；插件目录的上级就是 AstrBot/data。
        return self._plugin_store_path().parent / "plugin_data" / PLUGIN_NAME

    def _cache_state_path(self) -> Path:
        return self._cache_dir() / "cache.json"

    def _set_enabled_plugins(self, enabled: Optional[set[str]]) -> None:
        """更新内存筛选条件；只有筛选条件变化时才使图片缓存失效。"""
        normalized = None if enabled is None else set(enabled)
        with self._cache_lock:
            if self._enabled_plugins == normalized:
                return
            self._enabled_plugins = normalized
            self._cache_signature = None
            self._cache_text = None
            self._cache_image = None

    async def _load_settings(self) -> None:
        """从 AstrBot KV 加载筛选设置；没有配置时默认整合全部插件。"""
        try:
            raw = await self.get_kv_data(SETTINGS_KEY, {}) or {}
        except Exception as exc:
            logger.warning("读取菜单导航配置失败，暂时整合全部插件：%s", exc)
            self._set_enabled_plugins(None)
            return
        if not isinstance(raw, dict):
            self._set_enabled_plugins(None)
            return
        include_all = raw.get("include_all", True)
        if not isinstance(include_all, bool):
            include_all = True
        selected = raw.get("enabled_plugins", [])
        if not isinstance(selected, list):
            selected = []
        normalized = {
            str(item).strip()
            for item in selected
            if str(item).strip()
        }
        self._set_enabled_plugins(None if include_all else normalized)

    def _plugin_catalog(self) -> List[Dict[str, object]]:
        """扫描可配置的插件目录，返回后台页面所需的简要信息。"""
        root = self._plugin_store_path()
        if not root.is_dir():
            return []
        catalog: List[Dict[str, object]] = []
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name == PLUGIN_NAME:
                continue
            metadata, _ = self._read_metadata(child)
            menu_text, _ = self._read_menu(child, metadata)
            items = self._extract_menu_items(menu_text) if menu_text else []
            catalog.append(
                {
                    "directory": child.name,
                    "display_name": self._display_name(child, metadata),
                    "has_menu": bool(items),
                    "description": self._short_description(metadata),
                }
            )
        return catalog

    async def _web_config_get(self):
        await self._load_settings()
        catalog = self._plugin_catalog()
        with self._cache_lock:
            enabled = None if self._enabled_plugins is None else set(self._enabled_plugins)
        selected = [
            item["directory"]
            for item in catalog
            if enabled is None or item["directory"] in enabled
        ]
        return json_response(
            {
                "status": "success",
                "data": {
                    "settings": {
                        "include_all": enabled is None,
                        "enabled_plugins": selected,
                    },
                    "plugins": catalog,
                },
            }
        )

    async def _web_config_set(self):
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        settings = payload.get("settings", payload)
        if not isinstance(settings, dict):
            return error_response("settings 必须是对象")

        try:
            current = await self.get_kv_data(SETTINGS_KEY, {}) or {}
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        include_all = settings.get("include_all", current.get("include_all", True))
        if not isinstance(include_all, bool):
            return error_response("include_all 必须是布尔值")
        selected = settings.get("enabled_plugins", current.get("enabled_plugins", []))
        if not isinstance(selected, list):
            return error_response("enabled_plugins 必须是列表")

        known = {
            str(item["directory"])
            for item in self._plugin_catalog()
            if item.get("directory")
        }
        normalized: List[str] = []
        for item in selected:
            directory = str(item).strip()
            if directory in known and directory not in normalized:
                normalized.append(directory)
        try:
            await self.put_kv_data(
                SETTINGS_KEY,
                {
                    "include_all": include_all,
                    "enabled_plugins": normalized,
                },
            )
        except Exception as exc:
            logger.error("保存菜单导航配置失败：%s", exc, exc_info=True)
            return error_response(f"保存失败：{exc}")

        self._set_enabled_plugins(None if include_all else set(normalized))
        return json_response(
            {
                "status": "success",
                "data": {
                    "message": "菜单导航配置已保存并生效",
                    "settings": {
                        "include_all": include_all,
                        "enabled_plugins": normalized,
                    },
                },
            }
        )

    def _read_metadata(self, plugin_dir: Path) -> Tuple[dict, str]:
        """读取 metadata，并返回原始内容用于菜单版本指纹。"""
        for filename in ("metadata.yaml", "metadata.yml"):
            path = plugin_dir / filename
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
                data = yaml.safe_load(raw)
                return (data if isinstance(data, dict) else {}), raw
            except (OSError, UnicodeError, yaml.YAMLError):
                return {}, ""
        return {}, ""

    def _read_menu(self, plugin_dir: Path, metadata: dict) -> Tuple[Optional[str], str]:
        """读取插件菜单：优先 menu.md，其次 metadata 的 menu 字段。"""
        menu_file = plugin_dir / "menu.md"
        if menu_file.is_file():
            try:
                raw = menu_file.read_text(encoding="utf-8")
                text = raw.strip()
                if text:
                    return text, raw
            except (OSError, UnicodeError):
                pass

        menu = metadata.get("menu")
        if isinstance(menu, str) and menu.strip():
            return menu.strip(), json.dumps(menu, ensure_ascii=False)
        if isinstance(menu, list):
            lines = []
            for item in menu:
                if isinstance(item, dict):
                    command = str(
                        item.get("command") or item.get("name") or ""
                    ).strip()
                    description = str(
                        item.get("description") or item.get("desc") or ""
                    ).strip()
                    if command:
                        lines.append(
                            f"{command} ─ {description}" if description else command
                        )
                elif str(item).strip():
                    lines.append(str(item).strip())
            text = "\n".join(lines).strip()
            if text:
                return text, json.dumps(menu, ensure_ascii=False, sort_keys=True)
        return None, ""

    @staticmethod
    def _display_name(plugin_dir: Path, metadata: dict) -> str:
        value = str(
            metadata.get("display_name")
            or metadata.get("name")
            or plugin_dir.name
        )
        return " ".join(value.split())

    @staticmethod
    def _short_description(metadata: dict) -> str:
        value = metadata.get("short_desc") or metadata.get("desc") or ""
        text = " ".join(str(value).split())
        return text[:120] + ("…" if len(text) > 120 else "")

    @staticmethod
    def _is_scope(line: str) -> bool:
        return (
            ("所有人" in line and ("用" in line or "可" in line))
            or "管理员" in line
        )

    @staticmethod
    def _scope_label(line: str) -> str:
        if "管理员" in line:
            return "🌙 仅管理员"
        return "👥 所有人可用"

    @classmethod
    def _extract_menu_items(cls, text: str) -> List[Dict[str, str]]:
        """从 menu.md 中提取指令行，丢弃标题、提示、分隔线等长篇说明。"""
        items: List[Dict[str, str]] = []
        scope = ""
        for raw_line in text.splitlines():
            line = " ".join(raw_line.split()).strip()
            if not line:
                continue
            if cls._is_scope(line):
                scope = cls._scope_label(line)
                continue
            if _DIVIDER_RE.fullmatch(line):
                continue
            if line.startswith(("#", "提示", "说明", "⚙️", "🔗")):
                continue

            has_bullet = bool(_BULLET_RE.match(line))
            if has_bullet:
                line = _BULLET_RE.sub("", line, count=1).strip()
            separator = _SEPARATOR_RE.search(line)
            # README 中允许不写项目符号，但没有分隔符的普通标题不应被当成指令。
            if not has_bullet and separator is None:
                continue
            if not line:
                continue

            command = line
            description = ""
            if separator is not None:
                command = line[: separator.start()].strip()
                description = line[separator.end() :].strip()
            command = command.strip("` ")
            if not command or len(command) > 120:
                continue
            items.append(
                {
                    "scope": scope,
                    "command": command,
                    "description": description[:180]
                    + ("…" if len(description) > 180 else ""),
                }
            )
        return items

    @staticmethod
    def _signature(
        source: List[Dict[str, str]], enabled: Optional[set[str]] = None
    ) -> str:
        payload = json.dumps(
            {
                "format": CACHE_FORMAT_VERSION,
                "enabled_plugins": (
                    None if enabled is None else sorted(enabled)
                ),
                "plugins": source,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _collect_snapshot(self) -> Tuple[str, str, List[Dict[str, object]]]:
        """扫描当前插件菜单，返回指纹、纯文本摘要和 HTML 所需结构。"""
        root = self._plugin_store_path()
        if not root.is_dir():
            text = (
                "🌸 ELYSIAN PINK PEARL · 菜单导航\n"
                "╭──────────────╮\n"
                "（未找到插件目录）\n"
                "╰──────────────╯"
            )
            return self._signature([]), text, []

        source: List[Dict[str, str]] = []
        plugins: List[Dict[str, object]] = []
        enabled = getattr(self, "_enabled_plugins", None)
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name == PLUGIN_NAME:
                continue
            if enabled is not None and child.name not in enabled:
                continue

            metadata, metadata_raw = self._read_metadata(child)
            menu_text, menu_raw = self._read_menu(child, metadata)
            display_name = self._display_name(child, metadata)
            repo = " ".join(str(metadata.get("repo") or "").split())
            items = self._extract_menu_items(menu_text) if menu_text else []
            fallback = self._short_description(metadata)

            # 即使插件暂时没有 menu.md，也把它纳入指纹；以后补充菜单时下一次触发即可更新。
            source.append(
                {
                    "directory": child.name,
                    "metadata": metadata_raw,
                    "menu": menu_raw,
                }
            )
            if not items and not fallback and not repo:
                continue
            plugins.append(
                {
                    "display_name": display_name,
                    "items": items,
                    "fallback": fallback,
                    "repo": repo,
                }
            )

        signature = self._signature(source, enabled)
        if not plugins:
            empty_text = (
                "（暂未发现提供菜单的插件）"
                if not source
                else "（已启用插件均未提供可识别菜单）"
            )
            return (
                signature,
                "🌸 ELYSIAN PINK PEARL · 菜单导航\n"
                "╭──────────────╮\n"
                f"{empty_text}\n"
                "╰──────────────╯",
                [],
            )
        text = self._summary_text(plugins)
        return signature, text, plugins

    @staticmethod
    def _summary_text(plugins: List[Dict[str, object]]) -> str:
        parts = [
            "🌸 ELYSIAN PINK PEARL · 菜单导航",
            "╭──────────────╮",
        ]
        for plugin in plugins:
            parts.append(f"【{plugin['display_name']}】")
            items = plugin["items"]
            if items:
                last_scope = None
                for item in items:
                    scope = item.get("scope") or ""
                    if scope and scope != last_scope:
                        parts.append(scope)
                        last_scope = scope
                    line = f"• {item['command']}"
                    if item.get("description"):
                        line += f" ─ {item['description']}"
                    parts.append(line)
            elif plugin.get("fallback"):
                parts.append(f"• {plugin['fallback']}")
            else:
                parts.append("• （未提供可识别的菜单指令）")
            if plugin.get("repo"):
                parts.append(f"🔗 开源：{plugin['repo']}")
            parts.append("")
        parts.append("╰──────────────╯")
        return "\n".join(parts).rstrip()

    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_render_height(plugins: List[Dict[str, object]]) -> int:
        lines = 5
        for plugin in plugins:
            lines += 3
            items = plugin["items"]
            if items:
                for item in items:
                    content_length = len(item["command"]) + len(item.get("description") or "")
                    lines += max(1, (content_length + 38) // 39)
            else:
                lines += 2
            if plugin.get("repo"):
                lines += 2
        return max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, 120 + lines * 46))

    @staticmethod
    def _html_for_snapshot(plugins: List[Dict[str, object]]) -> str:
        cards: List[str] = []
        bundled_emoji_face = ""
        if BUNDLED_EMOJI_FONT.is_file():
            try:
                font_url = BUNDLED_EMOJI_FONT.as_uri()
            except ValueError:
                font_url = ""
            if font_url:
                bundled_emoji_face = f"""
    @font-face {{
      font-family: "Menu Bundled Emoji";
      src: url("{font_url}") format("truetype");
      font-weight: normal;
      font-style: normal;
      font-display: block;
    }}
"""
        for plugin in plugins:
            rows: List[str] = []
            items = plugin["items"]
            if items:
                last_scope = None
                for item in items:
                    scope = item.get("scope") or ""
                    if scope and scope != last_scope:
                        rows.append(f'<div class="scope">{_html_text(scope)}</div>')
                        last_scope = scope
                    description = item.get("description") or ""
                    rows.append(
                        '<div class="item">'
                        f'<span class="command">{_html_text(item["command"])}</span>'
                        + (
                            f'<span class="description">{_html_text(description)}</span>'
                            if description
                            else ""
                        )
                        + "</div>"
                    )
            elif plugin.get("fallback"):
                rows.append(
                    f'<div class="empty">{_html_text(plugin["fallback"])}</div>'
                )
            else:
                rows.append('<div class="empty">未提供可识别的菜单指令</div>')
            if plugin.get("repo"):
                repo = html.escape(str(plugin["repo"]), quote=True)
                rows.append(
                    f'<div class="repo">{_html_text("🔗 开源：")}<span>{repo}</span></div>'
                )
            cards.append(
                '<section class="plugin">'
                f'<h2>{_html_text(plugin["display_name"])}</h2>'
                f'{"".join(rows)}'
                "</section>"
            )

        body = "".join(cards) or '<div class="empty global">暂未发现提供菜单的插件</div>'
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>菜单导航</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #21162d; }}
    {bundled_emoji_face}
    body {{ color: #4c315b; font-family: {MENU_FONT_FAMILY}; font-variant-east-asian: simplified; text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased; }}
    .emoji {{ font-family: {EMOJI_FONT_FAMILY}; font-variant-emoji: emoji; font-weight: normal; letter-spacing: 0; }}
    .page {{ width: {RENDER_WIDTH}px; margin: 0 auto; padding: 46px 56px 56px; position: relative; overflow: hidden;
      background:
        radial-gradient(circle at 94% 8%, rgba(255,177,218,.28), transparent 24%),
        radial-gradient(circle at 6% 88%, rgba(145,223,247,.22), transparent 26%),
        linear-gradient(135deg,#24142f 0%,#382044 52%,#213847 100%); }}
    .page:before {{ content:""; position:absolute; inset:0; opacity:.2; pointer-events:none;
      background-image:linear-gradient(120deg,transparent 0 46%,rgba(255,255,255,.18) 47%,transparent 48%),
        linear-gradient(60deg,transparent 0 72%,rgba(255,192,226,.12) 73%,transparent 74%);
      background-size:180px 180px,220px 220px; mask-image:linear-gradient(to bottom,black,transparent 90%); }}
    .page:after {{ content:"✿"; position:absolute; right:78px; top:84px; color:rgba(255,225,241,.62);
      font-size:54px; transform:rotate(14deg); pointer-events:none; }}
    .header {{ position:relative; z-index:1; margin-bottom: 34px; }}
    .eyebrow {{ color:#ffd2e9; font-size:14px; font-weight:800; line-height:1.5; letter-spacing:3.6px; text-shadow:0 0 16px rgba(255,170,216,.45); }}
    .title {{ color: #fff7fb; font-size: 38px; font-weight: 900; line-height:1.4; letter-spacing: 1.6px; margin-top:8px;
      text-shadow:0 3px 20px rgba(255,137,195,.45); }}
    .subtitle {{ color: #f2d8e8; font-size: 17px; line-height:1.65; letter-spacing:.3px; margin-top: 10px; }}
    .plugin {{ position:relative; overflow:hidden; margin: 0 0 25px; padding: 27px 32px 30px;
      background:linear-gradient(145deg,rgba(255,252,255,.98),rgba(255,230,244,.93));
      border: 1px solid rgba(255,211,235,.92); border-radius: 18px;
      box-shadow: 0 16px 34px rgba(20,8,35,.28), inset 0 0 26px rgba(255,255,255,.65); }}
    .plugin:before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px;
      background:linear-gradient(#df6c9e,#b4eaf1); box-shadow:0 0 18px rgba(223,108,158,.7); }}
    h2 {{ margin: 0 0 20px; color: #51315d; font-size: 25px; line-height:1.4; font-weight:900; letter-spacing:.5px; }}
    .scope {{ margin: 16px 0 10px; color: #8d5f82; font-size: 16px; line-height:1.5; font-weight: 800; letter-spacing:.45px; }}
    .item {{ display: flex; gap: 18px; align-items: baseline; padding: 8px 0; font-size: 19px; line-height: 1.72; letter-spacing:.3px; }}
    .command {{ color: #c44786; font-weight: 800; letter-spacing:.45px; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .description {{ color: #705276; letter-spacing:.3px; overflow-wrap: anywhere; }}
    .repo {{ margin-top: 18px; color: #7a6a86; font-size: 14px; line-height:1.6; letter-spacing:.25px; overflow-wrap: anywhere; }}
    .repo span {{ color: #6c93a8; }}
    .empty {{ color: #a17a95; font-size: 17px; }}
    .global {{ padding: 30px; background:rgba(255,252,255,.96); border-radius: 16px; }}
  </style>
</head>
<body>
  <main class="page">
    <header class="header">
      <div class="eyebrow">ELYSIAN // PINK PEARL MENU</div>
      <div class="title">{_html_text("🌸 菜单导航")}</div>
      <div class="subtitle">已收录各插件可用指令 · 发送“菜单”查看</div>
    </header>
    {body}
  </main>
</body>
</html>
"""

    @staticmethod
    def _find_renderers() -> List[Tuple[str, str]]:
        """按优先级返回可用的 HTML 渲染器。"""
        configured = (
            os.environ.get("MENU_NAVIGATION_RENDERER")
            or os.environ.get("MENU_NAVIGATION_BROWSER")
            or os.environ.get("MENU_NAVIGATION_CHROME")
        )
        candidates = [configured] if configured else []
        candidates.extend(
            [
                "chromium",
                "chromium-browser",
                "google-chrome",
                "google-chrome-stable",
                "firefox",
                "wkhtmltoimage",
            ]
        )
        renderers: List[Tuple[str, str]] = []
        seen = set()
        for candidate in candidates:
            if not candidate:
                continue
            if candidate.lower() in {"pillow", "pil"}:
                renderers.append(("pillow", "pillow"))
                continue
            path = shutil.which(candidate)
            if not path or path in seen:
                continue
            seen.add(path)
            name = Path(path).name.lower()
            if "firefox" in name:
                kind = "firefox"
            elif "wkhtmltoimage" in name:
                kind = "wkhtmltoimage"
            else:
                # Chromium、Chrome、Edge、Brave 等均支持这组 headless 参数。
                kind = "chromium"
            renderers.append((kind, path))
        return renderers

    @staticmethod
    def _font_index_from_env(default: int = 0) -> int:
        try:
            return max(
                0,
                int(os.environ.get("MENU_NAVIGATION_FONT_INDEX", default)),
            )
        except (TypeError, ValueError):
            return max(0, default)

    @staticmethod
    def _find_cjk_font_spec(*, bold: bool = False) -> Tuple[Optional[str], int]:
        """返回真正的简体中文字体路径及 TTC face index。"""
        configured = os.environ.get("MENU_NAVIGATION_FONT")
        if configured and Path(configured).is_file():
            default_index = (
                2
                if Path(configured).suffix.casefold() == ".ttc"
                and "NotoSansCJK" in Path(configured).name
                else 0
            )
            return configured, MenuNavPlugin._font_index_from_env(default_index)

        fc_match = shutil.which("fc-match")
        if fc_match:
            style = "Bold" if bold else "Regular"
            queries = (
                f"Noto Sans CJK SC:style={style}",
                "Noto Sans CJK SC",
                ":lang=zh-cn",
            )
            for query in queries:
                try:
                    result = subprocess.run(
                        [
                            fc_match,
                            "-f",
                            "%{file}|%{index}",
                            query,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    path_text, _, index_text = (
                        result.stdout.strip().partition("|")
                    )
                    if result.returncode != 0 or not Path(path_text).is_file():
                        continue
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
                except (OSError, subprocess.SubprocessError):
                    break

        if bold:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
            ]
        else:
            candidates = [
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
                ("/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc", 2),
                ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
                ("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf", 0),
            ]
        for path, index in candidates:
            if Path(path).is_file():
                return path, index
        return None, 0

    @staticmethod
    def _find_cjk_font() -> Optional[str]:
        """兼容旧调用方，只返回字体路径。"""
        path, _ = MenuNavPlugin._find_cjk_font_spec()
        return path

    @staticmethod
    def _emoji_font_index_from_env(default: int = 0) -> int:
        try:
            return max(
                0,
                int(
                    os.environ.get(
                        "MENU_NAVIGATION_EMOJI_FONT_INDEX", default
                    )
                ),
            )
        except (TypeError, ValueError):
            return max(0, default)

    @staticmethod
    def _find_emoji_font_spec() -> Tuple[Optional[str], int]:
        """优先使用插件内置 Emoji 字体，再尝试系统字体。"""
        configured = os.environ.get("MENU_NAVIGATION_EMOJI_FONT")
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_file():
                return (
                    str(configured_path),
                    MenuNavPlugin._emoji_font_index_from_env(),
                )

        if BUNDLED_EMOJI_FONT.is_file():
            return str(BUNDLED_EMOJI_FONT), 0

        fc_match = shutil.which("fc-match")
        if fc_match:
            try:
                result = subprocess.run(
                    [
                        fc_match,
                        "-f",
                        "%{file}|%{index}",
                        "Noto Color Emoji",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                path_text, _, index_text = (
                    result.stdout.strip().partition("|")
                )
                if result.returncode == 0 and Path(path_text).is_file():
                    try:
                        index = int(index_text or "0")
                    except ValueError:
                        index = 0
                    return path_text, max(0, index)
            except (OSError, subprocess.SubprocessError):
                pass

        for path in (
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        ):
            if Path(path).is_file():
                return path, 0
        return None, 0

    @staticmethod
    def _find_emoji_font_path() -> Optional[str]:
        """返回可用于 Pillow 的 Emoji 字体路径。"""
        path, _ = MenuNavPlugin._find_emoji_font_spec()
        return path

    @staticmethod
    def _load_emoji_font(image_font_module):
        """加载固定像素面的彩色 Emoji 字体。"""
        path, index = MenuNavPlugin._find_emoji_font_spec()
        if not path:
            return None
        # Noto Color Emoji 在不同 Pillow 版本中通常只能加载固定的
        # CBDT/CBLC 像素面；从首选尺寸开始逐个尝试，兼容其他字体。
        for size in (
            EMOJI_FONT_SIZE,
            128,
            96,
            72,
            64,
            48,
            32,
        ):
            try:
                return image_font_module.truetype(path, size, index=index)
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _run_external_renderer(
        kind: str,
        executable: str,
        html_path: Path,
        image_path: Path,
        height: int,
    ) -> bool:
        if kind == "firefox":
            command = [
                executable,
                "--headless",
                "--no-remote",
                "--screenshot",
                str(image_path),
                "--window-size",
                f"{RENDER_WIDTH},{height}",
                html_path.as_uri(),
            ]
        elif kind == "wkhtmltoimage":
            command = [
                executable,
                "--quiet",
                "--enable-local-file-access",
                "--width",
                str(RENDER_WIDTH),
                "--height",
                str(height),
                html_path.as_uri(),
                str(image_path),
            ]
        else:
            command = [
                executable,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--force-device-scale-factor=1",
                "--allow-file-access-from-files",
                f"--window-size={RENDER_WIDTH},{height}",
                f"--screenshot={image_path}",
                html_path.as_uri(),
            ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            result.returncode == 0
            and image_path.is_file()
            and image_path.stat().st_size > 0
        )

    @classmethod
    def _render_with_pillow(
        cls,
        plugins: List[Dict[str, object]],
        image_path: Path,
    ) -> bool:
        """没有浏览器时，用 Pillow 将同一份摘要绘制为 PNG。"""
        try:
            from PIL import Image as PILImage
            from PIL import ImageDraw, ImageFont
        except ImportError:
            return False

        regular_spec = cls._find_cjk_font_spec()
        bold_spec = cls._find_cjk_font_spec(bold=True)
        if not regular_spec[0] or not bold_spec[0]:
            return False
        try:
            title_font = ImageFont.truetype(
                bold_spec[0], 36, index=bold_spec[1]
            )
            subtitle_font = ImageFont.truetype(
                regular_spec[0], 17, index=regular_spec[1]
            )
            plugin_font = ImageFont.truetype(
                bold_spec[0], 25, index=bold_spec[1]
            )
            scope_font = ImageFont.truetype(
                bold_spec[0], 16, index=bold_spec[1]
            )
            item_font = ImageFont.truetype(
                regular_spec[0], 19, index=regular_spec[1]
            )
            repo_font = ImageFont.truetype(
                regular_spec[0], 14, index=regular_spec[1]
            )
        except (OSError, ValueError):
            return False

        emoji_font = cls._load_emoji_font(ImageFont)
        measure_image = PILImage.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure_image)
        emoji_cache = {}

        def line_height(font) -> int:
            box = measure_draw.textbbox((0, 0), "菜单导航Ag", font=font)
            return max(22, box[3] - box[1] + 8)

        def emoji_target_height(font) -> int:
            font_size = getattr(font, "size", 0)
            if isinstance(font_size, int) and font_size > 0:
                return font_size
            box = measure_draw.textbbox((0, 0), "Ag", font=font)
            return max(1, box[3] - box[1])

        def render_emoji(unit: str, target_height: int):
            """在高分辨率透明画布绘制 Emoji，再缩放到目标字号。"""
            cache_key = (unit, target_height)
            if cache_key in emoji_cache:
                return emoji_cache[cache_key]
            if emoji_font is None:
                emoji_cache[cache_key] = None
                return None
            try:
                bbox = emoji_font.getbbox(unit)
                advance = max(1, int(round(emoji_font.getlength(unit))))
                bbox_width = max(1, bbox[2] - bbox[0], advance)
                bbox_height = max(1, bbox[3] - bbox[1])
                padding = 12
                tile = PILImage.new(
                    "RGBA",
                    (bbox_width + padding * 2, bbox_height + padding * 2),
                    (0, 0, 0, 0),
                )
                tile_draw = ImageDraw.Draw(tile)
                draw_position = (
                    padding - bbox[0],
                    padding - bbox[1],
                )
                try:
                    tile_draw.text(
                        draw_position,
                        unit,
                        font=emoji_font,
                        embedded_color=True,
                    )
                except TypeError:
                    # 兼容旧 Pillow：至少保留字形，不因参数不识别而退回方框。
                    tile_draw.text(
                        draw_position,
                        unit,
                        font=emoji_font,
                    )
                alpha_box = tile.getchannel("A").getbbox()
                if not alpha_box:
                    emoji_cache[cache_key] = None
                    return None
                cropped = tile.crop(alpha_box)
                target_height = max(1, int(target_height))
                target_width = max(
                    1,
                    int(round(cropped.width * target_height / cropped.height)),
                )
                resampling = getattr(PILImage, "Resampling", PILImage)
                resized = cropped.resize(
                    (target_width, target_height),
                    resampling.LANCZOS,
                )
                # 保留 Emoji 的字符进位，窄字形（如 🎴）在进位盒中居中，
                # 避免后面的中文贴得过近。
                scaled_advance = max(
                    target_width,
                    int(
                        round(
                            advance
                            * target_height
                            / max(1, bbox_height)
                        )
                    ),
                )
                result = (resized, scaled_advance)
            except (OSError, ValueError, TypeError):
                result = None
            emoji_cache[cache_key] = result
            return result

        def fallback_unit(unit: str) -> str:
            return EMOJI_FALLBACKS.get(unit, "◆") if _is_emoji_unit(unit) else unit

        def mixed_width(
            value: str,
            font,
            tracking: float,
        ) -> float:
            """按实际中文/Emoji 绘制方式测量一行文字。"""
            widths = []
            target_height = emoji_target_height(font)
            for unit in _text_units(value):
                if _is_emoji_unit(unit):
                    rendered = render_emoji(unit, target_height)
                    if rendered is not None:
                        widths.append(rendered[1])
                        continue
                    unit = fallback_unit(unit)
                box = measure_draw.textbbox((0, 0), unit, font=font)
                widths.append(max(0, box[2] - box[0]))
            return sum(widths) + max(0, len(widths) - 1) * tracking

        def draw_mixed(
            canvas,
            canvas_draw,
            xy,
            value: str,
            font,
            fill,
            tracking: float,
        ) -> None:
            """在同一行中混合绘制普通文字和彩色 Emoji。"""
            cursor = float(xy[0])
            y = xy[1]
            target_height = emoji_target_height(font)
            regular_box = measure_draw.textbbox((0, 0), "Ag", font=font)
            emoji_y = y + regular_box[1]
            for unit in _text_units(value):
                if _is_emoji_unit(unit):
                    rendered = render_emoji(unit, target_height)
                    if rendered is not None:
                        emoji_image, advance = rendered
                        paste_x = round(
                            cursor + max(0, (advance - emoji_image.width) / 2)
                        )
                        canvas.paste(
                            emoji_image,
                            (paste_x, round(emoji_y)),
                            emoji_image,
                        )
                        cursor += advance + tracking
                        continue
                    unit = fallback_unit(unit)
                canvas_draw.text(
                    (round(cursor), y),
                    unit,
                    font=font,
                    fill=fill,
                )
                box = measure_draw.textbbox((0, 0), unit, font=font)
                cursor += box[2] - box[0] + tracking

        def wrap(
            value: str,
            font,
            max_width: int,
            tracking: float,
        ) -> List[str]:
            result: List[str] = []
            for paragraph in (value.splitlines() or [""]):
                current = ""
                for unit in _text_units(paragraph):
                    candidate = current + unit
                    if current and mixed_width(candidate, font, tracking) > max_width:
                        result.append(current)
                        current = unit
                    else:
                        current = candidate
                result.append(current or " ")
            return result

        inner_width = RENDER_WIDTH - 56 * 2 - 28 * 2
        cards = []
        y = (
            42
            + line_height(repo_font)
            + 9
            + line_height(title_font)
            + 8
            + line_height(subtitle_font)
            + 40
        )
        for plugin in plugins:
            rows = [("title", str(plugin["display_name"]), plugin_font, "#51315d")]
            items = plugin["items"]
            if items:
                last_scope = None
                for item in items:
                    scope = item.get("scope") or ""
                    if scope and scope != last_scope:
                        rows.append(("scope", scope, scope_font, "#8d5f82"))
                        last_scope = scope
                    item_text = f"• {item['command']}"
                    if item.get("description"):
                        item_text += f"  ─  {item['description']}"
                    rows.append(("item", item_text, item_font, "#705276"))
            elif plugin.get("fallback"):
                rows.append(("empty", str(plugin["fallback"]), item_font, "#a17a95"))
            else:
                rows.append(("empty", "未提供可识别的菜单指令", item_font, "#a17a95"))
            if plugin.get("repo"):
                rows.append(("repo", f"开源：{plugin['repo']}", repo_font, "#6c93a8"))

            card_top = y
            row_y = card_top + 23
            rendered_rows = []
            for row_kind, value, font, color in rows:
                if row_kind == "title":
                    row_y += 2
                elif row_kind == "scope":
                    row_y += 7
                elif row_kind == "repo":
                    row_y += 9
                tracking = (
                    0.5
                    if row_kind in {"title", "scope"}
                    else 0.25
                    if row_kind == "repo"
                    else 0.35
                )
                lines = wrap(value, font, inner_width, tracking)
                row_height = line_height(font)
                for line in lines:
                    rendered_rows.append(
                        (56 + 32, row_y, line, font, color, tracking)
                    )
                    row_y += row_height
                row_y += 7
            card_bottom = row_y + 25
            cards.append((card_top, card_bottom, rendered_rows))
            y = card_bottom + 20

        image_height = max(MIN_RENDER_HEIGHT, min(MAX_RENDER_HEIGHT, y + 32))
        image = PILImage.new("RGB", (RENDER_WIDTH, image_height), "#2a193b")
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (RENDER_WIDTH - 250, -76, RENDER_WIDTH + 24, 180),
            outline="#f3a9cb",
            width=2,
        )
        draw.ellipse(
            (RENDER_WIDTH - 226, -52, RENDER_WIDTH - 8, 158),
            outline="#b6e7f0",
            width=2,
        )
        _draw_tracked(
            draw,
            (56, 42),
            "ELYSIAN // PINK PEARL MENU",
            repo_font,
            "#ffd5e8",
            0.65,
        )
        title_y = 42 + line_height(repo_font) + 9
        draw_mixed(
            image,
            draw,
            (56, title_y),
            "🌸 菜单导航",
            title_font,
            "#fff7fb",
            1.0,
        )
        _draw_tracked(
            draw,
            (56, title_y + line_height(title_font) + 8),
            "已收录各插件可用指令 · 发送“菜单”查看",
            subtitle_font,
            "#f1d7e7",
            0.35,
        )
        for card_top, card_bottom, rendered_rows in cards:
            draw.rounded_rectangle(
                (56, card_top, RENDER_WIDTH - 56, card_bottom),
                radius=18,
                fill="#fff5fb",
                outline="#efc5dc",
                width=1,
            )
            for x, row_y, value, font, color, tracking in rendered_rows:
                draw_mixed(
                    image,
                    draw,
                    (x, row_y),
                    value,
                    font,
                    color,
                    tracking,
                )
        try:
            image.save(image_path, format="PNG")
        except OSError:
            return False
        return image_path.is_file() and image_path.stat().st_size > 0

    def _render_image(self, plugins: List[Dict[str, object]]) -> Optional[Path]:
        """写入 HTML，并尝试系统可用的 HTML 渲染器转成图片。"""
        cache_dir = self._cache_dir()
        html_path = cache_dir / "menu.html"
        image_path = cache_dir / "menu.png"
        html_tmp = cache_dir / "menu.html.tmp"
        image_tmp = cache_dir / "menu.render.png"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            html_tmp.write_text(self._html_for_snapshot(plugins), encoding="utf-8")
            os.replace(html_tmp, html_path)
        except OSError as exc:
            logger.warning("菜单导航写入 HTML 失败，改用纯文本：%s", exc)
            return None

        image_tmp.unlink(missing_ok=True)
        height = self._estimate_render_height(plugins)
        renderers = self._find_renderers()
        for kind, executable in renderers:
            image_tmp.unlink(missing_ok=True)
            if kind == "pillow":
                success = self._render_with_pillow(plugins, image_tmp)
            else:
                success = self._run_external_renderer(
                    kind, executable, html_path, image_tmp, height
                )
            if success and _is_valid_png(image_tmp):
                os.replace(image_tmp, image_path)
                logger.info("菜单导航已使用 %s 将 HTML 转为图片", kind)
                return image_path
            image_tmp.unlink(missing_ok=True)

        # Pillow 不是外部渲染器候选项，确保没有任何浏览器时也会尝试一次。
        image_tmp.unlink(missing_ok=True)
        if self._render_with_pillow(plugins, image_tmp) and _is_valid_png(
            image_tmp
        ):
            os.replace(image_tmp, image_path)
            logger.info("菜单导航已使用 Pillow 兜底生成图片")
            return image_path

        logger.warning("菜单导航没有可用的 HTML/图片渲染器，改用纯文本发送")
        return None

    def _load_cached_image(self, signature: str) -> Optional[Path]:
        """读取磁盘缓存，允许重启后复用未过期的菜单图片。"""
        state_path = self._cache_state_path()
        image_path = self._cache_dir() / "menu.png"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(state, dict):
            return None
        if (
            state.get("format") != CACHE_FORMAT_VERSION
            or state.get("signature") != signature
            or state.get("image") != image_path.name
        ):
            return None
        try:
            if not _is_valid_png(image_path):
                return None
        except OSError:
            return None
        return image_path

    def _save_cache_state(self, signature: str) -> None:
        state_path = self._cache_state_path()
        state_tmp = state_path.with_suffix(".json.tmp")
        state_tmp.write_text(
            json.dumps(
                {
                    "format": CACHE_FORMAT_VERSION,
                    "signature": signature,
                    "image": "menu.png",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(state_tmp, state_path)

    def _ensure_menu_image(self) -> Tuple[Optional[Path], str]:
        """触发时检查菜单版本；无变化复用图片，有变化才重新渲染。

        渲染/IO 失败都返回 (None, text)，保证必有纯文本兜底；失败后在
        RENDER_FAILURE_COOLDOWN 窗口内不再重复尝试同一签名。
        """
        with self._cache_lock:
            try:
                signature, text, plugins = self._collect_snapshot()
            except Exception as exc:  # noqa: BLE001
                logger.warning("菜单扫描失败，改用纯文本：%s", exc, exc_info=True)
                return None, "（菜单生成失败，请稍后重试）"

            if (
                signature == self._cache_signature
                and self._cache_image is not None
                and self._cache_image.is_file()
            ):
                return self._cache_image, self._cache_text or text

            cached_image = self._load_cached_image(signature)
            if cached_image is not None:
                self._cache_signature = signature
                self._cache_text = text
                self._cache_image = cached_image
                return cached_image, text

            # 空态/无可用插件时不需要渲染图片，直接纯文本。
            if not plugins:
                self._cache_signature = signature
                self._cache_text = text
                self._cache_image = None
                return None, text

            # 同一签名刚失败过且仍在冷却期：不再完整重试（可能各 30s 超时）。
            if (
                signature == self._cache_failed_signature
                and self._cache_failed_at is not None
                and time.monotonic() - self._cache_failed_at
                < RENDER_FAILURE_COOLDOWN
            ):
                return None, text

            try:
                image_path = self._render_image(plugins)
            except Exception as exc:  # noqa: BLE001 - 渲染异常不能吞掉文字回复
                logger.warning("菜单渲染异常，改用纯文本：%s", exc, exc_info=True)
                image_path = None
            self._cache_signature = signature
            self._cache_text = text
            self._cache_image = image_path
            if image_path is not None:
                self._cache_failed_at = None
                self._cache_failed_signature = None
                try:
                    self._save_cache_state(signature)
                except OSError as exc:
                    logger.warning("菜单导航缓存状态保存失败：%s", exc)
            else:
                self._cache_failed_at = time.monotonic()
                self._cache_failed_signature = signature
            return image_path, text

    @staticmethod
    def _text_chunks(text: str):
        """纯文本兜底分片：尽量保持行/空格边界与首行缩进。

        后续分片带“接上条”标记；单条总长不超过 MAX_CHUNK。
        """
        value = str(text or "")
        marker = "……（接上条）\n"
        hard_limit = max(1, MAX_CHUNK - len(marker))
        start = 0
        total = len(value)
        first = True
        while start < total:
            end = min(start + hard_limit, total)
            if end < total:
                newline = value.rfind("\n", start, end)
                space = value.rfind(" ", start, end)
                best = -1
                if newline > start:
                    best = newline + 1
                elif space > start:
                    best = space + 1
                if best > start:
                    end = min(best, start + hard_limit)
            piece = value[start:end].lstrip("\r\n").rstrip("\r\n")
            if piece:
                if first:
                    yield piece
                else:
                    yield marker + piece
                first = False
            start = end

    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
        # QQ 官方指令面板可能自动补上“/”；统一去掉一个前缀后再匹配。
        if message_str.startswith("/"):
            message_str = message_str[1:].lstrip()
        if message_str not in MENU_COMMANDS:
            return

        # 后台配置保存后会立即更新内存；这里再读一次可兼容插件重载后的配置。
        await self._load_settings()
        # 图片渲染是阻塞操作，放到线程中，不阻塞 AstrBot 的事件循环。
        image_path, text = await asyncio.to_thread(self._ensure_menu_image)
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
            return
        for piece in self._text_chunks(text):
            yield event.plain_result(piece)
