import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from .templatetags.demo_extras import demo_static
from .tests import AuthenticatedTestCase


class AssetVersionTests(SimpleTestCase):
    def test_updated_asset_gets_new_url_and_unchanged_asset_keeps_url(self):
        with TemporaryDirectory() as directory:
            asset = Path(directory) / 'style.css'
            asset.write_bytes(b'old layout')
            with patch('demo.templatetags.demo_extras.finders.find', return_value=str(asset)), \
                 patch('demo.templatetags.demo_extras.static', return_value='/static/style.css?existing=1#anchor'):
                old_url = demo_static('style.css')
                self.assertEqual(old_url, demo_static('style.css'))
                timestamp = asset.stat().st_mtime_ns
                asset.write_bytes(b'new layout')
                os.utime(asset, ns=(timestamp + 1000000000, timestamp + 1000000000))
                new_url = demo_static('style.css')
                self.assertNotEqual(old_url, new_url)
                self.assertEqual(parse_qs(urlsplit(new_url).query)['existing'], ['1'])
                self.assertEqual(urlsplit(new_url).fragment, 'anchor')

    def test_external_asset_without_local_source_keeps_storage_url(self):
        with patch('demo.templatetags.demo_extras.finders.find', return_value=None), \
             patch('demo.templatetags.demo_extras.static', return_value='/static/style.a1b2.css'):
            self.assertEqual(demo_static('style.css'), '/static/style.a1b2.css')


class PageAssetVersionTests(AuthenticatedTestCase):
    def test_login_and_authenticated_pages_load_current_styles(self):
        for path in ('/login/', '/audit/'):
            with self.subTest(page=path):
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200)
                html = page.content.decode('utf-8')
                self.assertIn(demo_static('demo/style.css'), html)
                self.assertRegex(html, r'/static/demo/style\.css\?v=[0-9a-f]+')
                if path == '/audit/':
                    self.assertIn(demo_static('demo/ui.js'), html)
