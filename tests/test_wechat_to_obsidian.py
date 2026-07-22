#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_to_obsidian 关键纯函数单元测试。

运行:
    cd 仓库根目录
    python -m unittest tests.test_wechat_to_obsidian
    # 或
    python -m unittest discover tests
"""
import re
import sys
import unittest
from pathlib import Path

# 将 scripts/ 加入搜索路径以导入被测模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import wechat_to_obsidian as w  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402


def _make_cell_inline(img_map=None, ref=None):
    """构造与 html_to_markdown 内部 cell_inline 等价的闭包，用于测试 _table_to_markdown。"""
    img_map = img_map if img_map is not None else {}
    ref = ref or (lambda f: f"![](images/{f})")

    def cell_inline(cell):
        text = w._render_inline(cell, img_map, ref, ' ')
        text = re.sub(r'\s+', ' ', text).strip()
        return text.replace('|', '\\|')
    return cell_inline


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
        self.assertTrue(w.is_safe_url('https://mmbiz.qpic.cn/a.jpg'))
        self.assertTrue(w.is_safe_url('http://example.com/a.png'))

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


class TestTableMarkdown(unittest.TestCase):
    def test_simple_table(self):
        html = '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _make_cell_inline())
        self.assertIn('| A | B |', result)
        self.assertIn('| --- | --- |', result)
        self.assertIn('| 1 | 2 |', result)

    def test_pipe_escaped(self):
        html = '<table><tr><th>H</th></tr><tr><td>a|b</td></tr></table>'
        soup = BeautifulSoup(html, 'html.parser')
        result = w._table_to_markdown(soup.find('table'), _make_cell_inline())
        self.assertIn('a\\|b', result)


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


if __name__ == '__main__':
    unittest.main()
