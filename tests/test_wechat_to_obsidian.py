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
