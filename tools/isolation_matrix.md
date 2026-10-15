# 隔离矩阵（R-IND-01 交付物）

执行方式（可用 `PYTHON=` 指定解释器）：

```bash
PYTHON=/path/to/python bash tools/run_single_plugin.sh menu_navigation
```

| 用例 | 期望 | 结果 | 证据（时间/输出摘要） |
|---|---|---|---|
| 只装本插件启动 | 无异常；插件类可导入；工具已注册 | ✅ | 2026-10-15 21:05，`插件类：[MenuNavPlugin]`；真 AstrBot 日志 `Added llm tool: plugins_menu` / `plugins_intro` |
| 全量单测（隔离副本内） | 全绿 | ✅ | `67 passed` |
| 零跨插件 import | 门禁 exit 0 | ✅ | `零耦合门禁通过（12 个文件）`；植入 `from menu_navigation import x` 时正确转红（exit 1） |
| 不装其它三个插件 | 工具照常工作；速览仍含自身与 metadata 简述 | ✅ | `test_tools_work_with_no_other_plugin`、`test_plugins_menu_summary_lists_commands` |
| 缺失 `menu.md`/`intro.md` | 降级到 `metadata.menu/desc`，不抛异常 | ✅ | `test_missing_menu_and_intro_does_not_crash` |
| 停用本插件（框架侧） | 工具立即消失；无残留 | ✅（框架机制） | 框架按插件启停绑定工具；本插件无后台任务 |
| 停用**其它**插件 | 其指令/介绍从速览消失 | ✅ | `test_digest_excludes_disabled_plugin`、`test_disabled_plugin_excluded_from_digest` |
| 自己未被勾选 | 速览仍包含自己（与菜单一致） | ✅ | `test_digest_includes_self_even_when_not_selected` |

> 说明：本仓库**不依赖**其它三个插件；"不装它们"只是"速览内容更少"，不是"报错"。
