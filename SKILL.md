---
name: wechat-to-obsidian
description: 把一篇微信公众号文章（mp.weixin.qq.com 链接）抓取并转换为本地 Markdown，同时把正文图片下载到本地 images/ 目录，存入用户的 Obsidian 笔记库。也支持整本抓取「合集/专辑」链接（appmsgalbum）的全部文章。当用户发来微信公众号文章链接或合集链接，并表达「存一下 / 保存到笔记 / 转成 Markdown / 剪藏到 Obsidian / 保存到知识库 / 整个合集都存」等意图时使用。
version: 0.2.0
---

# 微信公众号文章 -> Obsidian

## 运行

**单篇**：
```bash
python scripts/wechat_to_obsidian.py "<URL>"
```

**整本合集/专辑**（用户给的是 `appmsgalbum` 链接时）：
```bash
python scripts/wechat_to_obsidian.py --album "<合集URL>"
```
- 合集 URL 里必须带 `__biz`；若只有 `album_id`，补 `--album-biz <__biz>`
- 建议先 `--list-only` 确认篇数与清单，再正式执行
- 结束后额外生成链接清单 `_合集链接-<合集名>.md`（含 wikilink）
- 中断后重跑加 `--resume`：跳过输出目录中已存在的文章，不重复抓取/下载

**通用选项**：
- 输出目录：默认 `<vault>/<wechat_folder>/`（见 `config/settings.json`）；用户指定别的目录用 `--out <绝对路径>`（图片进 `<out>/images/`）。`vault` 未配置时必须用 `--vault` 或 `--out` 指定。
- `--out` `--vault` `--obsidian`（wikilink）`--no-img` `--overwrite` `--dry-run` `--album` `--album-biz` `--list-only` `--resume` `--version`

## 合集抓取要点（勿改坏）
微信合集接口翻页靠**游标**，不是页码。逆向自 `album81568d.js`：
- `action=getalbum&__biz=X&album_id=Y&count=10&f=json`
- 下一页：`&begin_msgid=<上页末条msgid>&begin_itemidx=<其itemidx>`
- 必须带 `f=json`（否则返回 HTML）；`pageidx`/`from_msgid`/`begin` 实测**无效**
- 终止条件：`getalbum_resp.continue_flag == 0`

## 汇报（省 token）
脚本最后只打印一条汇总（`.md` 路径 + 图片成功/失败数）。**成功时直接转述这一条即可**，不要 Read 生成的 `.md`、脚本源码、`README.md`/`docs/design.md`（正文转换在 Python 离线完成，与上下文无关）。先确认再保存用 `--dry-run`。
合集模式逐篇打印进度行，最后打印总成功/失败数；篇数多时**不要逐条复述**，只报总数 + 失败项。

## 失败时如实告知
- 抓取失败 / 未找到正文：可能需登录或已删除。
- 个别图下载失败：链接失效或防盗链，属正常（脚本列失败 URL）。
- 视频无法下载，原位置插提示。
- 合集抓取中途失败：保留已抓到的部分并如实报告成功/失败数。
- 配置字段含义与完整限制见 `README.md`。

