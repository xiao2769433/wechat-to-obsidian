#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_to_obsidian 单元测试（纯函数 + 转换 + 下载路径，均用 mock，无需联网）。

运行:
    cd 仓库根目录
    python -m unittest tests.test_wechat_to_obsidian
    # 或
    python -m unittest discover tests

依赖缺失（bs4 / requests）时，相关用例自动 skip 而非整文件报错（E4）。
"""
import re
import os
import sys
import json as json_mod
import socket
import unittest
import tempfile
from pathlib import Path
from unittest import mock

# 将 scripts/ 加入搜索路径以导入被测模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

# ---- 依赖守卫：缺失时 skip，避免全盘 ImportError（E4）----
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    BeautifulSoup = None
    HAS_BS4 = False

try:
    import requests
    import wechat_to_obsidian as w
    HAS_MODULE = True
except Exception:
    w = None
    HAS_MODULE = False

HAS_DEPS = HAS_BS4 and HAS_MODULE
skip_no_deps = unittest.skipUnless(HAS_DEPS, "需要 bs4 与 requests")


def _cell_inline_fn(img_map=None, br_sep='<br>'):
    """构造与转换器内部 cell_inline 等价的闭包，用于测试 _table_to_markdown。"""
    img_map = img_map if img_map is not None else {}
    ref = lambda f: f"![](images/{f})"

    def cell_inline(cell):
        text = w._render_inline(cell, img_map, ref, br_sep)
        text = re.sub(r'[ \t]+', ' ', text).strip()
        return text.replace('|', '\\|')
    return cell_inline


@skip_no_deps
class TestFilenameAndEscaping(unittest.TestCase):
    def test_sanitize_filename_removes_invalid(self):
        self.assertEqual(w.sanitize_filename('a/b?c'), 'a_b_c')
        self.assertEqual(w.sanitize_filename(''), 'article')

    def test_sanitize_filename_strips_dots_spaces(self):
        self.assertEqual(w.sanitize_filename('  hello.  '), 'hello')

    def test_yaml_escape(self):
        self.assertEqual(w.yaml_escape('a"b'), 'a\\"b')
        self.assertEqual(w.yaml_escape('a\\b'), 'a\\\\b')
        self.assertEqual(w.yaml_escape('a\nb'), 'a b')

    def test_md_escape_url(self):
        self.assertEqual(w.md_escape_url('a b)c'), 'a%20b%29c')
        self.assertEqual(w.md_escape_url('normal'), 'normal')


@skip_no_deps
class TestImageUrlSafety(unittest.TestCase):
    def test_normalize_img_url(self):
        self.assertIsNone(w.normalize_img_url(''))
        self.assertIsNone(w.normalize_img_url('data:image/png;base64,xxx'))
        self.assertIsNone(w.normalize_img_url('javascript:alert(1)'))
        self.assertEqual(w.normalize_img_url('//a.com/x.jpg'), 'https://a.com/x.jpg')
        self.assertEqual(w.normalize_img_url('https://a.com/x.jpg'), 'https://a.com/x.jpg')

    def test_is_safe_url_blocks_internal(self):
        for u in ('http://127.0.0.1/x',
                  'http://169.254.169.254/latest/meta-data',
                  'http://10.0.0.1/x',
                  'http://192.168.1.1/x',
                  'http://172.16.0.1/x',
                  'http://localhost/x',
                  'http://0.0.0.0/x',
                  'ftp://a.com/x'):
            self.assertFalse(w.is_safe_url(u), f'{u} 应被拦截')

    def test_is_safe_url_allows_public(self):
        # mock DNS 返回公网地址，避免测试依赖外网解析
        with mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                        return_value=[(socket.AF_INET, 0, 0, '', ('93.184.216.34', 0))]):
            self.assertTrue(w.is_safe_url('https://mmbiz.qpic.cn/a.jpg'))
            self.assertTrue(w.is_safe_url('http://example.com/a.png'))

    def test_is_safe_url_blocks_metadata_hostname(self):
        # 云元数据域名直接拦截，不依赖 DNS
        self.assertFalse(w.is_safe_url('http://metadata.google.internal/x.png'))
        self.assertFalse(w.is_safe_url('http://169.254.169.254/latest/meta-data'))

    def test_is_safe_url_blocks_domain_resolving_to_internal(self):
        # B1：域名解析后指向内网，应被拦截（mock DNS 返回私有地址）
        with mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                        return_value=[(socket.AF_INET, 0, 0, '', ('10.0.0.5', 0))]):
            self.assertFalse(w.is_safe_url('http://internal.corp.local/x.png'))

    def test_is_safe_url_allows_domain_resolving_to_public(self):
        # 域名解析后指向公网，应放行
        with mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                        return_value=[(socket.AF_INET, 0, 0, '', ('93.184.216.34', 0))]):
            self.assertTrue(w.is_safe_url('http://example.com/x.png'))

    def test_is_safe_url_blocks_on_dns_failure(self):
        # DNS 解析失败时保守拦截，不放行无法验证的地址
        with mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                        side_effect=socket.gaierror('resolve failed')):
            self.assertFalse(w.is_safe_url('http://unresolvable.example/x.png'))

    def test_is_safe_url_caches_dns_per_host(self):
        # 同 host 第二次应命中缓存，不再 getaddrinfo（一篇文章多图只解析一次）
        with mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                        return_value=[(socket.AF_INET, 0, 0, '', ('93.184.216.34', 0))]) as m:
            self.assertTrue(w.is_safe_url('http://cached.example/x.png'))
            self.assertTrue(w.is_safe_url('http://cached.example/y.png'))
            self.assertEqual(m.call_count, 1, "同 host 第二次应命中缓存不再解析")

    def test_download_image_rejects_unsafe_url(self):
        with tempfile.TemporaryDirectory() as d:
            img_dir = Path(d)
            ok = w.download_image('http://127.0.0.1/x.png', img_dir, 'a.png',
                                  {'download_retries': 0})
            self.assertFalse(ok)
            self.assertFalse((img_dir / 'a.png').exists())

    def test_img_extension(self):
        self.assertEqual(w.img_extension('https://a.com/x?wx_fmt=png'), '.png')
        self.assertEqual(w.img_extension('https://a.com/x?wx_fmt=jpeg'), '.jpg')
        self.assertEqual(w.img_extension('https://a.com/x.gif'), '.gif')
        self.assertEqual(w.img_extension('https://a.com/x.unknown'), '.jpg')

    def test_looks_like_image(self):
        self.assertTrue(w.looks_like_image(b'\xff\xd8\xff' + b'x' * 10))
        self.assertTrue(w.looks_like_image(b'\x89PNG\r\n\x1a\n' + b'x'))
        self.assertTrue(w.looks_like_image(b'GIF89a' + b'x'))
        self.assertTrue(w.looks_like_image(b'RIFF\x00\x00\x00\x00WEBP'))
        self.assertFalse(w.looks_like_image(b'<html>not image</html>'))
        self.assertFalse(w.looks_like_image(b''))


@skip_no_deps
class TestCodeFence(unittest.TestCase):
    def test_fence_for_plain(self):
        self.assertEqual(w.fence_for('hello'), '```')

    def test_fence_for_with_backticks(self):
        self.assertEqual(w.fence_for('a ``` b'), '````')
        self.assertEqual(w.fence_for('a ```` b'), '`````')

    def test_code_language(self):
        soup = BeautifulSoup('<pre><code class="language-python">x</code></pre>', 'html.parser')
        self.assertEqual(w.code_language(soup.find('pre')), 'python')
        soup2 = BeautifulSoup('<pre><code class="brush:js">x</code></pre>', 'html.parser')
        self.assertEqual(w.code_language(soup2.find('pre')), 'js')
        soup3 = BeautifulSoup('<pre><code>x</code></pre>', 'html.parser')
        self.assertEqual(w.code_language(soup3.find('pre')), '')


@skip_no_deps
class TestNormalizeBlanklines(unittest.TestCase):
    def test_collapses_blank_lines(self):
        self.assertEqual(w.normalize_blanklines('a\n\n\n\nb'), 'a\n\nb')

    def test_strips_leading_trailing(self):
        self.assertEqual(w.normalize_blanklines('\n\na\n\n'), 'a')

    def test_preserves_code_fence(self):
        result = w.normalize_blanklines('```\n\n\n\ncode\n```')
        self.assertIn('\n\n\n\ncode\n', result)

    def test_recognizes_fenced_with_lang(self):
        result = w.normalize_blanklines('```python\n\n\nx\n```')
        self.assertIn('\n\n\nx\n', result)

    def test_tilde_fence(self):
        result = w.normalize_blanklines('~~~\n\n\nx\n~~~')
        self.assertIn('\n\n\nx\n', result)


@skip_no_deps
class TestPreToText(unittest.TestCase):
    def test_preserves_newlines_and_indent(self):
        soup = BeautifulSoup('<pre>line1\nline2\n  indented</pre>', 'html.parser')
        text = w.pre_to_text(soup.find('pre'))
        self.assertIn('line1', text)
        self.assertIn('line2', text)
        self.assertIn('  indented', text)

    def test_formula(self):
        soup = BeautifulSoup('<pre>x <span data-formula=" O(n) ">y</span></pre>', 'html.parser')
        self.assertIn('$O(n)$', w.pre_to_text(soup.find('pre')))


@skip_no_deps
class TestTableMarkdown(unittest.TestCase):
    def test_simple_table(self):
        html = '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _cell_inline_fn())
        self.assertIn('| A | B |', result)
        self.assertIn('| --- | --- |', result)
        self.assertIn('| 1 | 2 |', result)

    def test_pipe_escaped(self):
        html = '<table><tr><th>H</th></tr><tr><td>a|b</td></tr></table>'
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _cell_inline_fn())
        self.assertIn('a\\|b', result)

    def test_colspan(self):
        html = ('<table><tr><th colspan="2">合</th><th>单</th></tr>'
                '<tr><td>1</td><td>2</td><td>3</td></tr></table>')
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _cell_inline_fn())
        self.assertIn('| 合 |  | 单 |', result)
        self.assertIn('| 1 | 2 | 3 |', result)

    def test_rowspan(self):  # C2
        html = ('<table>'
                '<tr><th>姓名</th><th>年龄</th></tr>'
                '<tr><td rowspan="2">张三</td><td>20</td></tr>'
                '<tr><td>21</td></tr>'
                '</table>')
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _cell_inline_fn())
        self.assertIn('| 姓名 | 年龄 |', result)
        self.assertIn('| 张三 | 20 |', result)
        self.assertIn('|  | 21 |', result)  # 跨行占位单元格为空
        self.assertEqual(result.count('张三'), 1)  # 内容只在首行出现一次

    def test_cell_br_preserved(self):  # C3
        html = '<table><tr><td>a<br>b</td></tr></table>'
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _cell_inline_fn(br_sep='<br>'))
        self.assertIn('a<br>b', result)


@skip_no_deps
class TestRenderInline(unittest.TestCase):
    def test_bold_italic_link(self):
        soup = BeautifulSoup('<p><b>粗</b> <i>斜</i> <a href="x">链</a></p>', 'html.parser')
        result = w._render_inline(soup.find('p'), {}, lambda f: f"![](images/{f})", '\n')
        self.assertIn('**粗**', result)
        self.assertIn('*斜*', result)
        self.assertIn('[链](x)', result)

    def test_formula(self):
        soup = BeautifulSoup('<p>x <span data-formula=" O(n) "></span></p>', 'html.parser')
        result = w._render_inline(soup.find('p'), {}, lambda f: f, '\n')
        self.assertIn('$O(n)$', result)

    def test_skips_nested_list(self):
        soup = BeautifulSoup('<p>hello<ul><li>item</li></ul></p>', 'html.parser')
        result = w._render_inline(soup.find('p'), {}, lambda f: f, '\n')
        self.assertIn('hello', result)
        self.assertNotIn('item', result)  # 嵌套列表被跳过


@skip_no_deps
class TestHtmlToMarkdown(unittest.TestCase):
    """集成测试:验证 html_to_markdown 整体渲染(不下载图片)。"""
    def setUp(self):
        self.settings = {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}

    def _render(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        return w.html_to_markdown(soup, None, False, False, self.settings)

    def test_list_preserves_inline_format(self):
        md = self._render('<ul><li><b>粗</b>项</li><li>普通</li></ul>')
        self.assertIn('**粗**', md)
        self.assertIn('- ', md)

    def test_nested_list_indent(self):
        md = self._render('<ul><li>父<ul><li>子</li></ul></li></ul>')
        self.assertIn('- 父', md)
        self.assertIn('  - 子', md)

    def test_code_block_with_language(self):
        md = self._render('<pre><code class="language-python">print(1)</code></pre>')
        self.assertIn('```python', md)
        self.assertIn('print(1)', md)

    def test_blockquote_preserves_format(self):
        md = self._render('<blockquote>引用 <b>粗</b></blockquote>')
        self.assertIn('> ', md)
        self.assertIn('**粗**', md)

    def test_heading_promotion(self):  # E1: 正文标题逐级上移
        md = self._render('<h2>小节</h2><h3>子节</h3>')
        self.assertIn('# 小节', md)
        self.assertIn('## 子节', md)

    def test_figure_with_caption(self):  # C4
        html = ('<figure><img data-src="http://x/a.png">'
                '<figcaption>示意图</figcaption></figure>')
        md = self._render(html)
        self.assertIn('![](http://x/a.png)', md)  # 未下载时保留原链接
        self.assertIn('*图：示意图*', md)

    def test_dry_run_counts_images_no_download(self):  # C7
        html = '<p>前</p><img data-src="http://x/1.png"><img data-src="http://x/2.png">'
        soup = BeautifulSoup(html, 'html.parser')
        conv = w.WechatMarkdownConverter(base=Path('.'), img_dir=None,
                                         obsidian_mode=False, download_images=True,
                                         settings=self.settings, dry_run=True)
        conv.convert(soup)
        self.assertEqual(conv.img_total, 2)
        self.assertEqual(conv.img_ok, 0)
        self.assertEqual(conv.img_fail, 0)


@skip_no_deps
class TestExtractAndFrontmatter(unittest.TestCase):
    def _settings(self):
        return {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}

    def test_extract_publish_date_ct(self):  # C6
        soup = BeautifulSoup('<html></html>', 'html.parser')
        html = 'var ct = "1700000000";'
        self.assertEqual(w.extract_publish_date(soup, html), '2023-11-14')

    def test_extract_publish_date_ori(self):  # C6: oriCreateTime 回退
        soup = BeautifulSoup('<html></html>', 'html.parser')
        html = 'var oriCreateTime = "1600000000";'
        self.assertEqual(w.extract_publish_date(soup, html), '2020-09-13')

    def test_extract_publish_date_fallback_is_date(self):  # E1
        soup = BeautifulSoup('<html></html>', 'html.parser')
        d = w.extract_publish_date(soup, 'no date here')
        self.assertRegex(d, r'^\d{4}-\d{2}-\d{2}$')

    def test_build_frontmatter(self):  # E1
        fm = w.build_frontmatter('测试"标题', 'http://mp.weixin.qq.com/s/x', '2024-01-01')
        self.assertTrue(fm.startswith('---\n'))
        self.assertIn('title: "测试\\"标题"', fm)
        self.assertIn('source: http://mp.weixin.qq.com/s/x', fm)
        self.assertIn('created: 2024-01-01', fm)


@skip_no_deps
class TestConverterDownload(unittest.TestCase):
    """图片下载路径测试，用 mock 替代网络请求（E2）。"""
    PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 300  # 合法 PNG 头 + 足够体积

    def setUp(self):
        # mock DNS 返回公网地址，避免测试依赖外网解析（is_safe_url 会解析域名）
        self._gai = mock.patch('wechat_to_obsidian.socket.getaddrinfo',
                               return_value=[(socket.AF_INET, 0, 0, '', ('93.184.216.34', 0))])
        self._gai.start()

    def tearDown(self):
        self._gai.stop()

    def _settings(self):
        return {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}

    def _fake_session(self, content=PNG, status=200):
        class Resp:
            def __init__(self):
                self.status_code = status
                self.content = content
                self.encoding = None
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError('http error')
        class Sess:
            def get(self, *a, **k):
                return Resp()
        return Sess()

    def test_download_image_writes_file(self):  # E2
        with tempfile.TemporaryDirectory() as d:
            img_dir = Path(d)
            with mock.patch.object(w, '_session', lambda: self._fake_session()):
                ok = w.download_image('http://x/a.png', img_dir, 'a.png', self._settings())
            self.assertTrue(ok)
            self.assertTrue((img_dir / 'a.png').exists())

    def test_dedupe_same_url_within_run(self):  # C5 + E2
        settings = self._settings()
        soup = BeautifulSoup(
            '<img data-src="http://x/same.png"><img data-src="http://x/same.png">',
            'html.parser')
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            img_dir = base / 'images'
            img_dir.mkdir()
            with mock.patch.object(w, '_session', lambda: self._fake_session()):
                conv = w.WechatMarkdownConverter(base=base, img_dir=img_dir,
                                                 obsidian_mode=False, download_images=True,
                                                 settings=settings, dry_run=False)
                conv.convert(soup)
            files = list(img_dir.glob('*'))
            self.assertEqual(len(files), 1, "同一 URL 本次运行只应下载一次")
            self.assertEqual(conv.img_total, 1, "img_total 按去重后的唯一 URL 计数")
            self.assertEqual(conv.img_ok, 1)

    def test_is_wechat_url(self):  # C7
        self.assertTrue(w.is_wechat_url('https://mp.weixin.qq.com/s/abc'))
        self.assertTrue(w.is_wechat_url('https://mp.weixin.qq.com/s/abc?x=1'))
        self.assertFalse(w.is_wechat_url('https://example.com/x'))
        self.assertFalse(w.is_wechat_url('not a url'))


class TestAlbumParsing(unittest.TestCase):
    """合集 URL / album_id 解析（F1：--album 支持两种入参）。"""

    def test_parse_full_album_url(self):
        url = ('https://mp.weixin.qq.com/mp/appmsgalbum?__biz=Mzg4NDkzNTU1Ng=='
               '&action=getalbum&album_id=4511205931589779463&scene=126&sessionid=#wechat_redirect')
        biz, album = w.parse_album_target(url)
        self.assertEqual(biz, 'Mzg4NDkzNTU1Ng==')
        self.assertEqual(album, '4511205931589779463')

    def test_parse_bare_album_id_with_biz(self):
        biz, album = w.parse_album_target('4511205931589779463', biz='Mzg4NDkzNTU1Ng==')
        self.assertEqual(biz, 'Mzg4NDkzNTU1Ng==')
        self.assertEqual(album, '4511205931589779463')

    def test_parse_bare_album_id_without_biz(self):
        biz, album = w.parse_album_target('4511205931589779463')
        self.assertEqual(biz, '')
        self.assertEqual(album, '4511205931589779463')

    def test_parse_empty(self):
        self.assertEqual(w.parse_album_target(''), ('', ''))
        self.assertEqual(w.parse_album_target(None), ('', ''))

    def test_parse_album_url_without_album_id(self):
        biz, album = w.parse_album_target('https://mp.weixin.qq.com/mp/appmsgalbum?__biz=ABC')
        self.assertEqual(biz, 'ABC')
        self.assertEqual(album, '')


class TestAlbumApiUrl(unittest.TestCase):
    """合集接口 URL 构造（F2：游标参数名不可改）。"""

    def test_first_page_url(self):
        u = w._album_api_url('BIZ', 'ALBUM')
        self.assertIn('action=getalbum', u)
        self.assertIn('__biz=BIZ', u)
        self.assertIn('album_id=ALBUM', u)
        self.assertIn('count=10', u)
        self.assertIn('f=json', u)          # 缺 f=json 会返回 HTML
        self.assertNotIn('begin_msgid', u)  # 首页无游标

    def test_next_page_uses_cursor(self):
        u = w._album_api_url('BIZ', 'ALBUM', begin=('2247485639', '1'))
        self.assertIn('begin_msgid=2247485639', u)
        self.assertIn('begin_itemidx=1', u)

    def test_custom_count(self):
        self.assertIn('count=3', w._album_api_url('B', 'A', count=3))

    def test_ignores_legacy_pageidx(self):
        """回归：pageidx/from_msgid 实测无效，URL 中不应出现。"""
        u = w._album_api_url('BIZ', 'ALBUM')
        self.assertNotIn('pageidx', u)
        self.assertNotIn('from_msgid', u)


@skip_no_deps
class TestFetchAlbumItems(unittest.TestCase):
    """合集游标翻页（F2），全部用 mock，不联网。"""

    def _page(self, items, continue_flag='1'):
        return json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {
                'article_list': items,
                'continue_flag': continue_flag,
                'base_info': {'title': '图解计算机原理'},
            },
        })

    @staticmethod
    def _item(mid, title):
        return {'title': title, 'msgid': mid, 'itemidx': '1',
                'url': f'http://mp.weixin.qq.com/s?mid={mid}'}

    def _session_for(self, pages):
        """按请求顺序依次返回 pages；记录每次请求的 URL 供断言。"""
        calls = []

        class Resp:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.content = b''
                self.encoding = None

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, url, **k):
                calls.append(url)
                idx = min(len(calls) - 1, len(pages) - 1)
                return Resp(pages[idx])

        return Sess(), calls

    def test_paginates_until_continue_flag_zero(self):
        pages = [
            self._page([self._item('100', 'A'), self._item('101', 'B')], '1'),
            self._page([self._item('102', 'C')], '0'),
        ]
        sess, calls = self._session_for(pages)
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: sess):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertIsNone(r['error'])
        self.assertEqual(len(r['items']), 3)
        self.assertEqual(r['album_title'], '图解计算机原理')
        self.assertEqual([i['title'] for i in r['items']], ['A', 'B', 'C'])
        self.assertEqual(r['items'][0]['n'], 1)
        self.assertEqual(r['items'][2]['n'], 3)
        # 第 2 次请求必须带游标（上一页最后一条）
        self.assertEqual(len(calls), 2)
        self.assertIn('begin_msgid=101', calls[1])

    def test_stops_when_no_new_items(self):
        """接口重复返回同一页时应立即停止，避免死循环。"""
        same = self._page([self._item('100', 'A')], '1')
        sess, calls = self._session_for([same])
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: sess):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertEqual(len(r['items']), 1)
        self.assertLess(len(calls), w.ALBUM_MAX_PAGES)

    def test_dedupes_by_msgid_and_itemidx(self):
        pages = [
            self._page([self._item('100', 'A'), self._item('101', 'B')], '1'),
            self._page([self._item('101', 'B'), self._item('102', 'C')], '0'),
        ]
        sess, _ = self._session_for(pages)
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: sess):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertEqual([i['title'] for i in r['items']], ['A', 'B', 'C'])

    def test_missing_biz_reports_error(self):
        settings = {'request_timeout': 30, 'download_retries': 0}
        r = w.fetch_album_items('4511205931589779463', settings)
        self.assertIsNotNone(r['error'])
        self.assertIn('__biz', r['error'])
        self.assertEqual(r['items'], [])

    def test_missing_album_id_reports_error(self):
        settings = {'request_timeout': 30, 'download_retries': 0}
        r = w.fetch_album_items('', settings, biz='BIZ')
        self.assertIsNotNone(r['error'])
        self.assertIn('album_id', r['error'])

    def test_first_page_failure_reports_error(self):
        class Sess:
            def get(self, *a, **k):
                raise RuntimeError('network down')
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: Sess()):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertIsNotNone(r['error'])
        self.assertEqual(r['items'], [])

    def test_html_response_treated_as_failure(self):
        """接口未带 f=json 时返回 HTML，应报错而非崩溃/静默空结果。"""
        class Resp:
            status_code = 200
            text = '<html><body>验证</body></html>'
            content = b''

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, *a, **k):
                return Resp()

        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: Sess()):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertIsNotNone(r['error'])

    def test_url_html_entities_unescaped(self):
        """接口返回的 &amp; 应还原为 &。"""
        item = {'title': 'A', 'msgid': '1', 'itemidx': '1',
                'url': 'http://mp.weixin.qq.com/s?__biz=B&amp;mid=1'}
        pages = [self._page([item], '0')]
        sess, _ = self._session_for(pages)
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: sess):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertNotIn('&amp;', r['items'][0]['url'])
        self.assertIn('&mid=1', r['items'][0]['url'])

    def test_single_object_article_list(self):
        """接口单条时可能返回对象而非数组，需兼容。"""
        single = json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {
                'article_list': self._item('100', 'Only'),
                'continue_flag': '0',
                'base_info': {'title': 'T'},
            },
        })
        sess, _ = self._session_for([single])
        settings = {'request_timeout': 30, 'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: sess):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.fetch_album_items('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM', settings)
        self.assertIsNone(r['error'])
        self.assertEqual(len(r['items']), 1)


@skip_no_deps
class TestProcessAlbum(unittest.TestCase):
    """合集批量落盘（F3）：串联 fetch_album_items 与 process_note。"""

    ART_HTML = (
        '<html><head><meta property="og:title" content="{title}"></head>'
        '<body><div class="rich_media_content"><p>{title} 正文</p>'
        '</div></body></html>'
    )

    def _sessions(self, pages):
        """按顺序返回合集 JSON 页 + 文章 HTML。"""
        album_txt = json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {'article_list': pages, 'continue_flag': '0',
                              'base_info': {'title': '测试合集'}},
        })

        class Resp:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.content = b''
                self.encoding = None

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, url, **k):
                if 'action=getalbum' in url:
                    return Resp(album_txt)
                # 文章抓取：按 URL 里的 mid 造标题
                mm = re.search(r'mid=(\d+)', url)
                return Resp(self_outer.ART_HTML.format(title=f'T{mm.group(1)}'))

        self_outer = self
        return Sess()

    def test_writes_all_articles_and_index(self):
        pages = [{'title': f'T{i}', 'msgid': str(i), 'itemidx': '1',
                  'url': f'http://mp.weixin.qq.com/s?__biz=B&mid={i}'}
                 for i in (11, 12, 13)]
        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: self._sessions(pages)):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                with tempfile.TemporaryDirectory() as d:
                    r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM',
                                        settings, vault='', out_dir=d,
                                        download_images=False, progress=False)
                    self.assertIsNone(r['error'])
                    self.assertEqual(r['total'], 3)
                    self.assertEqual(len(r['ok']), 3)
                    self.assertEqual(r['fail'], [])
                    # 每篇 md 均落盘（须在临时目录存活期间断言）
                    for o in r['ok']:
                        self.assertTrue(Path(o['path']).exists(), o['path'])
                    # 链接清单已生成且含 3 条 wikilink
                    self.assertIsNotNone(r['index_path'])
                    idx = Path(r['index_path']).read_text(encoding='utf-8')
                    self.assertEqual(idx.count('[['), 3)

    def test_partial_failure_kept_and_reported(self):
        """单篇失败不应中断整本；成功项保留、失败项入 fail。"""
        pages = [{'title': 'OK', 'msgid': '1', 'itemidx': '1',
                  'url': 'http://mp.weixin.qq.com/s?__biz=B&mid=1'},
                 {'title': 'BAD', 'msgid': '2', 'itemidx': '1',
                  'url': 'http://mp.weixin.qq.com/s?__biz=B&mid=2'}]
        album_txt = json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {'article_list': pages, 'continue_flag': '0',
                              'base_info': {'title': 'C'}},
        })

        class Resp:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.content = b''
                self.encoding = None

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, url, **k):
                if 'action=getalbum' in url:
                    return Resp(album_txt)
                if 'mid=2' in url:
                    return Resp('<html><body>无正文</body></html>')  # 触发失败
                return Resp('<html><head><meta property="og:title" content="OK">'
                            '</head><body><div class="rich_media_content">'
                            '<p>x</p></div></body></html>')

        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: Sess()):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                with tempfile.TemporaryDirectory() as d:
                    r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM',
                                        settings, vault='', out_dir=d,
                                        download_images=False, progress=False)
        self.assertIsNone(r['error'])
        self.assertEqual(len(r['ok']), 1)
        self.assertEqual(len(r['fail']), 1)
        self.assertEqual(r['fail'][0]['title'], 'BAD')

    def test_empty_album_reports_error(self):
        album_txt = json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {'article_list': [], 'continue_flag': '0',
                              'base_info': {'title': '空'}},
        })

        class Resp:
            status_code = 200
            text = album_txt
            content = b''

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, *a, **k):
                return Resp()

        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: Sess()):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                    '?__biz=BIZ&album_id=ALBUM', settings,
                                    vault='', out_dir='/tmp', progress=False)
        self.assertIsNotNone(r['error'])
        self.assertEqual(r['ok'], [])

    def test_dry_run_writes_nothing(self):
        pages = [{'title': 'A', 'msgid': '1', 'itemidx': '1',
                  'url': 'http://mp.weixin.qq.com/s?__biz=B&mid=1'}]
        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: self._sessions(pages)):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                with tempfile.TemporaryDirectory() as d:
                    r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM',
                                        settings, vault='', out_dir=d,
                                        download_images=False, dry_run=True,
                                        progress=False)
        self.assertEqual(len(r['ok']), 1)
        self.assertIsNone(r['ok'][0]['path'])       # dry-run 不落盘
        self.assertIsNone(r['index_path'])          # 不生成清单
        self.assertEqual(list(Path(d).glob('*.md')), [])

    # ---------- --resume 断点续抓（F4） ----------

    def _pages(self):
        return [{'title': f'T{i}', 'msgid': str(i), 'itemidx': '1',
                 'url': f'http://mp.weixin.qq.com/s?__biz=B&mid={i}'}
                for i in (11, 12, 13)]

    def test_resume_skips_existing(self):
        """已存在的文章应跳过，且不发起抓取（不调用文章请求）。"""
        pages = self._pages()
        album_txt = json_mod.dumps({
            'base_resp': {'ret': 0},
            'getalbum_resp': {'article_list': pages, 'continue_flag': '0',
                              'base_info': {'title': 'C'}},
        })
        article_hits = []

        class Resp:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.content = b''
                self.encoding = None

            def raise_for_status(self):
                pass

        class Sess:
            def get(self, url, **k):
                if 'action=getalbum' in url:
                    return Resp(album_txt)
                article_hits.append(url)
                mm = re.search(r'mid=(\d+)', url)
                return Resp(f'<html><head><meta property="og:title" '
                            f'content="T{mm.group(1)}"></head><body>'
                            f'<div class="rich_media_content"><p>x</p>'
                            f'</div></body></html>')

        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: Sess()):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                with tempfile.TemporaryDirectory() as d:
                    # 预置第 1 篇已存
                    (Path(d) / (w.sanitize_filename('T11') + '.md')).write_text(
                        'placeholder', encoding='utf-8')
                    r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM',
                                        settings, vault='', out_dir=d,
                                        download_images=False, progress=False,
                                        resume=True)
                    self.assertIsNone(r['error'])
                    self.assertEqual(len(r['skipped']), 1)
                    self.assertEqual(r['skipped'][0]['title'], 'T11')
                    self.assertEqual(len(r['ok']), 2)
                    # 只对未存的两篇发起抓取
                    self.assertEqual(len(article_hits), 2)
                    # 清单须覆盖全部 3 篇（含跳过项）
                    idx = Path(r['index_path']).read_text(encoding='utf-8')
                    self.assertEqual(idx.count('[['), 3)

    def test_resume_off_fetches_all(self):
        """未开 resume 时不跳过，全部重新抓取。"""
        pages = self._pages()
        settings = {'image_prefix': 'wechat', 'request_timeout': 30,
                    'download_retries': 0}
        with mock.patch.object(w, '_session', lambda: self._sessions(pages)):
            with mock.patch.object(w.time, 'sleep', lambda *a: None):
                with tempfile.TemporaryDirectory() as d:
                    (Path(d) / (w.sanitize_filename('T11') + '.md')).write_text(
                        'placeholder', encoding='utf-8')
                    r = w.process_album('https://mp.weixin.qq.com/mp/appmsgalbum'
                                        '?__biz=BIZ&album_id=ALBUM',
                                        settings, vault='', out_dir=d,
                                        download_images=False, progress=False,
                                        resume=False)
                    self.assertEqual(r['skipped'], [])
                    self.assertEqual(len(r['ok']), 3)

    def test_resume_matches_numbered_duplicate(self):
        """重名副本 _1.md 也应视为已存。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / (w.sanitize_filename('T11') + '_1.md')).write_text(
                'x', encoding='utf-8')
            self.assertTrue(w._is_article_saved(Path(d), 'T11'))

    def test_is_article_saved_rejects_unrelated(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'T110.md').write_text('x', encoding='utf-8')
            self.assertFalse(w._is_article_saved(Path(d), 'T11'))
            (Path(d) / 'Other.md').write_text('x', encoding='utf-8')
            self.assertFalse(w._is_article_saved(Path(d), 'T11'))

    def test_is_article_saved_handles_blank_title(self):
        """标题清洗后为空/占位时不应误判为已存。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'article.md').write_text('x', encoding='utf-8')
            self.assertFalse(w._is_article_saved(Path(d), '???'))
            self.assertFalse(w._is_article_saved(Path(d), ''))


@skip_no_deps
class TestFetchArticle(unittest.TestCase):
    """用 mock session 验证抓取解析逻辑（E1：fetch + 日期 + 标题）。"""
    ARTICLE = (
        '<html><head><meta property="og:title" content="测试标题"></head>'
        '<body><div class="rich_media_content">'
        '<h2>小节</h2><p>正文内容</p>'
        '</div></body></html>'
    )

    def _fake_session(self, text=ARTICLE):
        class Resp:
            def __init__(self):
                self.status_code = 200
                self.text = text
                self.content = b''
                self.encoding = None
            def raise_for_status(self):
                pass
        class Sess:
            def get(self, *a, **k):
                return Resp()
        return Sess()

    def test_fetch_extracts_title_and_content(self):  # E1
        settings = {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}
        with mock.patch.object(w, '_session', lambda: self._fake_session()):
            soup, title, pub_date, content, error = w._fetch_article(
                'https://mp.weixin.qq.com/s/x', settings)
        self.assertIsNone(error)
        self.assertEqual(title, '测试标题')
        self.assertIsNotNone(content)
        self.assertRegex(pub_date, r'^\d{4}-\d{2}-\d{2}$')

    def test_fetch_no_content_returns_error(self):  # E1
        settings = {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}
        sess = self._fake_session(text='<html><body>无正文</body></html>')
        with mock.patch.object(w, '_session', lambda: sess):
            soup, title, pub_date, content, error = w._fetch_article(
                'https://mp.weixin.qq.com/s/x', settings)
        self.assertIsNone(content)
        self.assertIn('正文', error)

    def test_no_img_reports_image_count(self):  # 回归：--no-img 非 dry-run 应正确计数
        html = ('<html><head><meta property="og:title" content="T"></head>'
                '<body><div class="rich_media_content">'
                '<p>hi</p><img data-src="http://x/1.png"><img data-src="http://x/2.png">'
                '</div></body></html>')
        settings = {'image_prefix': 'wechat', 'request_timeout': 30, 'download_retries': 2}
        with mock.patch.object(w, '_session', lambda: self._fake_session(text=html)):
            with tempfile.TemporaryDirectory() as d:
                r = w.process_note('https://mp.weixin.qq.com/s/x', settings, vault='',
                                   out_dir=d, download_images=False, dry_run=False)
        self.assertIsNone(r['error'])
        self.assertEqual(r['images'], 2, "--no-img 应报告文中图片数")



if __name__ == '__main__':
    unittest.main()
