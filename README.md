# wechat-to-obsidian

将微信公众号文章一键剪藏为 Obsidian 友好的本地 Markdown：正文自动转写、图片本地化保存、自动写入 YAML 笔记属性（frontmatter）。

> One-click clipper that saves WeChat Official Account articles as Obsidian-friendly local Markdown — text converted, images localized, frontmatter auto-filled.

## 功能特性

- 抓取 `mp.weixin.qq.com` 文章正文，转换为干净的 Markdown
- 图片本地化：自动下载到 `images/` 目录，带微信 `Referer` 规避 403，优先取懒加载地址 `data-src`
- 自动写入 frontmatter：`title` / `source`(URL) / `created`
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
│   ├── settings.example.json # 配置模板（复制为 settings.json 后填写）
│   └── settings.json         # 运行时配置（已 gitignore，本地填写）
├── requirements.txt          # Python 依赖
├── LICENSE                   # MIT 许可（含上游署名）
├── .gitignore
├── docs/
│   └── design.md             # 设计文档
└── README.md                 # 本文件
```

## 环境要求

- Python 3.8 及以上
- 依赖：`requests`、`beautifulsoup4`

## 安装

本仓库有三种使用方式，按需选择。

### 方式一：让 Claude Code 自动安装（推荐）

若你的 Claude Code 版本支持从 Git 仓库一键安装插件 / 技能，直接执行安装命令即可，Claude Code 会自动加载 `SKILL.md`：

```bash
帮我从这个 GitHub 仓库安装 skill：https://github.com/xiao2769433/wechat-to-obsidian
```

### 方式二：手动安装（WorkBuddy / Claude Code）

把仓库克隆到本地后，将目录复制到对应客户端的技能目录：

```bash
git clone https://github.com/xiao2769433/wechat-to-obsidian.git
cd wechat-to-obsidian
pip install -r requirements.txt

# WorkBuddy（用户级）
cp -r wechat-to-obsidian ~/.workbuddy/skills/wechat-to-obsidian

# Claude Code（用户级，所有项目可用）
cp -r wechat-to-obsidian ~/.claude/skills/wechat-to-obsidian

# 或 Claude Code 项目级（仅当前项目）
cp -r wechat-to-obsidian .claude/skills/wechat-to-obsidian
```

- **WorkBuddy**：在对话中给出文章链接并要求「保存到笔记 / 剪藏到 Obsidian」即可触发。
- **Claude Code**：在对话中说「用 wechat-to-obsidian 把 `<链接>` 存到我的 Obsidian」即可触发（`SKILL.md` 的 `description` 已写好意图描述，便于自动匹配；首次运行会请求脚本执行权限）。

### 方式三：本地使用（纯命令行）

不想接入 AI 助手、只想在本地把文章转成 Markdown，直接运行脚本即可：

```bash
git clone https://github.com/xiao2769433/wechat-to-obsidian.git
cd wechat-to-obsidian
pip install -r requirements.txt
python scripts/wechat_to_obsidian.py "https://mp.weixin.qq.com/s/xxxxx"
```

结果默认写入 `config/settings.json` 里 `vault` 配置的笔记库（或每次用 `--vault` / `--out` 指定）。

## 配置（config/settings.json）

`config/settings.json` 已加入 `.gitignore` 不会跟踪。首次使用前从模板复制并填写：

```bash
cp config/settings.example.json config/settings.json
```

然后填写你的笔记库路径（或每次用命令行参数指定）：

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

### 作为 Claude Code Skill 使用（推荐）

在 Claude Code 对话中给出链接并表达保存意图即可触发(由 `SKILL.md` 的 `description` 匹配，并非 slash 命令)：

```
把这篇微信文章存到我的 Obsidian：https://mp.weixin.qq.com/s/xxxxx
```

需要指定目录时，让 Claude 用 `--out <目录>` 或 `--vault <目录>` 参数运行脚本即可。

### 作为本地命令行工具使用

基本用法（结果保存到 `vault/wechat_folder/`）：

```bash
python scripts/wechat_to_obsidian.py "https://mp.weixin.qq.com/s/xxxxx"
```

可选参数：

- `--out DIR`：指定 Markdown 输出目录（覆盖默认目录），图片进入 `<out>/images/`
- `--vault DIR`：指定笔记库根目录（覆盖 `settings.json` 的 `vault`）
- `--obsidian`：图片改用 Obsidian wikilink 格式 `![[图片名]]`
- `--no-img`：不下载图片，仅保留原文图片链接
- `--overwrite`：已存在同名文件时覆盖，而非生成 `_1.md` 副本
- `--version`：显示版本号

## 输出示例

生成的 Markdown 头部会自动写入如下 frontmatter：

```markdown
---
title: "文章标题"
source: https://mp.weixin.qq.com/s/xxxxx
created: 2026-07-17
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
