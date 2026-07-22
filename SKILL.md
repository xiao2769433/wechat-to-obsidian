---
name: wechat-to-obsidian
description: 把一篇微信公众号文章（mp.weixin.qq.com 链接）抓取并转换为本地 Markdown，同时把正文图片下载到本地 images/ 目录，存入用户的 Obsidian 笔记库。当用户发来微信公众号文章链接，并表达「存一下 / 保存到笔记 / 转成 Markdown / 剪藏到 Obsidian / 保存到知识库」等意图时使用。
version: 0.1.0
---

# 微信公众号文章 → Obsidian 本地笔记

把 `mp.weixin.qq.com` 的文章保存成 Obsidian 友好的 Markdown：正文转 Markdown，图片本地化到 `images/`，自动添加 YAML 笔记属性（frontmatter），正文标题逐级上移（不写与文件名重复的 `#` 文档标题）。

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
└── docs/
    └── design.md             # 设计文档
```

## 运行环境

- 需要 **Python 3.8+** 与以下依赖（首次使用前安装）：
  ```bash
  pip install -r requirements.txt
  ```
- 脚本位置：`scripts/wechat_to_obsidian.py`
- 运行时配置集中在 `config/settings.json`（见下方「配置说明」）。所有可配置项都在这里，命令行参数优先于配置。

## 触发条件

用户给出类似 `https://mp.weixin.qq.com/s/xxxxx` 的链接，并要求保存 / 剪藏 / 转 Markdown 到笔记库。

## 执行流程

1. **确认保存位置**（如用户未明确指定）：
   - 默认输出目录：`<vault>/<wechat_folder>/`，图片自动放在该目录下的 `images/`。
   - `vault`（笔记库根目录）与 `wechat_folder`（默认子目录名）都需在使用前于 `config/settings.json` 配置；若 `vault` 留空，则必须通过 `--vault <目录>` 或 `--out <目录>` 指定。
   - 若用户想直接存到别的目录，用 `--out <绝对路径>` 指定（图片会进 `<out>/images/`）。
2. **运行脚本**（在 Bash 中执行，注意用引号包裹含中文 / 特殊字符的路径与 URL）：
   ```bash
   python scripts/wechat_to_obsidian.py "<文章URL>"
   ```
   可选参数：
   - `--out DIR` 指定 Markdown 输出目录（覆盖默认目录）
   - `--vault DIR` 指定笔记库根目录（覆盖 settings.json 的 `vault`）
   - `--obsidian` 图片改用 Obsidian wikilink 格式 `![[图片名]]`（默认是 `![](images/图片名)`）
   - `--no-img` 不下载图片，仅保留原文图片链接
   - `--overwrite` 已存在同名 `.md` 时覆盖原文件，而非生成 `_1.md` 副本
3. **汇报结果**：告诉用户保存的 `.md` 路径、图片数量、以及是否有下载失败的图片（脚本会打印 `图片下载失败: ...`）。

## 配置说明（config/settings.json）

- `vault`：笔记库根目录（默认文章保存到这里）。**发布版为空，使用前必须填你的笔记库路径，或用 `--vault` / `--out` 指定。**
- `wechat_folder`：默认子目录名（最终保存目录 = `vault/wechat_folder`）
- `image_prefix`：图片文件名前缀（如 `wechat` → `wechat_yyyyMMdd_HHmmss_随机四位.原格式`）
- `obsidian_wikilink`：`true` 时图片用 `![[图片名]]`，否则用 `![](images/图片名)`
- `download_images`：是否下载图片（等价于 `--no-img` 的反向）
- `request_timeout`：请求超时秒数
- `download_retries`：单张图片下载失败重试次数

## 已知限制（务必如实告知用户）

- 微信**视频**无法直接下载，脚本会在原位置插入提示，引导去原文观看。
- 个别公众号文章有访问限制 / 需登录，可能抓不到正文（脚本会提示「未找到正文内容」）。
- 图片下载带微信 `Referer` 以规避 403；若某张图仍失败，通常是该图链接已失效或被防盗链，属正常现象。
- 图片文件名按 `wechat_yyyyMMdd_HHmmss_随机四位数字.<原格式>` 生成（保留原图格式，不会强行改成 png），不会清空已有图片目录，可放心重复运行。同文章重复运行时，已下载过的图片通过图片目录下的 `.wechat_image_map.json`（URL → 本地文件名映射）复用，不重复下载。

## 技术要点（便于后续维护）

- 图片真实地址在 `data-src`（懒加载），`src` 多为占位图；脚本优先取 `data-src`。
- 处理 `//` 协议相对地址与 `data:` 占位图；扩展名从 `wx_fmt` 参数或路径后缀推断。
- 正文标题逐级上移（`h2→#`、`h3→##`…），不生成与文件名重复的 `#` 文档标题。
- **笔记属性（frontmatter）**：自动写入 `title` / `source`(URL) / `created`(尽量取页面发布日期，回退当天)。格式见 `templates/output-template.md`。
- 自动压缩正文多余空行（连续空行合并为一行、去除行尾空白），列表渲染为紧凑形态。代码块（``` 围栏内）原样保留，不压缩换行/缩进；`<pre>` 经 `pre_to_text` 转换，保留原始换行并兼容微信高亮代码与 `<span data-formula>` 公式。
- **表格**：`<table>` 转 GFM Markdown 表格（首行作表头，支持 colspan 展开，单元格内 `|` 自动转义）。
- **公式**：微信用 `<span data-formula=" O(n) ">` 表示 LaTeX 公式（前端渲染成图，原文无文字无图），脚本抓取该属性并包成 `$公式$`（Obsidian/MathJax 可渲染）。
