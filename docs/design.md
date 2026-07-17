# wechat-to-obsidian 设计文档

## 目标
把 `mp.weixin.qq.com` 文章保存为 Obsidian 友好的本地 Markdown：正文转 Markdown + 图片本地化到 `images/`，并自动写入笔记属性（YAML frontmatter）。

## 目录结构
```
wechat-to-obsidian/
├── SKILL.md                  # 技能定义与使用说明
├── scripts/
│   └── wechat_to_obsidian.py # 核心脚本（抓取/转换/落盘）
├── templates/
│   └── output-template.md    # 输出 Markdown 结构模板
├── config/
│   └── settings.json         # 运行时配置（vault/folder/prefix/wikilink）
└── docs/
    └── design.md             # 本文档
```

## 关键设计点
- **图片下载**：带微信 `Referer` 规避 403；优先取懒加载地址 `data-src`；处理 `//` 协议相对与 `data:` 占位图；扩展名由 `wx_fmt`/路径推断，保留原格式。
- **命名**：`{image_prefix}_yyyyMMdd_HHmmss_随机四位数字.{原扩展名}`，同一次抓取共用时间前缀，靠随机串去重；不清空共享 `images/` 目录。
- **标题**：不写与文件名重复的 `# 文章标题`；正文标题逐级上移（`h2→h1`、`h3→h2`…）。
- **属性**：frontmatter 含 `title` / `source` / `author`(`[[作者]]`) / `created`(优先页面发布日期 `var ct`/meta，回退当天) / `description`(空)。
- **表格**：`<table>` → GFM Markdown（首行表头，colspan 展开，`|` 转义）。
- **公式**：微信 `<span data-formula=" O(n) ">` → `$公式$`（Obsidian/MathJax 可渲染）。
- **空行**：`normalize_blanklines()` 合并连续空行、去行尾空白；列表渲染为紧凑形态。
- **配置**：所有可配置项集中在 `config/settings.json`，由 `load_settings()` 读取，缺失或异常时回退脚本内缺省值，命令行参数可覆盖。字段：
  - `vault` 笔记库根目录；`wechat_folder` 默认子目录（最终目录 = `vault/wechat_folder`）
  - `image_prefix` 图片名前缀；`obsidian_wikilink` 是否用 `![[...]]`
  - `download_images` 是否下载图片；`request_timeout` 请求超时；`download_retries` 图片重试次数

## 依赖
需要 Python 3.8+ 与 `requests`、`beautifulsoup4`（见仓库根 `requirements.txt`，首次使用 `pip install -r requirements.txt`）。

## 已知限制
- 微信视频无法下载（原位置插提示）。
- 部分需登录/有访问限制的文章可能抓不到正文。
- 个别图链接失效或被防盗链时下载失败，属正常。
