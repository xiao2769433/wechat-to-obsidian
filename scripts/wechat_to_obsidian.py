#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号文章 -> Obsidian 本地 Markdown（文章正文 + 图片本地化）

改造自 GitHub likemaoke/wechat-article-to-md (MIT)，针对实际使用做了增强：
  * 图片下载带微信 Referer，规避 403 空白图
  * 处理协议相对地址 (//) 与 data: URI 占位图
  * 解析 wx_fmt 得到正确图片扩展名（jpg/png/gif/webp）
  * 图片文件名：xiao_yyyyMMdd_HHmmss_随机四位数字.<原格式>；不清空共享图片目录（安全）
  * 默认相对 images/ 引用（适配 Obsidian 与用户笔记习惯）；--obsidian 用 ![[...]]
  * 不写与文件名重复的 # 文档标题；正文标题逐级上移（h2→h1，h3→h2…）
  * 自动添加 Obsidian 笔记属性（YAML frontmatter）：title/source/created
  * 自动压缩多余空行，输出更紧凑

用法:
  python wechat_to_obsidian.py <文章URL> [选项]
  选项:
    --vault DIR     笔记库根目录（覆盖 settings.json 的 vault；留空则需在 settings.json 配置或用 --out 指定）
    --out DIR       直接指定 Markdown 输出目录（覆盖 --vault 下的默认目录）
    --obsidian      使用 Obsidian wikilink 格式 ![[图片名]] 而非 !(images/x.jpg)
    --no-img        不下载图片，仅保留原文图片链接
    --overwrite     已存在同名文件时覆盖原文件，而非生成 _1.md 副本
"""

import sys
import re
import argparse
import json
import time
import random
import string
import ipaddress
from datetime import datetime
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

# 以下为「配置缺省值」，仅当 config/settings.json 缺失或某项缺失时兜底使用。
# 实际运行取值全部来自 config/settings.json —— 改配置请改 json，不要改这里。
WECHAT_FOLDER = "微信文章"
IMAGE_PREFIX = "wechat"
REQUEST_TIMEOUT = 30
DOWNLOAD_RETRIES = 2

__version__ = '0.1.0'

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 复用 HTTP 连接(P2)
_session = requests.Session()
# 优先 lxml 解析(更快)，未安装则回退 html.parser(P3)
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

def load_settings():
    """读取同目录 config/settings.json 作为运行时配置；缺失或异常时回退下方默认值。"""
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
    except Exception as e:
        print(f"警告: 读取配置 {cfg_path} 失败({e})，使用默认配置。")
    return defaults

def sanitize_filename(name: str) -> str:
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

def is_safe_url(url: str) -> bool:
    """防 SSRF：仅允许 http/https，且主机非 localhost/内网/保留地址(字面 IP 检查)。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    if not host or host == 'localhost':
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 域名，放行(微信图床均为公网域名)
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)

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
    """带微信 Referer 下载图片，失败按配置重试(指数退避，404 不重试)。"""
    if not is_safe_url(url):
        print(f"  跳过不安全图片地址(SSRF 防护): {url[:90]}")
        return False
    headers = {
        'User-Agent': UA,
        'Referer': 'https://mp.weixin.qq.com/',
        'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
    }
    for attempt in range(1 + settings['download_retries']):
        try:
            r = _session.get(url, headers=headers, timeout=settings['request_timeout'])
            if r.status_code == 200 and len(r.content) > MIN_IMAGE_BYTES and looks_like_image(r.content):
                (img_dir / filename).write_bytes(r.content)
                return True
            if r.status_code == 404:
                return False  # 资源不存在，重试无意义
        except Exception as e:
            print(f"  图片下载异常: {e}")
        if attempt < settings['download_retries']:
            time.sleep(2 ** attempt)  # 指数退避: 1s, 2s, 4s...
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
    inline 与 cell_inline 的公共逻辑；br_sep 区分换行符，列表元素跳过(由 render_list 处理)。"""
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
    """把 <table> 转为 GFM Markdown 表格(首行表头，colspan 展开)。"""
    all_tr = table_el.find_all('tr')
    if not all_tr:
        return ''
    grid = []
    for tr in all_tr:
        cells = tr.find_all(['td', 'th'])
        row = []
        for c in cells:
            try:
                cs = int(c.get('colspan', 1))
            except (ValueError, TypeError):
                cs = 1
            row.append((cell_inline_fn(c), cs))
        if row:
            grid.append(row)
    if not grid:
        return ''
    ncols = max(sum(cs for _, cs in r) for r in grid)
    out = []
    for ri, row in enumerate(grid):
        expanded = []
        for content, cs in row:
            expanded.append(content)
            for _ in range(cs - 1):
                expanded.append('')
        while len(expanded) < ncols:
            expanded.append('')
        out.append('| ' + ' | '.join(expanded) + ' |')
        if ri == 0:
            out.append('| ' + ' | '.join(['---'] * ncols) + ' |')
    return '\n'.join(out)

def extract_publish_date(soup, html_text: str) -> str:
    """尽量从页面提取发布日期（YYYY-MM-DD），失败则用当天。"""
    m = soup.find('meta', attrs={'property': 'article:published_time'}) or \
        soup.find('meta', attrs={'itemprop': 'datePublished'})
    if m and m.get('content'):
        return str(m.get('content'))[:10]
    pt = soup.find(id='publish_time')
    if pt and pt.get_text(strip=True):
        return pt.get_text(strip=True)[:10]
    mm = re.search(r'var\s+ct\s*=\s*["\']?(\d{10})', html_text or '')
    if mm:
        try:
            return datetime.fromtimestamp(int(mm.group(1)), datetime.UTC).strftime('%Y-%m-%d')
        except Exception:
            pass  # 时间戳解析失败，回退当天
    return datetime.now().strftime('%Y-%m-%d')

def html_to_markdown(soup, img_dir, obsidian_mode, download_images, settings):
    """将正文 soup 转为 Markdown，保持元素原始顺序。"""
    md = []
    img_map = {}  # 原图 URL -> 本地文件名

    # 图片复用：读取已保存的 URL->本地文件名 映射，避免重复下载同一文章的历史图片
    map_path = (img_dir.parent / '.wechat_image_map.json') if img_dir else None
    url_to_file = {}
    if map_path and map_path.exists():
        try:
            raw = json.loads(map_path.read_text(encoding='utf-8'))
            # 仅保留本地文件仍存在的映射，自动清理失效项
            url_to_file = {u: f for u, f in raw.items()
                           if (img_dir / f).exists()}
        except Exception as e:
            print(f"  警告: 图片复用映射读取失败({e})，将重新下载")
            url_to_file = {}

    if img_dir and download_images:
        # 统一图片命名：xiao_yyyyMMdd_HHmmss_随机四位.<ext>
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        to_download = []  # (src, fname) 待并发下载
        for img in soup.find_all('img'):
            src = normalize_img_url(img.get('data-src') or img.get('src', ''))
            if src and src not in img_map:
                # 已存在且对应本地文件仍在 -> 直接复用，不重新下载
                if src in url_to_file and (img_dir / url_to_file[src]).exists():
                    img_map[src] = url_to_file[src]
                    print(f"  图片复用: {url_to_file[src]}")
                    continue
                ext = img_extension(src)
                rand4 = ''.join(random.choices(string.digits, k=4))
                to_download.append((src, f"{settings['image_prefix']}_{stamp}_{rand4}{ext}"))
        # 并发下载(P1)
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(
                lambda sf: (sf[0], sf[1], download_image(sf[0], img_dir, sf[1], settings)),
                to_download))
        for src, fname, ok in results:
            if ok:
                img_map[src] = fname
                url_to_file[src] = fname
                print(f"  图片已存: {fname}")
            else:
                print(f"  图片下载失败: {src[:90]}")
        # 回写映射（含本次新下载的），供下次复用
        if map_path:
            try:
                if len(url_to_file) > MAX_IMAGE_MAP:
                    url_to_file = dict(list(url_to_file.items())[-MAX_IMAGE_MAP:])
                map_path.write_text(json.dumps(url_to_file, ensure_ascii=False, indent=2),
                                    encoding='utf-8')
            except Exception as e:
                print(f"  警告: 图片复用映射写入失败({e})")

    def ref(fname: str) -> str:
        return f"![[{fname}]]" if obsidian_mode else f"![](images/{fname})"

    def add_img(src, alt=''):
        url = normalize_img_url(src)
        if url and url in img_map:
            md.append(ref(img_map[url]) + "\n")
        elif url:
            md.append(f"![{alt}]({md_escape_url(url)})\n")

    def inline(element) -> str:
        return _render_inline(element, img_map, ref, '\n')

    def render_list(list_el, ordered, depth=0):
        """递归渲染列表(支持嵌套，子级缩进两空格)。"""
        lines = []
        for idx, li in enumerate(list_el.find_all('li', recursive=False), 1):
            marker = f"{'  ' * depth}{idx}. " if ordered else f"{'  ' * depth}- "
            content = inline(li).strip()
            if content:
                lines.append(marker + content)
            for sub in li.find_all(['ul', 'ol'], recursive=False):
                rendered = render_list(sub, sub.name == 'ol', depth + 1)
                if rendered:
                    lines.append(rendered)
        return '\n'.join(lines)

    def cell_inline(cell):
        """把单元格内容渲染为单行 Markdown（图片/粗体/链接保留）。"""
        text = _render_inline(cell, img_map, ref, ' ')
        text = re.sub(r'\s+', ' ', text).strip()
        return text.replace('|', '\\|')

    # 表格渲染见模块级 _table_to_markdown(通过 cell_inline 闭包参数化)
    def traverse(el, in_list=False, in_quote=False):
        for child in el.children:
            if not hasattr(child, 'name') or child.name is None:
                txt = str(child).strip()
                if txt and not in_list and not in_quote:
                    md.append(txt + "\n\n")
                continue
            t = child.name
            if t == 'img':
                add_img(child.get('data-src') or child.get('src', ''), child.get('alt', ''))
                continue
            if t == 'iframe':
                md.append("\n> 🎥 视频：微信视频无法在 Markdown 中直接播放，请访问原文观看。\n\n")
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
                    md.append(f"\n{'#' * level} {text}\n\n")
                for im in child.find_all('img'):
                    add_img(im.get('data-src') or im.get('src', ''), im.get('alt', ''))
                continue
            if t == 'p':
                content = inline(child).strip()
                if content:
                    md.append(content + "\n\n")
                continue
            if t == 'ul':
                rendered = render_list(child, ordered=False)
                if rendered:
                    md.append(rendered + '\n')
                continue
            if t == 'ol':
                rendered = render_list(child, ordered=True)
                if rendered:
                    md.append(rendered + '\n')
                continue
            if t == 'blockquote':
                txt = inline(child).strip()
                if txt:
                    quoted = '\n'.join(f"> {ln}" for ln in txt.split('\n') if ln.strip())
                    md.append(f"\n{quoted}\n\n")
                continue
            if t == 'pre':
                code = pre_to_text(child)
                if code:
                    lang = code_language(child)
                    fence = fence_for(code)
                    md.append(f"\n{fence}{lang}\n{code}\n{fence}\n\n")
                continue
            if t == 'hr':
                md.append("\n---\n\n")
                continue
            if t == 'br':
                md.append("\n")
                continue
            if t == 'table':
                tbl = _table_to_markdown(child, cell_inline)
                if tbl:
                    md.append("\n" + tbl + "\n\n")
                continue
            traverse(child, in_list, in_quote)

    traverse(soup)
    return '\n'.join(md)

def fetch_wechat_article(url, settings, vault="",
                         obsidian_mode=False, out_dir=None, download_images=True,
                         overwrite=False):
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    print(f"请求: {url}")
    try:
        r = _session.get(url, headers=headers, timeout=settings['request_timeout'])
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        print(f"请求失败: {e}")
        return None

    soup = BeautifulSoup(r.text, _PARSER)

    # 标题
    meta = soup.find('meta', property='og:title')
    if meta:
        title = meta.get('content', '').strip() or '无标题'
    else:
        h = soup.find('h1', class_='rich_media_title')
        title = h.get_text(strip=True) if h else '无标题'
    print(f"标题: {title}")

    # 作者
    author = None
    ae = soup.find(id='js_author_name')
    if ae:
        author = ae.get_text(strip=True)
    if not author:
        ae = soup.find('a', class_='rich_media_meta_link')
        author = ae.get_text(strip=True) if ae else '未知作者'
    print(f"作者: {author}")

    # 正文
    content = soup.find('div', class_='rich_media_content') or soup.find('div', id='js_content')
    if not content:
        print("未找到正文内容（该文章可能需登录或已删除）")
        return None

    base = Path(out_dir) if out_dir else (Path(vault) / settings['wechat_folder'])
    try:
        base.mkdir(parents=True, exist_ok=True)
        img_dir = base / 'images'
        if download_images:
            img_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"错误: 无法创建输出目录 {base}({e})")
        return None

    pub_date = extract_publish_date(soup, r.text)

    # 笔记属性（YAML frontmatter，格式见 templates/output-template.md）
    safe_title = yaml_escape(title)
    frontmatter = (
        "---\n"
        f'title: "{safe_title}"\n'
        f"source: {url}\n"
        f"created: {pub_date}\n"
        "---\n"
    )

    body_md = html_to_markdown(content, img_dir if download_images else None,
                               obsidian_mode, download_images, settings)
    body = normalize_blanklines(body_md)

    out = frontmatter + "\n" + body + "\n"

    fname = sanitize_filename(title) + '.md'
    fpath = base / fname
    if not overwrite:
        i = 1
        while fpath.exists():
            fpath = base / f"{sanitize_filename(title)}_{i}.md"
            i += 1
    fpath.write_text(out, encoding='utf-8')
    print(f"已保存: {fpath}")
    if download_images:
        print(f"图片目录: {(img_dir).resolve()}")
    return str(fpath)

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
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()

    if not args.url:
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

    result = fetch_wechat_article(args.url, settings, vault, obsidian_mode, out_dir, download_images, args.overwrite)
    if not result:
        sys.exit(1)

if __name__ == '__main__':
    main()
