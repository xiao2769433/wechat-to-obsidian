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
│   ├── settings.example.json # 配置模板
│   └── settings.json         # 运行时配置（已 gitignore）
├── requirements.txt          # Python 依赖（必装）
├── requirements-optional.txt # 可选加速依赖（lxml，非必须）
├── tests/
│   └── test_wechat_to_obsidian.py # 单元测试
├── LICENSE                   # MIT 许可
├── .gitignore
├── docs/
│   └── design.md             # 本文档
└── README.md                 # 说明文档
```

## 关键设计点
- **图片下载**：带微信 `Referer` 规避 403；优先取懒加载地址 `data-src`；处理 `//` 协议相对与 `data:` 占位图；扩展名由 `wx_fmt`/路径推断，保留原格式。`is_safe_url` 拦截内网/保留地址(SSRF 防护)，`looks_like_image` 按文件头校验。
- **图片复用**：文章目录下（与 `images/` 同级）`.wechat_image_map.json` 记录 URL->本地文件名映射，同文章重复运行不重复下载；读取时过滤失效项，写入有上限。
- **命名**：`{image_prefix}_yyyyMMdd_HHmmss_随机四位数字.{原扩展名}`，同一次抓取共用时间前缀，靠随机串去重；不清空共享 `images/` 目录。
- **覆盖**：`--overwrite` 同名 `.md` 直接覆盖，否则生成 `_1.md`、`_2.md`… 副本。
- **标题**：不写与文件名重复的 `# 文章标题`；正文标题逐级上移（`h2→h1`、`h3→h2`…）。
- **属性**：frontmatter 含 `title` / `source` / `created`(优先页面发布日期 `var ct`/meta，回退当天)。
- **表格**：`<table>` → GFM Markdown（首行表头，colspan 展开，`|` 转义）。
- **公式**：微信 `<span data-formula=" O(n) ">` → `$公式$`（Obsidian/MathJax 可渲染）。
- **代码块**：`<pre>` 经 `pre_to_text` 转纯文本(保留换行/缩进)；输出带语言标识，围栏长度按代码内反引号自适应(`fence_for`)。
- **空行**：`normalize_blanklines()` 合并连续空行、去行尾空白；列表渲染为紧凑形态；代码块(```/~~~ 围栏，含带语言)内原样保留。
- **配置**：所有可配置项集中在 `config/settings.json`，由 `load_settings()` 读取，缺失或异常时回退脚本内缺省值，命令行参数可覆盖。字段：
  - `vault` 笔记库根目录；`wechat_folder` 默认子目录（最终目录 = `vault/wechat_folder`）
  - `image_prefix` 图片名前缀；`obsidian_wikilink` 是否用 `![[...]]`
  - `download_images` 是否下载图片；`request_timeout` 请求超时；`download_retries` 图片重试次数

## 合集（专辑）抓取设计
单篇文章走 `process_note()`；整本合集走 `process_album()`，分层如下：

- `parse_album_target(target, biz)`：解析入参 → `(biz, album_id)`。兼容完整合集 URL、裸 `album_id`、带 `#wechat_redirect` 的链接。
- `_album_api_url(biz, album_id, begin, count)`：**唯一**构造合集接口 URL 的地方，参数名硬编码并加注释保护。
- `_album_fetch_page(url, settings)`：拉取单页 JSON，带重试；非 JSON（被风控返回 HTML）视为失败。
- `fetch_album_items(...)`：游标翻页，直到 `continue_flag == 0` 或无新增条目。
- `process_album(...)`：先取全量条目，再**逐篇复用 `process_note()`** 落盘，保证正文/图片/frontmatter 行为与单篇完全一致，避免两套转换逻辑漂移。

翻页协议（逆向自 `album81568d.js`，**勿改参数名**）：

```
/mp/appmsgalbum?action=getalbum&__biz=<biz>&album_id=<id>&count=10&f=json
  [&begin_msgid=<上页末条msgid>&begin_itemidx=<其itemidx>]
```

- 必须带 `f=json`，否则返回 HTML。
- 翻页靠游标而非页码：`pageidx` / `from_msgid` / `begin` 实测均**无效**（永远返回首页）。
- 终止条件：`getalbum_resp.continue_flag == 0`。

安全阀与容错：
- `ALBUM_MAX_PAGES=100` 上限，防止游标异常导致死循环；「本页无新增条目」也立即停止。
- 页间 `sleep 0.6s`、篇间 `sleep 0.4s` 限速，降低风控概率。
- 中途失败保留已抓到的条目；单篇失败不中断整本，计入 `fail` 列表。
- 完成后生成 `_合集链接-<合集名>.md`（wikilink 清单），生成失败不影响正文。

断点续抓（`--resume`）：
- `_is_article_saved(base, title)` 按「输出目录是否已有 `<清洗标题>.md`」判定，同时匹配重名副本 `<清洗标题>_N.md`。
- 判定发生在**抓取之前**，命中则整篇跳过，不发起文章请求、不下载图片，被跳过项计入 `skipped`（不计 ok/fail）。
- 清单生成时把 `skipped` 与 `ok` 合并排序，保证续抓后的清单仍覆盖整本，不会因跳过而残缺。
- 与 `--overwrite` 互斥生效：`--resume --overwrite` 时以覆盖为准（不跳过）。

## 依赖
需要 Python 3.8+ 与 `requests`、`beautifulsoup4`（见仓库根 `requirements.txt`，首次使用 `pip install -r requirements.txt`）。

## 已知限制
- 微信视频无法下载（原位置插提示）。
- 部分需登录/有访问限制的文章可能抓不到正文。
- 个别图链接失效或被防盗链时下载失败，属正常。
