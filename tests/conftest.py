"""菜单导航测试夹具：不启动 AstrBot，只造"半真实"插件实例（真实方法 + 内存 KV + 临时插件目录）。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as menu_main


class FakeKV:
    """内存 KV：替掉框架的 get_kv_data / put_kv_data。"""

    def __init__(self, initial: dict | None = None) -> None:
        self.data = dict(initial or {})

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def put(self, key, value):
        self.data[key] = value


class FakeEvent:
    def __init__(self, umo: str = "aiocqhttp:GroupMessage:1000", is_group: bool = True) -> None:
        self.unified_msg_origin = umo
        self.message_str = "菜单"
        self._is_group = is_group

    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType
        return MessageType.GROUP_MESSAGE if self._is_group else MessageType.FRIEND_MESSAGE


class FakeReq:
    def __init__(self, system_prompt: str = "你是群助手") -> None:
        self.system_prompt = system_prompt


def write_plugin(root: Path, directory: str, menu: str = "", intro: str = "", desc: str = "") -> Path:
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    if menu:
        (path / "menu.md").write_text(menu, encoding="utf-8")
    if intro:
        (path / "intro.md").write_text(intro, encoding="utf-8")
    metadata = [
        "name: %s" % directory,
        "display_name: %s" % directory,
        "short_desc: %s" % (desc or "测试插件"),
    ]
    (path / "metadata.yaml").write_text("\n".join(metadata) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def make_plugin(tmp_path):
    """返回 (plugin, store_root)：plugin 的 KV 与插件目录都在 tmp_path 下。"""

    def _build(initial_kv: dict | None = None, extra_plugins: list[dict] | None = None):
        store_root = tmp_path / "data" / "plugins"
        store_root.mkdir(parents=True, exist_ok=True)
        write_plugin(
            store_root,
            "menu_navigation",
            menu="• 菜单 / 菜单导航 ─ 查看聚合菜单\n• 插件介绍 ─ 查看插件介绍",
            desc="菜单导航",
        )
        for item in extra_plugins or []:
            write_plugin(
                store_root,
                item["directory"],
                menu=item.get("menu", ""),
                intro=item.get("intro", ""),
                desc=item.get("desc", "测试插件"),
            )
        plugin = menu_main.MenuNavPlugin.__new__(menu_main.MenuNavPlugin)
        kv = FakeKV(initial_kv)
        plugin.get_kv_data = kv.get
        plugin.put_kv_data = kv.put
        plugin._ai_cache = None
        # 模拟 __init__ 里那批缓存字段（__new__ 会绕过 __init__，保存配置路径需要它们）
        plugin._cache_lock = threading.Lock()
        plugin._cache_signature = None
        plugin._cache_text = None
        plugin._cache_image = None
        plugin._intro_signature = None
        plugin._intro_text = None
        plugin._intro_image = None
        plugin._cache_failed_signature = None
        plugin._cache_failed_at = None
        plugin._intro_failed_signature = None
        plugin._intro_failed_at = None
        plugin._enabled_plugins = None
        plugin._ai_digest_lock = threading.Lock()
        plugin._ai_throttle = menu_main.ai_common.ToolThrottle(limit=2, window=60)
        plugin._plugin_store_path = lambda: store_root
        plugin._cache_dir = lambda: tmp_path / "cache"
        plugin._kv = kv
        return plugin, store_root

    return _build
