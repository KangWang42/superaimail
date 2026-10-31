import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from superaimail.engine import core
from superaimail.engine import imap_link
from superaimail import api as server
NOW = int(time.time())
CONTENT = {'2': b'PK-score-sheet-zhang', '3': b'PK-second-file'}

def fake_message(uid, subject, sender='', address='z@x.cn', files=(), received=NOW, text='正文'):
    return {'uid': uid, 'received': received, 'sent': received, 'from_name': sender, 'from_addr': address, 'to_addrs': 'me@x.cn', 'subject': subject, 'size': 100, 'message_id': '<%d@x>' % uid, 'body_part': '1', 'text': text, 'text_cut': False, 'attachments': [{'part': str(i + 2), 'name': name, 'type': 'application/octet-stream', 'size': 20, 'encoding': 'base64', 'inline': False} for i, name in enumerate(files)]}

class FakeWorkerTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.worker = core.Worker(self.dir, log=lambda text: None)
        self.worker.accounts.save_ai({'url': 'https://model.example.org/v1', 'model': 'test-model'})
        self.mail = {}
        self.patches = [(imap_link, 'fetch_messages', lambda client, uids: [self.mail[u] for u in uids if u in self.mail]), (imap_link, 'stream_part', lambda client, uid, part, enc, write, limit: write(CONTENT[part]) or len(CONTENT[part])), (imap_link, 'select', lambda client, folder: 'v1')]
        self.saved = [(module, name, getattr(module, name)) for module, name, _ in self.patches]
        for module, name, value in self.patches:
            setattr(module, name, value)
        record, _ = self.worker.accounts.save_account({'address': 'me@mail.example.org', 'imap_host': 'imap.example.org', 'secret': 'x', 'label': '测试邮箱', 'enabled': False})
        self.account = self.worker.accounts.account(record['id'])

    def tearDown(self):
        for module, name, value in self.saved:
            setattr(module, name, value)
        self.worker.store.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def ingest(self, *messages, live=True, folder='INBOX'):
        for message in messages:
            self.mail[message['uid']] = message
        self.worker.ingest(object(), self.account, folder, 'v1', [m['uid'] for m in messages], live)

    def notices(self):
        return [e['payload']['notice'] for e in self.worker.store.events(0) if e['kind'] == 'notice']

    def make_task(self, **extra):
        payload = dict({'name': '2026标书评审-评分表', 'account_id': self.account['id'], 'keywords': '评分表', 'start': NOW - 3600, 'deadline': NOW + 86400, 'roster': '张三\n李四'}, **extra)
        return self.worker.save_task(payload)

class Receiving(FakeWorkerTest):

    def test_new_mail_is_indexed_and_announced(self):
        self.ingest(fake_message(1, '您的验证码', text='验证码 482913'))
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn('验证码 482913', notices[0]['message'])
        self.assertIn('收件邮箱 me@mail.example.org', notices[0]['message'])
        listed = self.worker.store.messages()
        self.assertEqual(listed[0]['labels'], ['验证码'])
        text = self.worker.notice_text(notices[0]['id'])
        self.assertEqual(text['mails'][0]['text'], '验证码 482913')

    def test_backfill_and_junk_are_quiet(self):
        self.ingest(fake_message(1, '旧邮件'), live=False)
        self.ingest(fake_message(2, '广告'), live=True, folder='Junk')
        self.assertEqual(self.notices(), [])
        self.assertIn('垃圾邮件', self.worker.store.messages(folder='Junk')[0]['labels'])

    def test_rule_silences_and_labels(self):
        self.worker.accounts.save_rule({'label': '投稿', 'from': 'editorialmanager', 'notify': 'silent'})
        self.ingest(fake_message(1, 'Manuscript', address='em@editorialmanager.com'))
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.worker.store.messages(label='投稿')[0]['subject'], 'Manuscript')

class Collecting(FakeWorkerTest):

    def test_submission_is_filed_and_announced_once(self):
        self.make_task()
        self.ingest(fake_message(10, '张三+评分表', files=('评分表.xlsx',)))
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn('张三已提交（1/2）', notices[0]['title'])
        view = self.worker.task_view(self.worker.tasks()[0]['id'])
        self.assertEqual(view['counts']['submitted'], 1)
        self.assertEqual([p['name'] for p in view['people'] if p['status'] == 'missing'], ['李四'])
        self.ingest(fake_message(11, '周末聚餐'))
        self.assertEqual(view['counts']['mails'], 1)
        self.assertEqual([m['subject'] for m in self.worker.store.messages(task=view['id'])], ['张三+评分表'])
        manifest = self.worker.sync_manifest()
        self.assertEqual(manifest['files'][0]['path'], 'me@mail.example.org/2026标书评审-评分表/01 已提交/张三/张三_评分表.xlsx')
        with open(self.worker.file_path(manifest['files'][0]['file_id'])['path'], 'rb') as stream:
            self.assertEqual(stream.read(), CONTENT['2'])

    def test_backfill_after_task_creation_and_pending_assign(self):
        self.ingest(fake_message(10, '评分表', sender='Reviewer', address='r@x.cn', files=('新建 Microsoft Excel 工作表.xlsx',)), live=False)
        task = self.make_task()
        self.worker.ingest(object(), self.account, 'INBOX', 'v1', [10], False)
        view = self.worker.task_view(task['id'])
        self.assertEqual(view['counts']['pending'], 1)
        pending = view['pending'][0]
        view = self.worker.assign(pending['id'], '李四')
        self.assertEqual(view['counts']['submitted'], 1)
        self.assertEqual(self.worker.task(task['id'])['config']['senders'], {'r@x.cn': '李四'})
        self.ingest(fake_message(11, '修改后的材料', address='r@x.cn', files=('评分表v2.xlsx',)))
        view = self.worker.task_view(task['id'])
        self.assertEqual(view['people'][1]['count'], 2)
        paths = sorted((f['path'] for f in self.worker.sync_manifest()['files']))
        self.assertTrue(any(('/李四/历史版本/' in p for p in paths)))
        self.assertTrue(any((p.endswith('/李四/李四_评分表.xlsx') for p in paths)))

    def test_zip_matches_manifest(self):
        self.make_task()
        self.ingest(fake_message(10, '张三+评分表', files=('a.xlsx', 'b.xlsx')))
        sink = io.BytesIO()

        class Unseekable:

            def write(self, data):
                return sink.write(data)

            def flush(self):
                pass
        self.worker.write_zip('', Unseekable())
        names = zipfile.ZipFile(io.BytesIO(sink.getvalue())).namelist()
        self.assertEqual(sorted(names), sorted((f['path'] for f in self.worker.sync_manifest()['files'])))

    def test_end_task_stops_collecting(self):
        task = self.make_task()
        self.worker.end_task(task['id'], True)
        self.ingest(fake_message(10, '张三+评分表', files=('a.xlsx',)))
        self.assertEqual(self.worker.task_view(task['id'])['counts']['submitted'], 0)
        self.assertEqual(len(self.notices()), 1)

    def test_reminder_once(self):
        task = self.make_task(deadline=NOW + 3600, remind_hours=48)
        self.worker._reminders()
        self.worker._reminders()
        summaries = [n for n in self.notices() if n['id'].startswith('col-before')]
        self.assertEqual(len(summaries), 1)
        self.assertIn('未交：张三、李四', summaries[0]['message'])
        self.assertTrue(self.worker.task(task['id'])['state'].get('before'))

class Nightly(FakeWorkerTest):

    def setUp(self):
        super().setUp()
        self.worker.accounts.save_ai({'enabled': True})
        self.replies = []
        self.worker.ai.chat = lambda system, user, max_tokens=900: self.replies.pop(0)
        self.worker.attachment_preview = lambda message_id, part: {'sheets': []}

    def test_classify_candidates_and_rename(self):
        task = self.make_task(rename='ai')
        self.ingest(fake_message(1, 'Manuscript', address='em@x.com'), fake_message(2, '材料', sender='王', address='w@x.cn', files=('Book1.xlsx',)))
        import queue
        self.worker.links[self.account['id']] = type('L', (), {'queued': [], 'backfill': None, 'jobs': queue.Queue(), 'queue_collect': lambda self, t, items: self.queued.extend(items)})()
        self.replies = [json.dumps({'items': [{'n': 1, 'group': '其他', 'summary': 'x'}, {'n': 2, 'group': '工作与项目', 'summary': 'y'}]}), json.dumps({'items': [{'n': 1, 'belongs': True, 'person': '李四', 'reason': '附件是评分表'}]})]
        report = {'classified': 0, 'suggested': 0, 'renamed': 0, 'errors': []}
        self.worker._night_classify(report)
        self.worker._night_candidates(report)
        self.assertEqual((report['classified'], report['suggested']), (2, 1))
        groups = {m['subject']: m['group_name'] for m in self.worker.store.messages()}
        self.assertEqual(groups, {'Manuscript': '其他', '材料': '工作与项目'})
        queued = self.worker.links[self.account['id']].queued
        self.worker.collect_suggested(object(), self.account, task['id'], queued)
        pending = self.worker.task_view(task['id'])['pending'][0]
        self.assertEqual(pending['suggest'], '李四')
        self.assertIn('AI 判断', pending['reason'])
        self.worker.assign(pending['id'], '李四')
        self.replies = [json.dumps({'items': [{'n': 1, 'rename': True, 'name': '李四_评分表'}]})]
        self.worker._night_rename(report)
        self.assertEqual(report['renamed'], 1)
        self.assertTrue(self.worker.sync_manifest()['files'][0]['path'].endswith('/李四/李四_评分表.xlsx'))

    def test_manual_group_is_kept_and_ai_off_blocks(self):
        self.ingest(fake_message(1, 'x'))
        message_id = self.worker.store.messages()[0]['id']
        self.worker.set_message_group(message_id, '工作与项目')
        report = {'classified': 0, 'suggested': 0, 'renamed': 0, 'errors': []}
        self.replies = [json.dumps({'items': [{'n': 1, 'group': '其他', 'summary': '投稿已受理'}]})]
        self.worker._night_classify(report)
        row = self.worker.store.messages()[0]
        self.assertEqual((row['group_name'], row['ai_summary'], row['ai_tag']), ('工作与项目', '投稿已受理', None))
        self.replies = []
        self.worker._night_classify(report)
        self.worker.accounts.save_ai({'enabled': False})
        with self.assertRaises(core.WorkerError):
            self.worker.start_night()

class Modes(FakeWorkerTest):

    def test_each_mode_queues_new_inbox_mail_only(self):
        self.worker.accounts.save_ai({'enabled': True})
        self.assertEqual(self.worker.accounts.ai()['mode'], 'each')
        self.ingest(fake_message(1, 'a'), live=True)
        self.ingest(fake_message(2, 'b'), live=False)
        self.ingest(fake_message(3, 'c'), live=True, folder='Junk')
        self.assertEqual(self.worker.live_queue.qsize(), 1)
        self.worker.accounts.save_ai({'mode': 'daily'})
        self.ingest(fake_message(4, 'd'), live=True)
        self.assertEqual(self.worker.live_queue.qsize(), 1)

    def test_schedule_gaps_and_validation(self):
        from datetime import datetime
        from superaimail.engine import ai
        self.worker.accounts.save_ai({'enabled': True, 'mode': 'every3', 'night': '02:30'})
        at = lambda s: datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=ai.BEIJING).timestamp()
        self.worker.store.set_meta('night_last', {'date': '2026-10-24'})
        self.assertFalse(self.worker.night_due(at('2026-10-26 03:00')))
        self.assertTrue(self.worker.night_due(at('2026-10-27 03:00')))
        self.assertFalse(self.worker.night_due(at('2026-10-27 02:00')))
        self.worker.accounts.save_ai({'mode': 'daily'})
        self.assertTrue(self.worker.night_due(at('2026-10-25 02:31')))
        from superaimail.engine import accounts
        with self.assertRaises(accounts.AccountError):
            self.worker.accounts.save_ai({'mode': 'hourly'})

    def test_live_batch_sorts_its_mail(self):
        self.worker.accounts.save_ai({'enabled': True})
        self.worker.ai.chat = lambda system, user, max_tokens=900: '{"items": [{"n": 1, "group": "工作与项目", "summary": "投稿确认"}]}'
        self.ingest(fake_message(1, 'Manuscript received'), live=True)
        report = {'classified': 0, 'suggested': 0, 'renamed': 0, 'errors': []}
        self.worker._night_classify(report, [self.worker.live_queue.get_nowait()])
        self.assertEqual(report['classified'], 1)
        self.assertEqual(self.worker.store.messages()[0]['group_name'], '工作与项目')

class Reading(FakeWorkerTest):

    def test_stored_views_send_kept_text_without_mail_server_or_read_mark(self):
        self.ingest(fake_message(1, '第一封', text='正文一'), fake_message(2, '第二封', text='正文二'))
        by_subject = {m['subject']: m for m in self.worker.store.messages()}
        first, second = (by_subject['第一封'], by_subject['第二封'])
        self.worker.store.update_message(second['id'], text=None, text_at=None)
        imap_link.fetch_messages = lambda client, uids: self.fail('must not reach the mail server')
        views = self.worker.stored_views([first['id'], second['id'], first['id'], 'missing'])
        self.assertEqual([v['id'] for v in views], [first['id']])
        self.assertEqual(views[0]['text'], '正文一')
        self.assertIn('account', views[0])
        self.assertFalse(self.worker.store.message(first['id'])['read'])
        opened = self.worker.message_view(first['id'])
        self.assertTrue(opened['read'])
        self.assertEqual({k: v for k, v in opened.items() if k != 'read'}, {k: v for k, v in views[0].items() if k != 'read'})
        self.assertLessEqual(len(self.worker.stored_views([str(i) for i in range(100)])), core.BODIES_MAX)

    def test_text_is_kept_as_long_as_the_message(self):
        self.ingest(fake_message(1, '旧邮件', text='正文', received=NOW - 100 * 86400))
        message = self.worker.store.messages()[0]
        self.worker.store.update_message(message['id'], text_at=NOW - 100 * 86400)
        self.worker.store.purge(NOW)
        self.assertEqual(self.worker.store.message(message['id'])['text'], '正文')
        self.worker.store.update_message(message['id'], text_at=NOW - 200 * 86400, starred=1)
        self.worker.store.purge(NOW)
        self.assertIsNone(self.worker.store.message(message['id'])['text'])

class Http(FakeWorkerTest):

    def setUp(self):
        super().setUp()
        server.Handler.worker, server.Handler.token = (self.worker, 't' * 40)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, payload=None, token='t' * 40):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return (reply.status, reply.read(), reply.headers)
        except urllib.error.HTTPError as reply:
            return (reply.code, reply.read(), reply.headers)

    def test_auth_actions_and_file_download(self):
        self.assertEqual(self.call('/v1/overview', token='wrong')[0], 401)
        code, body, _ = self.call('/v1/overview')
        overview = json.loads(body)
        self.assertEqual(code, 200)
        self.assertNotIn('secret', json.dumps(overview))
        code, body, _ = self.call('/v1/action', {'action': 'task_save', 'name': '评分表收集', 'account_id': self.account['id'], 'keywords': ['评分表'], 'start': NOW - 60})
        self.assertEqual(code, 200)
        self.ingest(fake_message(10, '张三+评分表', files=('a.xlsx',)))
        manifest = json.loads(self.call('/v1/sync')[1])
        code, body, headers = self.call('/v1/file?id=' + manifest['files'][0]['file_id'])
        self.assertEqual((code, body), (200, CONTENT['2']))
        self.assertIn("filename*=UTF-8''a.xlsx", headers['Content-Disposition'])
        code, body, _ = self.call('/v1/action', {'action': 'task_save', 'name': '', 'account_id': self.account['id']})
        self.assertEqual(code, 400)
        self.assertIn('任务名称', json.loads(body)['error'])
        events = json.loads(self.call('/v1/events?after=0')[1])
        self.assertEqual(len(events['events']), 1)

    def test_one_connection_serves_several_requests(self):
        import http.client
        self.ingest(fake_message(1, '正文邮件', text='保存的正文'))
        mail_id = self.worker.store.messages()[0]['id']
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        headers = {'Authorization': 'Bearer ' + 't' * 40}
        try:
            connection.request('GET', '/v1/overview', headers=headers)
            first = connection.getresponse()
            first.read()
            port = connection.sock.getsockname()
            connection.request('GET', '/v1/bodies?ids=' + mail_id + '%2Cmissing', headers=headers)
            reply = connection.getresponse()
            body = json.loads(reply.read())
            self.assertEqual((first.status, reply.status, reply.will_close), (200, 200, False))
            self.assertEqual(connection.sock.getsockname(), port)
            self.assertEqual([m['text'] for m in body['messages']], ['保存的正文'])
            connection.request('POST', '/v1/action', body=b'{"action":"x"}', headers={'Authorization': 'Bearer wrong', 'Content-Type': 'application/json'})
            refused = connection.getresponse()
            refused.read()
            self.assertEqual((refused.status, refused.will_close), (401, True))
        finally:
            connection.close()

class Attention(FakeWorkerTest):
    """Owner rule beats the model, the model beats the group default; lists, counts and bulk actions follow the level."""

    def setUp(self):
        super().setUp()
        self.worker.accounts.save_ai({'enabled': True})
        self.worker.ai.chat = lambda system, user, max_tokens=900: json.dumps({'items': [{'n': 1, 'group': '工作与项目', 'tag': '最终决定', 'summary': 's', 'level': 'act'}, {'n': 2, 'group': '订阅与推广', 'tag': '推广', 'summary': 's', 'level': 'low'}, {'n': 3, 'group': '账号与安全', 'tag': '验证码', 'summary': 's', 'level': 'nonsense'}]})
        self.ingest(fake_message(1, 'Final Decision', address='em@x.com'), fake_message(2, 'Sale', address='ad@shop.com'), fake_message(3, 'Code', address='no@x.com'))
        self.worker._night_classify({'classified': 0, 'suggested': 0, 'renamed': 0, 'errors': []})
        self.ids = {m['subject']: m['id'] for m in self.worker.store.messages()}

    def level(self, subject):
        return self.worker.store.message(self.ids[subject])['attention']

    def test_levels_rules_and_lists(self):
        self.assertEqual([self.level(s) for s in ('Final Decision', 'Sale', 'Code')], ['act', 'low', 'read'])
        self.assertEqual(self.worker.store.message(self.ids['Code'])['ai_level'], '')
        self.worker.ai.chat = lambda *a, **k: self.fail('nothing left to classify')
        self.worker._night_classify({'classified': 0, 'suggested': 0, 'renamed': 0, 'errors': []})
        group = next((g for g in self.worker.accounts.groups() if g['name'] == '账号与安全'))
        self.worker.save_group(dict(group, attention='fyi'))
        self.assertEqual(self.level('Code'), 'fyi')
        self.worker.attention_rule({'id': self.ids['Final Decision'], 'level': 'low'})
        self.assertEqual(self.level('Final Decision'), 'low')
        self.assertEqual({m['subject'] for m in self.worker.store.messages(level='quiet')}, {'Final Decision', 'Sale', 'Code'})
        key = next(iter(self.worker.store.meta('attention_rules')))
        self.worker.remove_attention_rule(key)
        self.assertEqual(self.level('Final Decision'), 'act')
        self.assertEqual([m['subject'] for m in self.worker.store.messages(level='focus')], ['Final Decision'])
        counts = self.worker.overview()['attention_counts']
        self.assertEqual((counts['act']['unread'], counts['low']['total'], counts['fyi']['total']), (1, 1, 1))
        with self.assertRaises(ValueError):
            self.worker.store.messages(level='urgent')

    def test_quiet_bulk_read_and_archive_spare_starred(self):
        from superaimail import api as server
        group = next((g for g in self.worker.accounts.groups() if g['name'] == '账号与安全'))
        self.worker.save_group(dict(group, attention='fyi'))
        self.worker.store.update_message(self.ids['Sale'], starred=1)
        self.assertEqual(server.actions(self.worker, {'action': 'messages_read', 'level': 'quiet', 'folder': 'INBOX'})['count'], 2)
        self.assertFalse(self.worker.store.message(self.ids['Final Decision'])['read'])
        self.assertEqual(server.actions(self.worker, {'action': 'messages_archive', 'level': 'quiet', 'folder': 'INBOX'})['count'], 1)
        archived = {s for s, i in self.ids.items() if self.worker.store.message(i)['archived']}
        self.assertEqual(archived, {'Code'})
        self.assertEqual([q['count'] for q in self.worker.store.quiet_senders()], [])
if __name__ == '__main__':
    unittest.main()
