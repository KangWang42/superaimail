"""Reading pages: cleaning keeps a mail's look and drops anything that could run; both views read well."""
import unittest
import zlib
from superaimail.engine import imap_link
from superaimail.engine import mail_html
from test_core import FakeWorkerTest, fake_message

def body(page):
    return page[page.index('<body>'):]

class CleanTests(unittest.TestCase):

    def test_nothing_that_runs_survives(self):
        dirty = '<script>alert(1)</script><img src="x" onerror="alert(2)"><a href="javascript:alert(3)">x</a><a href=" JaVaScRiPt:alert(4)">y</a><iframe src="https://evil.example"></iframe><form action="https://evil.example"><input name="p"><button>go</button></form><svg onload="alert(5)"><script>alert(6)</script></svg><div style="background:url(javascript:alert(7));width:expression(alert(8))">t</div><object data="x"></object><img src="data:image/svg+xml;base64,PHN2Zz4="><meta http-equiv="refresh" content="0;url=https://evil.example"><base href="https://evil.example/"><link rel="stylesheet" href="https://evil.example/a.css"><p onclick="x()">safe</p><style>@import url(https://evil.example/b.css); p{color:red;behavior:url(x.htc)}</style>'
        cleaned = mail_html.clean(dirty)
        for bad in ('<script', 'onerror', 'onclick', 'onload', 'javascript', '<iframe', '<form', '<input', '<button', '<svg', 'expression', '<object', 'svg+xml', '<meta', '<base', '<link', '@import', 'behavior', 'evil.example'):
            self.assertNotIn(bad, cleaned.lower(), bad)
        self.assertIn('safe', cleaned)
        self.assertIn('color:red', cleaned)

    def test_links_open_outside_and_keep_their_address(self):
        cleaned = mail_html.clean('<a href="https://example.org/a?b=1&amp;c=2">Yes, I am affiliated.</a><a href="mailto:x@y.z">m</a>')
        self.assertIn('href="https://example.org/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer"', cleaned)
        self.assertIn('href="mailto:x@y.z"', cleaned)
        self.assertEqual(mail_html.links(cleaned), [{'label': 'Yes, I am affiliated.', 'url': 'https://example.org/a?b=1&c=2', 'host': 'example.org'}])

    def test_inline_images_point_at_their_part(self):
        cleaned = mail_html.clean('<img src="cid:logo@x"><img src="cid:unknown">', {'<logo@x>': '2'})
        self.assertIn('src="inline:2"', cleaned)
        self.assertEqual(cleaned.count('<img'), 1)
        page = mail_html.page(cleaned, '', inline_url=lambda part: 'inline?id=m&part=' + part)
        self.assertIn('src="inline?id=m&amp;part=2"', page)

    def test_text_drops_invisible_padding_and_hidden_preview(self):
        cleaned = mail_html.clean('<div style="display:none;max-height:0">preview line</div><p>Hello͏\u200b&nbsp;there</p>' + '͏ ͏\n' * 30 + '<table><tr><td>订单号</td><td>FF1</td></tr><tr><td>数量</td><td>x2</td></tr></table><ul><li>one</li><li>two</li></ul>')
        self.assertEqual(mail_html.text(cleaned), 'Hello there\n\n订单号 FF1\n数量 x2\n\n• one\n• two')

class PageTests(unittest.TestCase):
    MAIL = '<style>.hero{color:#123456}</style><div style="display:none">preview</div><table width="600"><tr><td><img src="https://cdn.example/banner.png" width="600" height="200" alt="banner"><img src="https://track.example/p.gif" width="1" height="1"><h1 class="hero">Title</h1><table><tr><td>订单号</td><td>FF1</td></tr><tr><td>实付</td><td>¥2.34</td></tr></table><table><tr><td>•</td><td>Tech setup</td></tr></table><td>&nbsp;</td><p><a href="https://shop.example/order"><img src="https://cdn.example/btn.png" alt="查看订单"></a></p></td></tr></table><div class="gmail_quote">On Mon wrote: old</div>'

    def test_original_keeps_layout_and_blocks_remote_images_until_allowed(self):
        cleaned = mail_html.clean(self.MAIL)
        page = mail_html.page(cleaned, '', 'original', images=False)
        self.assertIn('<meta name="mail-has-html" content="1">', page)
        self.assertIn('<meta name="mail-blocked-images" content="2">', page)
        self.assertNotIn('cdn.example', body(page))
        self.assertNotIn('track.example', page)
        self.assertIn('color:#123456', page)
        self.assertIn('width="600"', page)
        shown = mail_html.page(cleaned, '', 'original', images=True)
        self.assertIn('src="https://cdn.example/banner.png"', shown)
        self.assertIn('<meta name="mail-blocked-images" content="0">', shown)
        self.assertNotIn('track.example', shown)

    def test_simple_view_is_our_typography(self):
        page = mail_html.page(mail_html.clean(self.MAIL), '', 'simple', theme='dark')
        content = body(page)
        self.assertIn('#e6edf3', page)
        self.assertNotIn('preview', content)
        self.assertNotIn('width=', content)
        self.assertNotIn('hero', content)
        self.assertIn('class="kv"', content)
        self.assertIn('class="kv bullet"', content)
        self.assertIn('<span class="img-alt">查看订单</span>', content)
        self.assertNotIn('banner', content)
        self.assertIn('<details class="quote"><summary>显示引用的原文</summary>', content)

    def test_dark_page_turns_light_backgrounds_and_dark_text(self):
        mail = mail_html.clean('<style>.card{background:#ffffff;color:#333} @media (prefers-color-scheme: dark){.card{background:#000}}</style><table bgcolor="#F1F5F9"><tr><td class="card" style="color:#111827;border:1px solid #e5e7eb">Hi</td><td style="background-color:#1a73e8;color:#fff">Button</td><td><font color="black">x</font></td></tr></table><img src="https://cdn.example/a.png" style="background:#fff">')
        page = mail_html.page(mail, '', 'original', theme='dark', images=True)
        self.assertIn('<meta name="mail-theme" content="dark">', page)
        self.assertIn('color-scheme:dark', page)
        self.assertNotIn('prefers-color-scheme', page)
        self.assertNotIn('bgcolor="#F1F5F9"', page)
        self.assertNotIn('#ffffff', page)
        self.assertNotIn('#111827', page)
        self.assertIn('background-color:#1a73e8', page)
        self.assertIn('color:#fff', page)
        self.assertIn('src="https://cdn.example/a.png"', page)
        light = mail_html.page(mail, '', 'original', theme='light', images=True)
        self.assertIn('bgcolor="#F1F5F9"', light)
        self.assertIn('<meta name="mail-theme" content="light">', light)

    def test_text_on_a_bright_brand_button_stays_dark(self):
        mail = mail_html.clean('<style>.cta{background-color:#00ffcc}</style><table><tr><td class="cta"><a style="color:#000" href="https://x.example">GO</a></td><td style="background:#ffeb3b"><span>Sale</span></td><td bgcolor="#1a73e8" style="color:#fff">Blue</td></tr></table>')
        page = mail_html.page(mail, '', 'original', theme='dark', images=True)
        self.assertIn('class="cta mail-on-light"', page)
        self.assertIn('<td style="background:#7e7312"><span>', page)
        self.assertNotIn('bgcolor="#1a73e8" style="color:#fff" class', page)
        self.assertIn('.mail-on-light,.mail-on-light *{color:#1f2328!important}', page)

    def test_colour_values_with_extra_words_do_not_break_the_dark_page(self):
        mail = mail_html.clean('<table bgcolor="#ffffff !" style="background:#fff !important;color:#zzz"><tr><td bgcolor="#12">x</td></tr></table>')
        page = mail_html.page(mail, '', 'original', theme='dark', images=True)
        self.assertIn('<meta name="mail-theme" content="dark">', page)
        self.assertIn('>x</td>', page)

    def test_plain_text_mail(self):
        text = 'Dear Reader,\n\nPlease confirm https://www.editorialmanager.com/pmedr/l.asp?i=1234567&l=EXAMPLEX-very-long-tracking-token\n> quoted line\n\nThanks\n-- \nEditor\n\n在 2026年10月25日 写道：\n> earlier'
        content = body(mail_html.page('', text, 'original'))
        self.assertIn('<meta name="mail-has-html" content="0">', mail_html.page('', text))
        self.assertIn('<p class="para">Dear Reader,</p>', content)
        self.assertIn('>www.editorialmanager.com/pmedr/l.asp?i=1234567&amp;l…</a>', content)
        self.assertIn('<blockquote>quoted line</blockquote>', content)
        self.assertIn('<div class="sig"><p class="para">Editor</p></div>', content)
        self.assertIn('<details class="quote"><summary>显示引用的原文</summary>', content)

class WorkerReadingTests(FakeWorkerTest):

    def test_html_kept_at_ingest_and_paged(self):
        message = fake_message(1, '订单已发货通知', text='订单号FF1')
        message.update(html=mail_html.clean('<p><a href="https://x.example/confirm">Confirm your email</a></p>'), html_inline={})
        self.ingest(message)
        key = self.worker.store.messages()[0]['id']
        view = self.worker.message_view(key, mark_read=False)
        self.assertTrue(view['has_html'])
        self.assertEqual(view['action_links'][0]['kind'], 'action')
        self.assertEqual(view['action_links'][0]['label'], 'Confirm your email')
        raw = self.worker.store.row('SELECT html FROM messages WHERE id=?', (key,))['html']
        self.assertIn(b'confirm', zlib.decompress(raw))
        self.assertIn('Confirm your email', self.worker.reading_page(key, 'simple'))

    def test_mail_indexed_before_html_is_read_once_on_opening(self):
        self.ingest(fake_message(2, '旧邮件', text='old flattened text'))
        key = self.worker.store.messages()[0]['id']
        self.worker.store.db.execute('UPDATE messages SET html=NULL WHERE id=?', (key,))
        calls = []
        saved = (imap_link.fetch_reading, imap_link.connect, imap_link.close)
        imap_link.connect, imap_link.close = (lambda account, proxy: object(), lambda client: None)
        imap_link.fetch_reading = lambda client, uid: calls.append(uid) or {'text': 'Better text', 'text_cut': False, 'body_part': '1', 'html': mail_html.clean('<p>Better <b>text</b></p>'), 'html_inline': {}}
        try:
            self.assertIn('<b>text</b>', self.worker.reading_page(key, 'original'))
            self.assertIn('<b>text</b>', self.worker.reading_page(key, 'original'))
        finally:
            imap_link.fetch_reading, imap_link.connect, imap_link.close = saved
        self.assertEqual(calls, [2])
        self.worker.store.db.execute('UPDATE messages SET html=NULL WHERE id=?', (key,))
        imap_link.connect, imap_link.close = (lambda account, proxy: object(), lambda client: None)
        imap_link.fetch_reading = lambda client, uid: {'text': 'Better text', 'text_cut': False, 'body_part': '1', 'html_inline': {}, 'html': mail_html.clean('<a href="https://x.example/ok">Confirm your email</a>')}
        try:
            view = self.worker.message_view(key, mark_read=False)
        finally:
            imap_link.fetch_reading, imap_link.connect, imap_link.close = saved
        self.assertTrue(view['has_html'])
        self.assertEqual(view['action_links'][0]['label'], 'Confirm your email')
        self.assertEqual(self.worker.store.message(key)['text'], 'Better text')
        self.assertEqual(self.worker.store.message(key)['snippet'], 'Better text')

    def test_background_backfill_reads_older_mail_once_per_folder_and_keeps_retention(self):
        self.ingest(fake_message(5, 'A', text='a'), fake_message(6, 'B', text='b'))
        keys = [m['id'] for m in self.worker.store.messages()]
        self.worker.store.db.execute('UPDATE messages SET html=NULL, text_at=111')
        sessions, fetched = ([], [])
        active = dict(self.account, enabled=True)
        self.worker.accounts.account = lambda account_id: active
        saved = (imap_link.fetch_reading, imap_link.connect, imap_link.close)
        imap_link.connect, imap_link.close = (lambda account, proxy: sessions.append(1) or object(), lambda client: None)
        imap_link.fetch_reading = lambda client, uid: fetched.append(uid) or {'text': 't%d' % uid, 'text_cut': False, 'body_part': '1', 'html': mail_html.clean('<p>x</p>'), 'html_inline': {}}
        try:
            self.assertEqual(self.worker.fill_html_backlog(), 2)
            self.assertEqual(self.worker.fill_html_backlog(), 0)
        finally:
            imap_link.fetch_reading, imap_link.connect, imap_link.close = saved
        self.assertEqual((len(sessions), sorted(fetched)), (1, [5, 6]))
        for key in keys:
            self.assertTrue(self.worker.store.message(key)['has_html'])
            self.assertEqual(self.worker.store.message(key)['text_at'], 111)

    def test_show_images_preference_defaults_on(self):
        self.assertTrue(self.worker.mail_preferences()['show_images'])
        self.assertFalse(self.worker.save_mail_preferences({'show_images': False})['mail_preferences']['show_images'])

    def test_inline_image_only_for_parts_the_page_uses(self):
        message = fake_message(3, '图片')
        message.update(html=mail_html.clean('<img src="cid:a">', {'a': '2'}), html_inline={'2': {'type': 'image/png', 'encoding': 'base64'}})
        self.ingest(message)
        key = self.worker.store.messages()[0]['id']
        self.assertEqual(self.worker.inline_image(key, '2')[0], 'image/png')
        with self.assertRaises(Exception):
            self.worker.inline_image(key, '3')
if __name__ == '__main__':
    unittest.main()
