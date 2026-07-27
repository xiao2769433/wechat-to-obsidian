---
name: wechat-to-obsidian
description: 把一篇微信公众号文章（mp.weixin.qq.com 链接）抓取并转换为本地 Markdown，同时把正文图片下载到本地 images/ 目录，存入用户的 Obsidian 笔记库。当用户发来微信公众号文章链接，并表达「存一下 / 保存到笔记 / 转成 Markdown / 剪藏到 Obsidian / 保存到知识库」等意图时使用。
version: 0.1.0
---

# 微信公众号文章 -> Obsidian

## 运行
```bash
python scripts/wechat_to_obsidian.py "<URL>"
```
- 输出目录：默认 `<vault>/<wechat_folder>/`（见 `config/settings.json`）；用户指定别的目录用 `--out <绝对路径>`（图片进 `<out>/images/`）。`vault` 未配置时必须用 `--vault` 或 `--out` 指定。
- 参数：`--out` `--vault` `--obsidian`（wikilink）`--no-img` `--overwrite` `--dry-run` `--version`

## 汇报（省 token）
脚本最后只打印一条汇总（`.md` 路径 + 图片成功/失败数）。**成功时直接转述这一条即可**，不要 Read 生成的 `.md`、脚本源码、`README.md`/`docs/design.md`（正文转换在 Python 离线完成，与上下文无关）。先确认再保存用 `--dry-run`。

## 失败时如实告知
- 抓取失败 / 未找到正文：可能需登录或已删除。
- 个别图下载失败：链接失效或防盗链，属正常（脚本列失败 URL）。
- 视频无法下载，原位置插提示。
- 配置字段含义与完整限制见 `README.md`。
