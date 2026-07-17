# wechat-to-obsidian

将微信公众号文章一键剪藏为 Obsidian 友好的本地 Markdown：正文自动转写、图片本地化保存、自动写入 YAML 笔记属性（frontmatter）。

> One-click clipper that saves WeChat Official Account articles as Obsidian-friendly local Markdown — text converted, images localized, frontmatter auto-filled.

## 功能特性

- 抓取 `mp.weixin.qq.com` 文章正文，转换为干净的 Markdown
- 图片本地化：自动下载到 `images/` 目录，带微信 `Referer` 规避 403，优先取懒加载地址 `data-src`
- 自动写入 frontmatter：`title` / `source`(URL) / `author`(`[[作者]]`) / `created` / `description`
- 标题逐级上移（`h2→#`、`h3→##`…），不生成与文件名重复的 `#` 文档标题
- 表格转为 GitHub 风格 Markdown 表格；微信 LaTeX 公式（`<span data-formula>`）包裹为 `$公式$`，可在 Obsidian / MathJax 中渲染
- 支持 Obsidian wikilink 图片格式（`![[图片名]]`，通过 `--obsidian` 或配置开启）
- 自动压缩多余空行、列表渲染为紧凑形态，输出整洁

## 目录结构

```
wechat-to-obsidian/
├── SKILL.md                  # 技能定义与使用说明
├── scripts/
│   └── wechat_to_obsidian.py # 核心脚本（抓取 / 转换 / 落盘）
├── templates/
│   └── output-template.md    # 输出 Markdown 结构模板
├── config/
│   └── settings.json         # 运行时配置（vault / folder / prefix / wikilink）
├── requirements.txt          # Python 依赖
├── docs/
│   └── design.md             # 设计文档
└── README.md                 # 本文件
```

## 环境要求

- Python 3.8 及以上
- 依赖：`requests`、`beautifulsoup4`

## 安装

```bash
git clone https://github.com/<你的用户名>/wechat-to-obsidian.git
cd wechat-to-obsidian
pip install -r requirements.txt
```

也可作为 WorkBuddy 技能使用：把整个目录复制到 `~/.workbuddy/skills/wechat-to-obsidian/` 即可被 WorkBuddy 调用。

## 配置（config/settings.json）

首次使用前需填写你的笔记库路径（或每次用命令行参数指定）：

```json
{
  "vault": "",
  "wechat_folder": "微信文章",
  "image_prefix": "wechat",
  "obsidian_wikilink": false,
  "download_images": true,
  "request_timeout": 30,
  "download_retries": 2
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `vault` | 笔记库根目录，文章默认保存到这里。发布版留空，使用前必须填写，或用 `--vault` / `--out` 指定 |
| `wechat_folder` | 默认子目录名（最终保存目录 = `vault/wechat_folder`） |
| `image_prefix` | 图片文件名前缀，生成形如 `wechat_yyyyMMdd_HHmmss_随机四位.原格式` |
| `obsidian_wikilink` | `true` 时图片用 `![[图片名]]`，否则用 `![](images/图片名)` |
| `download_images` | 是否下载图片（等价于 `--no-img` 的反向） |
| `request_timeout` | 请求超时秒数 |
| `download_retries` | 单张图片下载失败重试次数 |

## 使用方法

基本用法（结果保存到 `vault/wechat_folder/`）：

```bash
python scripts/wechat_to_obsidian.py "https://mp.weixin.qq.com/s/xxxxx"
```

可选参数：

- `--out DIR`：指定 Markdown 输出目录（覆盖默认目录），图片进入 `<out>/images/`
- `--vault DIR`：指定笔记库根目录（覆盖 `settings.json` 的 `vault`）
- `--obsidian`：图片改用 Obsidian wikilink 格式 `![[图片名]]`
- `--no-img`：不下载图片，仅保留原文图片链接

## 输出示例

生成的 Markdown 头部会自动写入如下 frontmatter：

```markdown
---
title: "文章标题"
source: https://mp.weixin.qq.com/s/xxxxx
author:
  - "[[作者名]]"
created: 2026-07-17
description:
---

# 正文首个一级标题

正文段落…

![图片说明](images/wechat_20260717_223000_8421.jpg)
```

## 已知限制

- 微信**视频**无法直接下载，脚本会在原位置插入提示，引导去原文观看。
- 部分公众号文章有访问限制 / 需登录，可能抓不到正文（脚本会提示「未找到正文内容」）。
- 个别图片链接已失效或被防盗链，下载会失败，属正常现象；图片文件名保留原格式，不会清空已有 `images/` 目录，可放心重复运行。

## 技术要点

- 图片真实地址在 `data-src`（懒加载），`src` 多为占位图；脚本优先取 `data-src`。
- 处理 `//` 协议相对地址与 `data:` 占位图；扩展名从 `wx_fmt` 参数或路径后缀推断，保留原图格式。
- 正文标题逐级上移（`h2→#`、`h3→##`…），不生成与文件名重复的 `#` 文档标题。
- 表格转为 GFM Markdown（首行作表头，支持 colspan 展开，单元格内 `|` 自动转义）。

## License

MIT License。
