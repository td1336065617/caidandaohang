# 配置说明

## 配置入口

AstrBot 管理面板 → 插件管理 → **菜单导航**。

## 菜单整合范围

- 默认“整合全部插件”，保持旧版本行为；以后新安装的插件也会自动纳入。
- 关闭“整合全部插件”后，在插件列表中勾选需要整合的插件，保存后立即生效。
- 菜单导航插件自身始终不会被整合，避免递归。
- 没有 `menu.md` 或可识别菜单指令的插件仍可被选择，聚合时会显示其简短描述。

接入方式：

1. 给目标插件根目录添加 `menu.md` 文件，内容为要展示的菜单文本；
2. 或在该插件 `metadata.yaml` 中增加 `menu` 字段（字符串或字符串列表）；
3. 发送 `菜单` 即可看到聚合结果。

聚合时自动跳过菜单导航插件自身，插件目录按名称排序展示。插件只收录
`menu.md`/`metadata.yaml` 中可识别的菜单指令行，不会把完整菜单说明原样拼接；
首次触发、选择范围变化或检测到插件菜单有变化时才会生成 HTML 并转成图片。转图会自动尝试
Chromium/Chrome、Firefox、`wkhtmltoimage`，没有浏览器时使用 Pillow；都不可用
才会回退为纯文本分片发送。也可用环境变量 `MENU_NAVIGATION_RENDERER` 指定
渲染器路径，或指定为 `pillow`。

中文字体会优先选择 `Noto Sans CJK SC` 的简体中文字体面；如需指定字体文件，
可设置 `MENU_NAVIGATION_FONT`，使用 TTC 字体时可通过
`MENU_NAVIGATION_FONT_INDEX` 指定字体面索引（默认自动选择简体中文面）。
Emoji 字体由插件自带 `assets/fonts/NotoColorEmoji.ttf`，不依赖服务器是否安装
Emoji 字体。HTML 渲染会通过本地 `@font-face` 加载它；Pillow 回退会以高分辨率
绘制后缩放，字体不可用时使用安全符号，避免出现方框字形。如需使用其他字体，
可设置 `MENU_NAVIGATION_EMOJI_FONT`，使用 TTC 字体时可通过
`MENU_NAVIGATION_EMOJI_FONT_INDEX` 指定字体面索引。
聚合菜单使用较宽的字间距、行距和卡片留白，中文条目不会紧贴在一起；
排版调整会通过缓存版本自动生效。
