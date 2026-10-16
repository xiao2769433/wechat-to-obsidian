#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号文章 -> Obsidian 本地 Markdown（文章正文 + 图片本地化）

改造自 GitHub likemaoke/wechat-article-to-md (MIT)，针对实际使用做了增强：
  * 图片下载带微信 Referer，规避 403 空白图
  * 处理协议相对地址 (//) 与 data: URI 占位图
  * 解析 wx_fmt 得到正确图片扩展名（jpg/png/gif/webp）
  * 图片文件名：<prefix>_yyyyMMdd_HHmmss_随机四位数字.<原格式>；不清空共享图片目录（安全）
  * 默认相对 images/ 引用（适配 Obsidian 与用户笔记习惯）；--obsidian 用 ![[...]]
  * 不写与文件名重复的 # 文档标题；正文标题逐级上移（h2→h1，h3→h2…）
  * 自动添加 Obsidian 笔记属性（YAML frontmatter）：title/source/created
  * 自动压缩多余空行，输出更紧凑

用法:
  python wechat_to_obsidian.py <文章URL> [选项]
  python wechat_to_obsidian.py --album <合集URL或album_id> [选项]
  选项:
    --vault DIR     笔记库根目录（覆盖 settings.json 的 vault；留空则需在 settings.json 配置或用 --out 指定）
    --out DIR       直接指定 Markdown 输出目录（覆盖 --vault 下的默认目录）
    --obsidian      使用 Obsidian wikilink 格式 ![[图片名]] 而非 !(images/x.jpg)
    --no-img        不下载图片，仅保留原文图片链接
    --overwrite     已存在同名文件时覆盖原文件，而非生成 _1.md 副本
    --dry-run       只分析不下载/不写文件，打印预计结果（标题/日期/图片数）
    --album         把「公众号合集/专辑」整本抓取下来（支持合集URL 或 album_id）
    --album-biz BIZ 配合 --album 使用 album_id 时，需同时给出 __biz
    --list-only     仅列出合集/单篇的待处理清单（URL+标题），不抓取正文
"""

import sys
# Windows 默认 stdout 常为 GBK，会让中文汇总变乱码、迫使调用方额外 ls 验证；强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
import re
import argparse
import json
import time
import random
import string
import ipaddress
import socket
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import requests
    from bs4 import BeautifulSoup, Comment
except ImportError as e:
    print(f"缺少依赖库: {e}")
    print("请先安装: pip install requests beautifulsoup4")
    sys.exit(1)


def _session():
    """惰性创建并复用 requests.Session（避免导入期即依赖网络库）。

    trust_env=False：不读取系统/env 代理，避免 Windows 残留的失效代理
    （如已关闭的 Clash 7890）导致 SSLEOFError / ProxyError 直连失败。
    若需走代理，请在系统层正确配置并保证代理可用，或改用支持代理的抓取方式。
    """
    if not hasattr(_session, '_inst'):
        sess = requests.Session()
        sess.trust_env = False
        _session._inst = sess
    return _session._inst


# 以下为「配置缺省值」，仅当 config/settings.json 缺失或某项缺失时兜底使用。
# 实际运行取值全部来自 config/settings.json —— 改配置请改 json，不要改这里。
WECHAT_FOLDER = "微信文章"
IMAGE_PREFIX = "wechat"
REQUEST_TIMEOUT = 30
DOWNLOAD_RETRIES = 2

__version__ = '0.2.0'

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 优先 lxml 解析(更快)，未安装则回退 html.parser
try:
    import lxml  # noqa: F401
    _PARSER = 'lxml'
except ImportError:
    _PARSER = 'html.parser'

VALID_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
# 图片文件头签名，用于校验下载内容确为图片(防恶意内容存为 .jpg)
IMAGE_SIGNATURES = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n',
                    b'GIF87a', b'GIF89a', b'BM')
MIN_IMAGE_BYTES = 200  # 图片最小字节数，过小视为无效(防 1x1 占位图)
MAX_IMAGE_MAP = 2000  # 图片复用映射条目上限，超过则保留最近的一批


# --------------------------------------------------------------------------- #
# 配置与工具函数
# --------------------------------------------------------------------------- #
def load_settings():
    """读取同目录 config/settings.json 作为运行时配置；缺失或异常时回退默认值（静默）。"""
    cfg_path = Path(__file__).resolve().parent.parent / 'config' / 'settings.json'
    defaults = {
        'vault': "",
        'wechat_folder': WECHAT_FOLDER,
        'image_prefix': IMAGE_PREFIX,
        'obsidian_wikilink': False,
        'download_images': True,
        'request_timeout': REQUEST_TIMEOUT,
        'download_retries': DOWNLOAD_RETRIES,
    }
    try:
        with open(cfg_path, encoding='utf-8') as f:
            data = json.load(f)
        for k in defaults:
            if k in data:
                defaults[k] = data[k]
    except Exception:
        pass  # 静默回退默认配置
    return defaults


def sanitize_filename(name: str) -> str:
    """清洗文件名：去掉非法字符，去除首尾空白与小数点。"""
    name = re.sub(r'[<>:"/\\|?*\r\n\t]', '_', name or '')
    name = name.strip('. ')
    return name or 'article'


def yaml_escape(s: str) -> str:
    """转义 YAML 双引号字符串中的反斜杠、双引号与换行，避免破坏 frontmatter。"""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def normalize_img_url(url: str):
    """清洗图片 URL：去掉占位 data: URI，补全协议相对地址。"""
    if not url:
        return None
    url = url.strip()
    if url.startswith('data:'):
        return None
    if url.startswith('//'):
        url = 'https:' + url
    if url.startswith('http'):
        return url
    return None


# 常见云元数据 / 内网管理域名，直接拦截（无需 DNS 解析）
_BLOCKED_HOSTNAMES = frozenset({
    'metadata.google.internal',
    'metadata.internal',
    '169.254.169.254',
})

# 进程内 DNS 校验缓存：同一 host 只解析一次，避免一篇文章多张图重复 getaddrinfo。
# 单进程生命周期内有效；DNS 变更导致的陈旧风险可接受（CLI 单次运行）。
_safe_host_cache = {}


def _is_ip_blocked(ip) -> bool:
    """判断一个 ipaddress 对象是否命中内网/保留地址段。"""
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_safe_url(url: str) -> bool:
    """防 SSRF：仅允许 http/https，且主机非 localhost/内网/保留地址。

    同时拦截字面 IP 与「解析后指向内网」的域名（含云元数据 169.254.169.254）。
    DNS 解析失败时按不安全处理，避免放行无法验证的地址。
    同一 host 的解析结果进程内缓存，避免多张图重复解析。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    if not host or host == 'localhost' or host in _BLOCKED_HOSTNAMES:
        return False
    # 字面 IP：直接判定
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return not _is_ip_blocked(ip)
    # 域名：命中缓存直接返回，否则解析后逐个校验返回地址是否命中内网
    if host in _safe_host_cache:
        return _safe_host_cache[host]
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        _safe_host_cache[host] = False
        return False  # 解析失败，保守拦截
    if not infos:
        _safe_host_cache[host] = False
        return False
    ok = True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            ok = False
            break
        if _is_ip_blocked(ip):
            ok = False
            break
    _safe_host_cache[host] = ok
    return ok


def looks_like_image(data: bytes) -> bool:
    """按文件头签名判断是否为常见图片，防恶意内容伪装成 .jpg。"""
    if not data:
        return False
    if data.startswith(IMAGE_SIGNATURES):
        return True
    return data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def md_escape_url(url: str) -> str:
    """转义 Markdown 链接 url 中的空格与 )，避免破坏图片/链接语法。"""
    return url.replace(' ', '%20').replace(')', '%29')


def img_extension(url: str) -> str:
    """从 wx_fmt 或路径后缀推断图片扩展名，默认 .jpg。"""
    parsed = urlparse(url)
    fmt = parse_qs(parsed.query).get('wx_fmt', [None])[0]
    if fmt:
        fmt = fmt.lower()
        if fmt == 'jpeg':
            return '.jpg'
        if fmt in ('jpg', 'png', 'gif', 'webp', 'bmp'):
            return '.' + fmt
    ext = Path(parsed.path).suffix.lower()
    if ext in VALID_EXTS:
        return '.jpg' if ext == '.jpeg' else ext
    return '.jpg'


def download_image(url: str, img_dir: Path, filename: str, settings) -> bool:
    """带微信 Referer 下载图片，失败按配置重试(指数退避，404 不重试)。

    不打印任何日志，仅返回是否成功，由调用方汇总。
    """
    if not is_safe_url(url):
        return False
    headers = {
        'User-Agent': UA,
        'Referer': 'https://mp.weixin.qq.com/',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    }
    for attempt in range(1 + settings['download_retries']):
        try:
            r = _session().get(url, headers=headers, timeout=settings['request_timeout'])
            if r.status_code == 200 and len(r.content) > MIN_IMAGE_BYTES and looks_like_image(r.content):
                (img_dir / filename).write_bytes(r.content)
                return True
            if r.status_code == 404:
                return False  # 资源不存在，重试无意义
        except Exception:
            pass
        if attempt < settings['download_retries']:
            time.sleep(2 ** attempt)  # 指数退避: 2^attempt 秒（1s, 2s, 4s…，共 download_retries 次）
    return False


def normalize_blanklines(text: str) -> str:
    """压缩正文多余空行：连续空行合并为一行，去除每行行尾空白，去掉首尾空行。
    代码块（``` 围栏内）原样保留，不做任何处理，避免破坏代码换行/缩进。"""
    lines = text.split('\n')
    out = []
    blank = False
    in_fence = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith('```') or stripped.startswith('~~~'):
            in_fence = not in_fence
            out.append(ln)
            blank = False
            continue
        if in_fence:
            out.append(ln)
            blank = False
            continue
        s = ln.rstrip()
        if s == '':
            if not blank:
                out.append('')
                blank = True
        else:
            out.append(s)
            blank = False
    while out and out[0] == '':
        out.pop(0)
    while out and out[-1] == '':
        out.pop()
    return '\n'.join(out)


def pre_to_text(pre_el) -> str:
    """把 <pre> 转成保留原始换行的纯文本，去掉所有 HTML 标签。

    - <br> 与块级元素 (p/section/div/li/h1-6/tr) 处换行
    - 行内 span/code 文本原样保留（兼容微信高亮代码：span 之间若源码含 \\n 一并保留）
    - 微信 LaTeX 公式 (<span data-formula="...">) 包成 $公式$
    """
    block_tags = {'p', 'section', 'div', 'li', 'tr',
                  'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
    out = []

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, str):  # NavigableString
            out.append(node)
            return
        name = node.name
        if name is None:
            return
        # 微信 LaTeX 公式
        formula = node.get('data-formula')
        if formula and formula.strip():
            out.append('$' + formula.strip() + '$')
            return
        if name == 'br':
            out.append('\n')
            return
        if name in block_tags:
            for c in node.children:
                walk(c)
            if not out or out[-1] != '\n':
                out.append('\n')
            return
        # 其他元素（span/code/a/strong…）按内联递归，不补换行
        for c in node.children:
            walk(c)

    walk(pre_el)
    text = ''.join(out)
    # 收敛多余连续空行（3 个及以上 → 2 个），首尾换行清理
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip('\n')
    return text


def fence_for(code: str) -> str:
    """根据代码内最长反引号序列选择足够长的围栏(至少 3 个反引号)。"""
    max_run = run = 0
    for ch in code:
        if ch == '`':
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return '`' * max(3, max_run + 1)


def code_language(pre_el) -> str:
    """尝试从 <pre>/<code> 的 class 提取语言标识(如 python)，取不到返回空串。"""
    code = pre_el.find('code')
    target = code if code else pre_el
    cls = target.get('class') or []
    if isinstance(cls, str):
        cls = cls.split()
    for c in cls:
        for prefix in ('language-', 'brush:', 'lang-'):
            if c.startswith(prefix):
                return c[len(prefix):].strip()
    return ''


def _render_inline(element, img_map, ref, br_sep):
    """渲染元素行内内容为 Markdown(粗体/斜体/链接/图片/公式/br)。

    inline 与 cell_inline 的公共逻辑；br_sep 区分换行符，列表元素跳过(由 render_list 处理)。
    img_map 命中时输出本地引用 ref(fname)，否则保留原始 URL。
    """
    parts = []
    for child in element.children:
        if hasattr(child, 'name') and child.name:
            t = child.name
            if t in ('ul', 'ol'):
                continue
            formula = child.get('data-formula')
            if formula and formula.strip():
                parts.append('$' + formula.strip() + '$')
                continue
            if t in ('b', 'strong'):
                txt = child.get_text(strip=True)
                if txt:
                    parts.append(f"**{txt}**")
            elif t in ('i', 'em'):
                txt = child.get_text(strip=True)
                if txt:
                    parts.append(f"*{txt}*")
            elif t == 'a':
                href = child.get('href', '')
                txt = child.get_text(strip=True)
                if txt:
                    parts.append(f"[{txt}]({href})")
            elif t == 'img':
                url = normalize_img_url(child.get('data-src') or child.get('src', ''))
                alt = child.get('alt', '')
                if url and url in img_map:
                    parts.append(ref(img_map[url]))
                elif url:
                    parts.append(f"![{alt}]({md_escape_url(url)})")
            elif t == 'br':
                parts.append(br_sep)
            else:
                r = _render_inline(child, img_map, ref, br_sep)
                if r:
                    parts.append(r)
        else:
            txt = str(child)
            if txt:
                parts.append(txt)
    return ''.join(parts)


def _table_to_markdown(table_el, cell_inline_fn):
    """把 <table> 转为 GFM Markdown 表格（首行表头；支持 colspan / rowspan 展开）。

    rowspan 通过在网格中留空单元格实现（GFM 无原生 rowspan，留空是通用写法）。
    以 HTML 行号作为网格行号，rowspan 跨占的网格行已由被跨行本身提供，不额外新增行。
    """
    rows = table_el.find_all('tr')
    if not rows:
        return ''
    grid = []  # grid[r][c] = 单元格内容；被跨行占用的格子填 ''
    for ri, tr in enumerate(rows):
        cells = [c for c in tr.find_all(['td', 'th'], recursive=False)]
        if not cells:
            continue
        # 确保网格至少有 ri+1 行（上方 rowspan 可能已预建）
        while len(grid) <= ri:
            grid.append([])
        r = ri
        c = 0
        for cell in cells:
            # 跳过被上方 rowspan 占用的列
            while c < len(grid[r]) and grid[r][c] is not None:
                c += 1
            try:
                colspan = int(cell.get('colspan', 1) or 1)
            except (ValueError, TypeError):
                colspan = 1
            try:
                rowspan = int(cell.get('rowspan', 1) or 1)
            except (ValueError, TypeError):
                rowspan = 1
            content = cell_inline_fn(cell)
            for rr in range(r, r + rowspan):
                while len(grid) <= rr:
                    grid.append([])
                rowref = grid[rr]
                while len(rowref) < c + colspan:
                    rowref.append(None)
                for cc in range(c, c + colspan):
                    rowref[cc] = content if (rr == r and cc == c) else ''
            c += colspan
    ncols = max((len(row) for row in grid), default=0)
    out = []
    for ri, row in enumerate(grid):
        expanded = [(x if x is not None else '') for x in row]
        while len(expanded) < ncols:
            expanded.append('')
        out.append('| ' + ' | '.join(expanded) + ' |')
        if ri == 0:
            out.append('| ' + ' | '.join(['---'] * ncols) + ' |')
    return '\n'.join(out)


def build_frontmatter(title: str, url: str, created: str) -> str:
    """生成 Obsidian YAML frontmatter（title / source / created）。

    title 与 source 均经 yaml_escape，避免含反斜杠/双引号/换行的值破坏 frontmatter。
    """
    safe_title = yaml_escape(title)
    safe_url = yaml_escape(url)
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f"source: {safe_url}\n"
        f"created: {created}\n"
        "---\n"
    )


def extract_publish_date(soup, html_text: str) -> str:
    """尽量从页面提取发布日期（YYYY-MM-DD），失败则用当天（UTC）。"""
    m = soup.find('meta', attrs={'property': 'article:published_time'}) or \
        soup.find('meta', attrs={'itemprop': 'datePublished'})
    if m and m.get('content'):
        return str(m.get('content'))[:10]
    pt = soup.find(id='publish_time')
    if pt and pt.get_text(strip=True):
        return pt.get_text(strip=True)[:10]
    # 多个候选时间戳字段（微信不同模板字段名不一）
    for pattern in (r'var\s+ct\s*=\s*["\']?(\d{10})',
                    r'var\s+oriCreateTime\s*=\s*["\']?(\d{10})',
                    r'var\s+publish_time\s*=\s*["\']?(\d{10})',
                    r'var\s+create_time\s*=\s*["\']?(\d{10})'):
        mm = re.search(pattern, html_text or '')
        if mm:
            try:
                return datetime.fromtimestamp(int(mm.group(1)), timezone.utc).strftime('%Y-%m-%d')
            except Exception:
                pass  # 时间戳解析失败，继续尝试下一个
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


# --------------------------------------------------------------------------- #
# 正文转换（核心）
# --------------------------------------------------------------------------- #
class WechatMarkdownConverter:
    """把微信正文 soup 转为 Markdown，并完成图片本地化与计数。

    设计要点：
      * 图片下载在 convert() 内并发完成，成功/失败计数集中在 img_ok / img_fail；
      * dry_run 模式只统计图片数量，不下载、不写盘；
      * 不打印任何中间日志，结果由调用方统一汇总。
    """

    def __init__(self, base, img_dir, obsidian_mode, download_images, settings, dry_run=False):
        self.base = Path(base)
        self.img_dir = Path(img_dir) if img_dir else None
        self.obsidian_mode = obsidian_mode
        self.download_images = bool(download_images) and (self.img_dir is not None)
        self.settings = settings
        self.dry_run = dry_run
        self.md = []
        self.img_map = {}            # 原图 URL -> 本地文件名
        self.url_to_file = {}        # 历史复用映射（从磁盘载入）
        self.img_total = 0           # 文中图片总数（去重后）
        self.img_ok = 0              # 下载/复用成功数
        self.img_fail = 0            # 下载失败数
        self.img_failed = []         # 失败的原图 URL 列表
        if self.img_dir is not None:
            self._load_reuse_map()

    def _load_reuse_map(self):
        """从 base/.wechat_image_map.json 载入历史图片映射（仅保留本地仍存在的项）。"""
        map_path = self.base / '.wechat_image_map.json'
        if map_path.exists():
            try:
                raw = json.loads(map_path.read_text(encoding='utf-8'))
                self.url_to_file = {u: f for u, f in raw.items()
                                    if (self.img_dir / f).exists()}
            except Exception:
                self.url_to_file = {}

    def ref(self, fname: str) -> str:
        """按模式返回图片引用：Obsidian wikilink 或相对 images/ 链接。"""
        return f"![[{fname}]]" if self.obsidian_mode else f"![](images/{fname})"

    def add_img(self, src, alt=''):
        """追加一张图片引用；若已下载/复用则指向本地文件，否则保留原始 URL。"""
        url = normalize_img_url(src)
        if url and url in self.img_map:
            self.md.append(self.ref(self.img_map[url]) + "\n")
        elif url:
            self.md.append(f"![{alt}]({md_escape_url(url)})\n")

    def render_inline(self, element, br_sep='\n'):
        """渲染行内内容（默认换行符为 \\n）。"""
        return _render_inline(element, self.img_map, self.ref, br_sep)

    def render_list(self, list_el, ordered, depth=0):
        """递归渲染列表(支持嵌套，子级缩进两空格)。"""
        lines = []
        for idx, li in enumerate(list_el.find_all('li', recursive=False), 1):
            marker = f"{'  ' * depth}{idx}. " if ordered else f"{'  ' * depth}- "
            content = self.render_inline(li).strip()
            if content:
                lines.append(marker + content)
            for sub in li.find_all(['ul', 'ol'], recursive=False):
                rendered = self.render_list(sub, sub.name == 'ol', depth + 1)
                if rendered:
                    lines.append(rendered)
        return '\n'.join(lines)

    def cell_inline(self, cell):
        """把单元格内容渲染为单行 Markdown；单元格内 <br> 保留为 <br>（换行不丢失）。"""
        text = _render_inline(cell, self.img_map, self.ref, '<br>')
        text = re.sub(r'[ \t]+', ' ', text).strip()
        return text.replace('|', '\\|')

    def figure(self, node):
        """处理 <figure>：渲染内嵌图片，figcaption 作为图注（斜体）。"""
        img = node.find('img')
        if img:
            self.add_img(img.get('data-src') or img.get('src', ''), img.get('alt', ''))
        cap = node.find('figcaption')
        if cap:
            txt = self.render_inline(cap).strip()
            if txt:
                self.md.append(f"*图：{txt}*\n\n")

    def _collect_images(self, content):
        """收集文中所有图片的原图 URL（去重，C5）。"""
        srcs = []
        seen = set()
        for img in content.find_all('img'):
            src = normalize_img_url(img.get('data-src') or img.get('src', ''))
            if not src or src in seen:
                continue
            seen.add(src)
            srcs.append(src)
        return srcs

    def _download_images(self, srcs):
        """并发下载图片：命中复用映射的跳过下载，重复 URL 本次运行内只下载一次（C5）。"""
        to_download = []
        for src in srcs:
            if src in self.url_to_file and (self.img_dir / self.url_to_file[src]).exists():
                self.img_map[src] = self.url_to_file[src]
                self.img_ok += 1
                continue
            ext = img_extension(src)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            rand4 = ''.join(random.choices(string.digits, k=4))
            fname = f"{self.settings['image_prefix']}_{stamp}_{rand4}{ext}"
            to_download.append((src, fname))
        if not to_download:
            return
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(
                lambda sf: (sf[0], sf[1], download_image(sf[0], self.img_dir, sf[1], self.settings)),
                to_download))
        for src, fname, ok in results:
            if ok:
                self.img_map[src] = fname
                self.url_to_file[src] = fname
                self.img_ok += 1
            else:
                self.img_fail += 1
                self.img_failed.append(src)
        # 回写映射（含本次新下载的），供下次复用
        try:
            if len(self.url_to_file) > MAX_IMAGE_MAP:
                self.url_to_file = dict(list(self.url_to_file.items())[-MAX_IMAGE_MAP:])
            (self.base / '.wechat_image_map.json').write_text(
                json.dumps(self.url_to_file, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass  # 静默：映射写入失败不影响正文

    def traverse(self, el):
        """递归遍历正文节点，按类型追加对应的 Markdown 到 self.md。"""
        for child in el.children:
            if not hasattr(child, 'name') or child.name is None:
                txt = str(child).strip()
                if txt:
                    self.md.append(txt + "\n\n")
                continue
            t = child.name
            if t == 'img':
                self.add_img(child.get('data-src') or child.get('src', ''), child.get('alt', ''))
                continue
            if t == 'iframe':
                self.md.append("\n> 🎥 视频：微信视频无法在 Markdown 中直接播放，请访问原文观看。\n\n")
                continue
            if t in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                level = max(1, int(t[1]) - 1)  # 文档标题已删除，正文标题逐级上移一级
                text = child.get_text(strip=True)
                for im in child.find_all('img'):
                    a = im.get('alt', '')
                    if a and a in text:
                        text = text.replace(a, '')
                text = text.strip()
                if text:
                    self.md.append(f"\n{'#' * level} {text}\n\n")
                for im in child.find_all('img'):
                    self.add_img(im.get('data-src') or im.get('src', ''), im.get('alt', ''))
                continue
            if t == 'p':
                content = self.render_inline(child).strip()
                if content:
                    self.md.append(content + "\n\n")
                continue
            if t == 'ul':
                rendered = self.render_list(child, ordered=False)
                if rendered:
                    self.md.append(rendered + '\n')
                continue
            if t == 'ol':
                rendered = self.render_list(child, ordered=True)
                if rendered:
                    self.md.append(rendered + '\n')
                continue
            if t == 'blockquote':
                txt = self.render_inline(child).strip()
                if txt:
                    quoted = '\n'.join(f"> {ln}" for ln in txt.split('\n') if ln.strip())
                    self.md.append(f"\n{quoted}\n\n")
                continue
            if t == 'pre':
                code = pre_to_text(child)
                if code:
                    lang = code_language(child)
                    fence = fence_for(code)
                    self.md.append(f"\n{fence}{lang}\n{code}\n{fence}\n\n")
                continue
            if t == 'hr':
                self.md.append("\n---\n\n")
                continue
            if t == 'br':
                self.md.append("\n")
                continue
            if t == 'table':
                tbl = _table_to_markdown(child, self.cell_inline)
                if tbl:
                    self.md.append("\n" + tbl + "\n\n")
                continue
            if t == 'figure':
                self.figure(child)
                continue
            # section/div/article/main/span/font 等容器：继续递归
            self.traverse(child)

    def convert(self, content_node):
        """执行转换：先并发下载图片（如需），再遍历生成 Markdown 正文。"""
        self.md = []
        srcs = self._collect_images(content_node)
        self.img_total = len(srcs)
        if self.download_images and not self.dry_run:
            self._download_images(srcs)
        self.traverse(content_node)
        body = ''.join(self.md)
        return normalize_blanklines(body)


def html_to_markdown(soup, img_dir=None, obsidian_mode=False, download_images=False, settings=None):
    """模块级便捷封装：直接把正文 soup 转为 Markdown（不下载图片时 img_dir 传 None）。

    保留此函数以兼容旧调用方式与单测。
    """
    settings = settings or {'image_prefix': IMAGE_PREFIX,
                            'request_timeout': REQUEST_TIMEOUT,
                            'download_retries': DOWNLOAD_RETRIES}
    base = Path(img_dir).parent if img_dir else Path('.')
    conv = WechatMarkdownConverter(base=base, img_dir=img_dir,
                                   obsidian_mode=obsidian_mode,
                                   download_images=download_images,
                                   settings=settings, dry_run=False)
    return conv.convert(soup)


# --------------------------------------------------------------------------- #
# 抓取与落盘
# --------------------------------------------------------------------------- #
def is_wechat_url(url: str) -> bool:
    """判断是否为微信公众号文章链接（用于抓取前的合法性提示）。"""
    try:
        host = urlparse(url).hostname or ''
    except Exception:
        return False
    return (host == 'mp.weixin.qq.com'
            or host.endswith('.mp.weixin.qq.com')
            or host == 'weixin.qq.com')


# --------------------------------------------------------------------------- #
# 合集（专辑）解析
# --------------------------------------------------------------------------- #
ALBUM_COUNT_PER_PAGE = 10   # 微信合集接口单页上限，硬编码在服务端
ALBUM_MAX_PAGES = 100       # 翻页安全上限，防止游标异常导致死循环


def parse_album_target(target: str, biz: str = ''):
    """从合集 URL 或裸 album_id 解析出 (biz, album_id)。

    支持三种输入：
      1. 完整合集 URL：https://mp.weixin.qq.com/mp/appmsgalbum?...__biz=X&album_id=Y
      2. 裸 album_id（需同时通过 biz 参数给出 __biz）
      3. 短链/带 fragment 的合集 URL（#wechat_redirect 之类会被忽略）

    返回 (biz, album_id)；解析失败相应字段为空串。
    """
    target = (target or '').strip()
    if not target:
        return '', ''
    if 'mp.weixin.qq.com' in target or target.startswith('http'):
        try:
            qs = parse_qs(urlparse(target).query)
        except Exception:
            return biz, ''
        got_biz = (qs.get('__biz') or [''])[0]
        got_album = (qs.get('album_id') or [''])[0]
        return (got_biz or biz), got_album
    # 裸 album_id
    return biz, target


def _album_api_url(biz: str, album_id: str, begin=None, count: int = ALBUM_COUNT_PER_PAGE) -> str:
    """构造合集 JSON 接口 URL。

    翻页协议（逆向自 album81568d.js，勿随意改动参数名）：
      * action=getalbum&__biz=<biz>&album_id=<id>&count=N&f=json
      * 翻下一页：begin_msgid=<上一页最后一条 msgid>&begin_itemidx=<其 itemidx>
      * 必须带 f=json，否则返回 HTML
    无效参数（实测翻页无效）：pageidx / from_msgid / begin
    """
    url = (f"https://mp.weixin.qq.com/mp/appmsgalbum?action=getalbum"
           f"&__biz={biz}&album_id={album_id}&count={count}&f=json")
    if begin:
        url += f"&begin_msgid={begin[0]}&begin_itemidx={begin[1]}"
    return url


def _album_fetch_page(url: str, settings) -> dict:
    """拉取一页合集数据，返回 getalbum_resp 字典；失败返回空字典（含重试）。"""
    headers = {
        'User-Agent': UA,
        'Accept': 'application/json,text/plain,*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    for attempt in range(1 + settings['download_retries']):
        try:
            r = _session().get(url, headers=headers, timeout=settings['request_timeout'])
            body = (r.text or '').lstrip()
            if body.startswith('{'):
                data = json.loads(body)
                return data.get('getalbum_resp') or {}
        except Exception:
            pass
        if attempt < settings['download_retries']:
            time.sleep(2 ** attempt)
    return {}


def fetch_album_items(album_target: str, settings, biz: str = '') -> dict:
    """抓取整个合集的全部文章条目（游标翻页直到 continue_flag==0）。

    返回 dict：
      {'items': [{'n','title','url','msgid','itemidx','create_time'}...],
       'error': None 或 错误说明,
       'album_title': 合集名}
    """
    biz, album_id = parse_album_target(album_target, biz)
    if not album_id:
        return {'items': [], 'error': '无法从输入解析出 album_id，请给出合集完整 URL 或 --album <album_id> + --album-biz <__biz>', 'album_title': ''}
    if not biz:
        return {'items': [], 'error': '缺少 __biz，请在合集 URL 中保留 __biz 参数，或用 --album-biz <__biz> 指定', 'album_title': ''}

    items = []
    seen = set()
    cursor = None
    album_title = ''
    first_page = True

    for page_no in range(ALBUM_MAX_PAGES):
        resp = _album_fetch_page(_album_api_url(biz, album_id, cursor), settings)
        if not resp:
            if first_page:
                return {'items': [], 'error': '合集接口无响应（可能链接失效、合集已删除，或被风控拦截）', 'album_title': ''}
            break  # 中途失败：保留已抓到的部分
        lst = resp.get('article_list') or []
        if isinstance(lst, dict):   # 单条时接口可能返回对象而非数组
            lst = [lst]
        if not lst:
            break
        if first_page:
            base_info = resp.get('base_info') or {}
            album_title = (base_info.get('title') or '').strip()
            first_page = False

        new_cnt = 0
        for it in lst:
            key = (it.get('msgid'), it.get('itemidx'))
            if key in seen:
                continue
            seen.add(key)
            items.append({
                'n': len(items) + 1,
                'title': (it.get('title') or '').strip(),
                'url': (it.get('url') or '').replace('&amp;', '&'),
                'msgid': it.get('msgid'),
                'itemidx': it.get('itemidx'),
                'create_time': it.get('create_time') or '',
            })
            new_cnt += 1

        last = lst[-1]
        cursor = (last.get('msgid'), last.get('itemidx'))
        # continue_flag == 0 表示没有下一页；无新增也视为结束，避免死循环
        if str(resp.get('continue_flag', '0')) == '0' or new_cnt == 0:
            break
        time.sleep(0.6)  # 轻微限速，降低被风控概率

    if not items:
        return {'items': [], 'error': '合集解析成功但未取到任何文章', 'album_title': album_title}
    return {'items': items, 'error': None, 'album_title': album_title}


def _fetch_article(url, settings):
    """抓取文章，返回 (soup, title, pub_date, content, error)。

    不打印任何日志；出错时通过 error 字符串由调用方汇总。
    """
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    try:
        r = _session().get(url, headers=headers, timeout=settings['request_timeout'])
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        return None, None, None, None, f'抓取失败：{e}'

    soup = BeautifulSoup(r.text, _PARSER)

    # 标题
    meta = soup.find('meta', property='og:title')
    if meta:
        title = meta.get('content', '').strip() or '无标题'
    else:
        h = soup.find('h1', class_='rich_media_title')
        title = h.get_text(strip=True) if h else '无标题'

    # 正文（C1: 不再提取/打印 author）
    content = soup.find('div', class_='rich_media_content') or soup.find('div', id='js_content')
    if not content:
        return soup, title, None, None, '未找到正文内容（可能需登录或已删除）'

    pub_date = extract_publish_date(soup, r.text)
    return soup, title, pub_date, content, None


def process_note(url, settings, vault='', obsidian_mode=False, out_dir=None,
                 download_images=True, overwrite=False, dry_run=False):
    """抓取并转换一篇文章，返回结果字典（含路径/图片计数/错误）。

    该函数在成功/失败时都通过返回值携带信息，由 main 统一打印最终汇总。
    """
    result = {'error': None, 'path': None, 'img_ok': 0, 'img_fail': 0,
              'img_failed': [], 'dry_run': dry_run, 'title': None,
              'created': None, 'images': 0}

    soup, title, pub_date, content, error = _fetch_article(url, settings)
    result['title'] = title
    result['created'] = pub_date
    if error:
        result['error'] = error
        return result

    base = Path(out_dir) if out_dir else (Path(vault) / settings['wechat_folder'])
    try:
        base.mkdir(parents=True, exist_ok=True)
        img_dir = base / 'images' if download_images else None
        if img_dir is not None:
            img_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result['error'] = f'无法创建输出目录 {base}：{e}'
        return result

    conv = WechatMarkdownConverter(base=base, img_dir=img_dir,
                                   obsidian_mode=obsidian_mode,
                                   download_images=download_images,
                                   settings=settings, dry_run=dry_run)

    # 笔记属性（YAML frontmatter，格式见 templates/output-template.md）
    frontmatter = build_frontmatter(title, url, pub_date)

    if dry_run:
        # 仍执行一次 convert 以统计图片（不下载）
        conv.convert(content)
        result['images'] = conv.img_total
        return result

    # convert() 内部已 normalize_blanklines；img_total 在 convert 后才确定
    body = conv.convert(content)
    result['images'] = conv.img_total
    out = frontmatter + "\n" + body + "\n"

    fname = sanitize_filename(title) + '.md'
    fpath = base / fname
    if not overwrite:
        i = 1
        while fpath.exists():
            fpath = base / f"{sanitize_filename(title)}_{i}.md"
            i += 1
    try:
        fpath.write_text(out, encoding='utf-8')
    except OSError as e:
        result['error'] = f'写入文件失败：{e}'
        return result

    result['path'] = str(fpath)
    result['img_ok'] = conv.img_ok
    result['img_fail'] = conv.img_fail
    result['img_failed'] = conv.img_failed
    return result


def process_album(album_target, settings, vault='', obsidian_mode=False, out_dir=None,
                  download_images=True, overwrite=False, dry_run=False, biz='', progress=True):
    """整本抓取合集：先解析全部文章链接，再逐篇转换落盘。

    返回 dict：
      {'album_title','total','ok':[...],'fail':[...],'error':None,
       'img_ok':N,'img_fail':N,'index_path':路径或None}
    ok 每项：{'n','title','path','img_ok','img_fail'}
    fail 每项：{'n','title','url','error'}
    """
    meta = fetch_album_items(album_target, settings, biz=biz)
    res = {'album_title': meta.get('album_title') or '', 'total': 0,
           'ok': [], 'fail': [], 'error': meta.get('error'),
           'img_ok': 0, 'img_fail': 0, 'index_path': None}
    if res['error']:
        return res

    items = meta['items']
    res['total'] = len(items)
    if progress:
        print(f"合集：{res['album_title'] or '(未命名)'}  共 {len(items)} 篇")

    base = Path(out_dir) if out_dir else (Path(vault) / settings['wechat_folder'])

    for it in items:
        # 复用单篇逻辑，保证正文/图片/frontmatter 行为完全一致
        r = process_note(it['url'], settings, vault, obsidian_mode, out_dir,
                         download_images, overwrite, dry_run)
        if r.get('error'):
            res['fail'].append({'n': it['n'], 'title': it['title'],
                                'url': it['url'], 'error': r['error']})
            if progress:
                print(f"[{it['n']:>3}/{len(items)}] ❌ {it['title'][:40]} :: {r['error'][:60]}")
            continue
        res['ok'].append({'n': it['n'], 'title': r.get('title') or it['title'],
                          'path': r.get('path'), 'img_ok': r.get('img_ok', 0),
                          'img_fail': r.get('img_fail', 0)})
        res['img_ok'] += r.get('img_ok', 0)
        res['img_fail'] += r.get('img_fail', 0)
        if progress:
            tag = '预计' if dry_run else '✅'
            print(f"[{it['n']:>3}/{len(items)}] {tag} {(r.get('title') or it['title'])[:40]}")
        time.sleep(0.4)  # 篇间限速，降低风控概率

    # 生成链接清单，便于日后单篇重取或核对
    if not dry_run and res['ok']:
        try:
            base.mkdir(parents=True, exist_ok=True)
            idx_name = sanitize_filename(f"_合集链接-{res['album_title'] or '合集'}") + '.md'
            idx_path = base / idx_name
            lines = [f"# {res['album_title'] or '合集'} - 链接清单（{len(res['ok'])} 篇）", '',
                     f"抓取时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", '']
            for o in sorted(res['ok'], key=lambda x: x['n']):
                lines.append(f"{o['n']}. [[{sanitize_filename(o['title'])}]]")
            idx_path.write_text('\n'.join(lines), encoding='utf-8')
            res['index_path'] = str(idx_path)
        except Exception:
            pass  # 清单生成失败不影响正文结果

    return res


def main():
    parser = argparse.ArgumentParser(
        prog='wechat_to_obsidian.py',
        description='微信公众号文章 -> Obsidian 本地 Markdown(正文 + 图片本地化)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('url', nargs='?', help='微信文章 URL (mp.weixin.qq.com)')
    parser.add_argument('--vault', help='笔记库根目录(覆盖 settings.json 的 vault)')
    parser.add_argument('--out', help='直接指定 Markdown 输出目录(覆盖 vault 下的默认目录)')
    parser.add_argument('--obsidian', action='store_true', help='图片改用 Obsidian wikilink ![[图片名]]')
    parser.add_argument('--no-img', action='store_true', help='不下载图片，仅保留原文图片链接')
    parser.add_argument('--overwrite', action='store_true', help='已存在同名文件时覆盖，而非生成 _1.md 副本')
    parser.add_argument('--dry-run', action='store_true', help='只分析不下载/不写文件，打印预计结果')
    parser.add_argument('--album', help='抓取整本合集/专辑：传合集 URL 或 album_id')
    parser.add_argument('--album-biz', default='', help='配合 --album 传未含 __biz 的 album_id 时使用')
    parser.add_argument('--list-only', action='store_true', help='仅列出待处理清单(URL+标题)，不抓正文')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()

    if not args.url and not args.album:
        parser.print_help()
        sys.exit(1)

    # 运行时配置：优先 config/settings.json，可被命令行参数覆盖
    settings = load_settings()
    obsidian_mode = settings.get('obsidian_wikilink', False) or args.obsidian
    download_images = settings.get('download_images', True) and not args.no_img
    vault = args.vault or settings['vault']
    out_dir = args.out

    if not vault and not out_dir:
        print("错误：未配置笔记库根目录 vault。")
        print("请在 config/settings.json 设置 \"vault\"，或用 --vault <目录> 指定，或用 --out <目录> 直接指定输出位置。")
        sys.exit(1)

    # ---------- 合集模式 ----------
    if args.album:
        if args.list_only:
            meta = fetch_album_items(args.album, settings, biz=args.album_biz)
            if meta.get('error'):
                print(f"失败：{meta['error']}")
                sys.exit(1)
            print(f"合集：{meta.get('album_title') or '(未命名)'}  共 {len(meta['items'])} 篇")
            for it in meta['items']:
                print(f"{it['n']}. {it['title']}")
                print(f"   {it['url']}")
            return
        res = process_album(args.album, settings, vault, obsidian_mode, out_dir,
                            download_images, overwrite=args.overwrite,
                            dry_run=args.dry_run, biz=args.album_biz)
        if res['error']:
            print(f"失败：{res['error']}")
            sys.exit(1)
        print(f"\n===== 合集完成：{res['album_title'] or '(未命名)'} =====")
        print(f"成功 {len(res['ok'])} / 失败 {len(res['fail'])} / 共 {res['total']} 篇")
        if download_images and not args.dry_run:
            print(f"图片：成功 {res['img_ok']} 张，失败 {res['img_fail']} 张")
        if res['index_path']:
            print(f"链接清单：{res['index_path']}")
        for f in res['fail']:
            print(f"  ❌ {f['n']}. {f['title'][:40]} :: {f['error'][:70]}")
        if res['fail']:
            sys.exit(1)
        return

    # ---------- 单篇模式 ----------
    if not is_wechat_url(args.url):
        print(f"警告：链接非微信公众号域名（{args.url}），仍尝试抓取。")

    result = process_note(args.url, settings, vault, obsidian_mode, out_dir,
                          download_images, args.overwrite, args.dry_run)

    # 最终汇总（脚本唯一打印位置）
    if result.get('dry_run'):
        print(f"【dry-run】标题：{result['title']}")
        print(f"发布日期：{result['created']}")
        print(f"预计图片：{result['images']} 张（未下载）")
        return

    if result['error']:
        print(f"失败：{result['error']}")
        sys.exit(1)

    print(f"已保存：{result['path']}")
    if download_images:
        print(f"图片：成功 {result['img_ok']} 张，失败 {result['img_fail']} 张")
        max_show = 10  # 失败 URL 封顶，避免汇总过长占用上下文
        for u in result['img_failed'][:max_show]:
            print(f"  - 下载失败：{u[:90]}")
        if len(result['img_failed']) > max_show:
            print(f"  - …其余 {len(result['img_failed']) - max_show} 张略")
    else:
        print(f"图片：保留原文链接（共 {result['images']} 张）")


if __name__ == '__main__':
    main()
