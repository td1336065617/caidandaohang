# 配置说明

菜单导航插件无需配置。

接入方式：

1. 给目标插件根目录添加 `menu.md` 文件，内容为要展示的菜单文本；
2. 或在该插件 `metadata.yaml` 中增加 `menu` 字段（字符串或字符串列表）；
3. 发送 `菜单` 即可看到聚合结果。

聚合时自动跳过菜单导航插件自身，插件目录按名称排序展示。插件只收录
`menu.md`/`metadata.yaml` 中可识别的菜单指令行，不会把完整菜单说明原样拼接；
首次触发或检测到插件菜单有变化时才会生成 HTML 并转成图片。转图会自动尝试
Chromium/Chrome、Firefox、`wkhtmltoimage`，没有浏览器时使用 Pillow；都不可用
才会回退为纯文本分片发送。也可用环境变量 `MENU_NAVIGATION_RENDERER` 指定
渲染器路径，或指定为 `pillow`。
