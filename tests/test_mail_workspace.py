"""Mail workspace operations use an isolated worker, never a real mailbox."""
import sqlite3
import time
import unittest
from unittest import mock
from test_core import FakeWorkerTest, fake_message
from superaimail import api as server
from superaimail.engine.store import Store

class WorkspaceTests(FakeWorkerTest):

    def test_archive_trash_restore_and_counts(self):
        self.ingest(fake_message(1, 'First'), fake_message(2, 'Second'))
        ids = [m['id'] for m in self.worker.store.messages()]
        server.actions(self.worker, {'action': 'messages_mark', 'ids': ids, 'archived': True})
        self.assertEqual(self.worker.store.messages(), [])
        self.assertEqual(len(self.worker.store.messages(view='archive')), 2)
        self.assertEqual(self.worker.overview()['unread'], 0)
        server.actions(self.worker, {'action': 'messages_mark', 'ids': ids, 'deleted': True})
        self.assertEqual(len(self.worker.store.messages(view='trash')), 2)
        self.assertEqual(self.worker.store.messages(view='archive'), [])
        server.actions(self.worker, {'action': 'messages_mark', 'ids': ids, 'deleted': False, 'archived': False})
        self.assertEqual(len(self.worker.store.messages()), 2)

    def test_list_shows_the_tasks_a_mail_was_filed_to(self):
        self.ingest(fake_message(1, '材料'), fake_message(2, '其他'))
        ids = {m['subject']: m['id'] for m in self.worker.store.messages()}
        store = self.worker.store
        store.execute("INSERT INTO tasks (id, created, enabled, config, state) VALUES ('t1', 1, 1, ?, '{}')", ('{"name": "示例评审"}',))
        store.execute("INSERT INTO tasks (id, created, enabled, config, state) VALUES ('t2', 1, 1, ?, '{}')", ('{"name": "照片"}',))
        store.execute("INSERT INTO submissions (id, task_id, message_id, pending, ignored, received) VALUES ('s1', 't1', ?, 0, 0, 1)", (ids['材料'],))
        store.execute("INSERT INTO submissions (id, task_id, message_id, pending, ignored, received) VALUES ('s2', 't2', ?, 1, 1, 2)", (ids['材料'],))
        listed = {m['subject']: m['tasks'] for m in store.messages()}
        self.assertEqual(listed['材料'], [{'id': 't1', 'name': '示例评审', 'pending': False}])
        self.assertEqual(listed['其他'], [])

    def test_read_all_covers_exactly_the_filtered_list(self):
        self.ingest(fake_message(1, 'Bill', address='a@example.org'), fake_message(2, 'Note', address='b@example.org'), fake_message(3, 'Other bill', address='b@example.org'))
        ids = {m['subject']: m['id'] for m in self.worker.store.messages()}
        group = self.worker.accounts.groups()[0]['name']
        self.worker.mark_messages({'ids': [ids['Bill'], ids['Note']], 'group': group})
        self.worker.mark_messages({'ids': [ids['Note']], 'archived': True})
        result = server.actions(self.worker, {'action': 'messages_read', 'group': group, 'view': 'inbox'})
        self.assertEqual(result['count'], 1)
        unread = {m['subject'] for m in self.worker.store.messages(view='all') if not m['read']}
        self.assertEqual(unread, {'Note', 'Other bill'})
        self.assertEqual(server.actions(self.worker, {'action': 'messages_read', 'q': 'bill'})['count'], 1)
        self.assertEqual(server.actions(self.worker, {'action': 'messages_read', 'account_id': self.account['id'], 'view': 'all'})['count'], 1)
        self.assertEqual(self.worker.overview()['unread'], 0)

    def test_bulk_missing_id_has_no_partial_writes(self):
        self.ingest(fake_message(1, 'First'))
        m = self.worker.store.messages()[0]
        with self.assertRaises(ValueError):
            self.worker.mark_messages({'ids': [m['id'], 'missing'], 'deleted': True})
        self.assertFalse(self.worker.store.message(m['id'])['deleted'])
        with self.assertRaises(ValueError):
            self.worker.mark_messages({'ids': [m['id']], 'deleted': 'false'})

    def test_sender_rule_exact_address_account_and_future_mail(self):
        self.ingest(fake_message(1, 'First', address='sender@example.com'), fake_message(2, 'Other', address='notsender@example.com'))
        m = self.worker.store.messages(sender='sender@example.com')[0]
        group = self.worker.accounts.groups()[0]['name']
        self.worker.sender_rule({'id': m['id'], 'group': group})
        self.ingest(fake_message(3, 'Next', address='SENDER@example.com'))
        matched = self.worker.store.messages(group=group)
        self.assertEqual(len(matched), 2)
        self.assertTrue(all((m['group_manual'] for m in matched)))
        self.worker.sender_rule({'id': m['id'], 'group': ''})
        self.ingest(fake_message(4, 'After removal', address='sender@example.com'))
        self.assertEqual(len(self.worker.store.messages(group=group)), 2)

    def test_read_preference(self):
        self.ingest(fake_message(1, 'First'))
        identifier = self.worker.store.messages()[0]['id']
        self.worker.save_mail_preferences({'read_on_open': False})
        self.assertFalse(self.worker.message_view(identifier)['read'])
        self.worker.save_mail_preferences({'read_on_open': True})
        self.assertTrue(self.worker.message_view(identifier)['read'])

    def test_sender_rules_follow_group_rename_and_deletion(self):
        self.ingest(fake_message(1, 'First', address='sender@example.org'))
        identifier = self.worker.store.messages()[0]['id']
        group = self.worker.accounts.groups()[0]
        self.worker.sender_rule({'id': identifier, 'group': group['name']})
        self.worker.save_group({'id': group['id'], 'name': '新分组', 'description': 'test'})
        self.ingest(fake_message(2, 'Second', address='sender@example.org'))
        self.assertEqual(len(self.worker.store.messages(group='新分组')), 2)
        self.worker.delete_group(group['id'])
        self.assertEqual(self.worker.store.meta('sender_rules'), {})
        self.ingest(fake_message(3, 'Third', address='sender@example.org'))
        self.assertEqual(self.worker.store.messages(group='新分组'), [])

    def test_auto_archive_opt_in_and_starred_exception(self):
        now = int(time.time())
        self.ingest(fake_message(1, '验证码 482913', received=now - 1000), fake_message(2, '验证码 482914', received=now - 1001))
        self.worker.mail_maintenance()
        self.assertEqual(len(self.worker.store.messages()), 2)
        kept = self.worker.store.messages()[0]
        self.worker.store.update_message(kept['id'], starred=1)
        self.worker.save_mail_preferences({'code_archive_minutes': 10})
        self.worker.mail_maintenance()
        self.assertEqual(len(self.worker.store.messages()), 1)
        self.assertEqual(self.worker.store.messages()[0]['id'], kept['id'])
        self.assertEqual(len(self.worker.store.messages(view='archive')), 1)

    def test_snooze_restores_once_and_notifies(self):
        self.ingest(fake_message(1, 'Review'))
        m = self.worker.store.messages()[0]
        self.worker.mark_messages({'ids': [m['id']], 'snoozed_until': int(time.time()) + 100})
        self.assertEqual(self.worker.store.messages(), [])
        self.assertEqual(len(self.worker.store.messages(view='snoozed')), 1)
        self.worker.store.update_message(m['id'], snoozed_until=int(time.time()) - 1)
        before = len(self.notices())
        self.worker.mail_maintenance()
        self.worker.mail_maintenance()
        self.assertEqual(len(self.notices()), before + 1)
        self.assertEqual(len(self.worker.store.messages()), 1)

    def test_tied_timestamp_cursor(self):
        for uid in range(1, 6):
            self.ingest(fake_message(uid, 'same time'))
        page = self.worker.store.messages(limit=2)
        second = self.worker.store.messages(limit=5, before=page[-1]['received'], before_id=page[-1]['id'])
        self.assertEqual(len(page + second), 5)
        self.assertEqual(len({m['id'] for m in page + second}), 5)

    def test_group_unread_matches_active_view(self):
        self.ingest(fake_message(1, 'First'), fake_message(2, 'Second'))
        ids = [m['id'] for m in self.worker.store.messages()]
        group = self.worker.accounts.groups()[0]['name']
        self.worker.mark_messages({'ids': ids, 'group': group})
        self.worker.mark_messages({'ids': [ids[0]], 'read': True})
        overview = self.worker.overview()
        self.assertEqual(overview['group_counts'][group], 2)
        self.assertEqual(overview['group_unread'][group], 1)

    def test_no_roster_latest_zip_keeps_history_recoverable(self):
        import io
        import zipfile
        task = self.make_task(roster='', start=int(time.time()) - 100)
        self.ingest(fake_message(10, '张三+评分表', files=('评分表.xlsx',), received=int(time.time()) - 10))
        self.ingest(fake_message(11, '张三+评分表', files=('评分表.xlsx',), received=int(time.time())))
        view = self.worker.task_view(task['id'])
        self.assertEqual(view['counts']['expected'], 0)
        self.assertEqual(view['counts']['submitted'], 1)
        self.assertEqual(view['counts']['duplicates'], 1)
        for latest, count in [(True, 1), (False, 2)]:
            out = io.BytesIO()
            self.worker.write_zip(task['id'], out, latest_only=latest)
            with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:
                self.assertEqual(len(z.namelist()), count)
        self.assertEqual(len(self.worker.sync_manifest()['files']), 2)

    def test_sender_archive_is_exact_and_scoped(self):
        self.ingest(fake_message(1, 'A', address='a@example.org'), fake_message(2, 'B', address='ba@example.org'))
        self.assertEqual(len(self.worker.store.senders()), 2)
        count = self.worker.store.archive_sender(self.account['id'], 'A@example.org')
        self.assertEqual(count, 1)
        self.assertEqual(self.worker.store.messages()[0]['from_addr'], 'ba@example.org')

    def test_sent_copy_does_not_enter_inbox_or_unread_count(self):
        self.worker.record_sent(self.account, {'to': ['recipient@example.org'], 'subject': 'sent', 'body': 'body'}, '<test@example.org>')
        self.assertEqual(self.worker.store.messages(), [])
        sent = self.worker.store.messages(view='sent')
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['read'])
        self.assertEqual(self.worker.overview()['unread'], 0)
        with mock.patch('superaimail.engine.core.imap_link.connect', side_effect=AssertionError('local sent copy must not contact IMAP')) as connect:
            opened = self.worker.message_view(sent[0]['id'])
        self.assertEqual(opened['to_addrs'], 'recipient@example.org')
        self.assertEqual(opened['text'], 'body')
        self.assertFalse(opened['has_html'])
        connect.assert_not_called()

    def test_sender_archive_preserves_other_groups_folders_and_snoozed(self):
        self.ingest(fake_message(1, 'target'), fake_message(2, 'other group'), fake_message(3, 'reminder'))
        self.ingest(fake_message(4, 'junk'), folder='Junk')
        target = next((m for m in self.worker.store.messages() if m['subject'] == 'target'))
        self.worker.store.update_message(target['id'], group_name='target')
        reminder = next((m for m in self.worker.store.messages() if m['subject'] == 'reminder'))
        self.worker.store.update_message(reminder['id'], group_name='target', snoozed_until=int(time.time()) + 3600)
        self.assertEqual(self.worker.store.senders('target')[0]['count'], 1)
        self.assertEqual(self.worker.store.archive_sender(self.account['id'], target['from_addr'], 'target'), 1)
        self.assertEqual(len(self.worker.store.messages(view='archive')), 1)
        self.assertEqual(len(self.worker.store.messages(folder='Junk')), 1)
        self.assertEqual(len(self.worker.store.messages(view='snoozed')), 1)

    def test_sent_copy_failure_does_not_report_smtp_failure_or_retry(self):
        from superaimail.engine import smtp_send
        account = dict(self.account, smtp_host='smtp.example.org', smtp_port=465)
        outbox = smtp_send.Outbox(self.worker)
        preview = outbox.preview(account, [{'to': ['recipient@example.org'], 'subject': 'test', 'body': 'body'}])
        with mock.patch.object(self.worker.accounts, 'account', return_value=account), mock.patch.object(smtp_send.net, 'SMTPS') as smtp, mock.patch.object(self.worker, 'record_sent', side_effect=OSError('disk full')):
            sent = outbox.send(preview['token'])
            self.assertEqual(sent['sent'], ['recipient@example.org'])
            self.assertEqual(sent['failed'], [])
            self.assertEqual(sent['copy_errors'], ['recipient@example.org'])
            with self.assertRaises(smtp_send.SendError):
                outbox.send(preview['token'])
            smtp.return_value.send_message.assert_called_once()

class MigrationTests(unittest.TestCase):

    def test_existing_database_keeps_rows_and_migrates_once(self):
        import tempfile
        from pathlib import Path
        from superaimail.engine.store import SCHEMA
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'old.db')
            db = sqlite3.connect(path)
            db.executescript(SCHEMA)
            db.execute("INSERT INTO messages(id,account_id,folder,validity,uid,received,subject) VALUES('one','a','INBOX','v',1,123,'kept')")
            db.commit()
            db.close()
            for _ in range(2):
                store = Store(path)
                self.assertEqual(store.message('one')['subject'], 'kept')
                self.assertEqual(store.message('one')['archived'], 0)
                store.db.close()
if __name__ == '__main__':
    unittest.main()
