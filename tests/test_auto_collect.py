import json
from test_core import FakeWorkerTest, fake_message

class AutoCollectTests(FakeWorkerTest):

    def test_forwarded_reviews_keep_each_teachers_current_file_and_do_not_learn_forwarder(self):
        task = self.make_task(roster='')
        cfg = self.worker.task(task['id'])['config']
        cfg['senders'] = {'forwarder@example.test': '刘小雨'}
        self.worker.store.execute('UPDATE tasks SET config=? WHERE id=?', (json.dumps(cfg), task['id']))
        for uid, person, filename in ((85, '刘小雨', '1-3 刘小雨 14份-评审(1029).xlsx'), (86, '周小云', '5-5 周小云 12份.xlsx')):
            self.ingest(fake_message(uid, '甲 和 乙 的聊天记录', address='forwarder@example.test', files=(filename,)))
            mid = next((m['id'] for m in self.worker.store.messages() if filename in m['attachments'][0]['name']))
            self.worker.collect_suggested(object(), self.account, task['id'], [(mid, (person, '评分及评阅意见', True))])
        subs = self.worker._submissions(task['id'])
        self.assertEqual(sorted(((s['person'], s['pending']) for s in subs)), [('刘小雨', 0), ('周小云', 0)])
        latest = self.worker.sync_manifest()['files']
        self.assertEqual(len(latest), 2)
        self.assertTrue(any(('/01 已提交/刘小雨/' in f['path'] for f in latest)))
        self.assertTrue(any(('/01 已提交/周小云/' in f['path'] for f in latest)))
        self.assertFalse(any(('/历史版本/' in f['path'] for f in latest)))
        zhao = next((s for s in subs if s['person'] == '周小云'))
        self.worker.assign(zhao['id'], '周小云')
        self.assertEqual(self.worker.task(task['id'])['config']['senders'], {})
        self.assertEqual(sorted((s['person'] for s in self.worker._submissions(task['id']))), ['刘小雨', '周小云'])

    def test_forwarded_material_without_signature_cannot_inherit_another_person(self):
        from superaimail.engine import collect
        cfg = self.make_task(roster='')['config']
        cfg['senders'] = {'forwarder@example.test': '刘小雨'}
        m = fake_message(87, 'Fw: 材料', address='forwarder@example.test', files=('sheet.xlsx',))
        self.assertEqual(collect.identify(cfg, m)[:2], ('', '转发材料未明确提交人'))

    def test_ambiguous_forward_does_not_replace_an_existing_teachers_current_version(self):
        task = self.make_task(roster='')
        cfg = self.worker.task(task['id'])['config']
        cfg['senders'] = {'forwarder@example.test': '刘小雨'}
        self.worker.store.execute('UPDATE tasks SET config=? WHERE id=?', (json.dumps(cfg), task['id']))
        self.ingest(fake_message(89, '刘小雨+评分表', address='forwarder@example.test', files=('刘小雨_评分表.xlsx',)))
        self.ingest(fake_message(90, 'Fw: 材料', address='forwarder@example.test', files=('sheet.xlsx',)))
        mid = next((m['id'] for m in self.worker.store.messages() if m['subject'] == 'Fw: 材料'))
        self.worker.collect_suggested(object(), self.account, task['id'], [(mid, ('刘小雨', '旧邮箱对应', True))])
        view = self.worker.task_view(task['id'])
        self.assertEqual((view['counts']['submitted'], view['counts']['duplicates'], view['counts']['pending']), (1, 0, 1))
        entries = self.worker.sync_manifest()['files']
        self.assertEqual(sum(('/01 已提交/刘小雨/' in f['path'] for f in entries)), 1)
        self.assertFalse(any(('/历史版本/' in f['path'] for f in entries)))

    def test_editing_a_task_never_sends_filed_mail_back_to_pending(self):
        task = self.make_task(roster='')
        self.ingest(fake_message(91, '照片', sender='钱小林', files=('林小夏.jpg', '高小宁.jpg', '钱小林.jpg')))
        mid = self.worker.store.messages()[0]['id']
        self.worker.store.execute("INSERT INTO submissions (id, task_id, message_id, person, pending, ignored, manual, reason, flags, received, subject, from_name, from_addr, suggest) VALUES ('s-ai', ?, ?, '钱小林', 0, 0, 0, 'AI 自动收录', '[]', 1, '照片', '钱小林', 'luo@example.test', '钱小林')", (task['id'], mid))
        self.worker.save_task({'id': task['id'], 'rename': 'ai'})
        sub = next((s for s in self.worker._submissions(task['id']) if s['id'] == 's-ai'))
        self.assertEqual((sub['person'], sub['pending']), ('钱小林', 0))

    def test_clear_ai_match_is_collected_once_without_manual_confirmation(self):
        task = self.make_task(roster='')
        self.ingest(fake_message(80, '8-3 孙小晴 6份', sender='孙小晴', files=('sheet.xlsx',)))
        message_id = self.worker.store.messages()[0]['id']
        picked = [(message_id, ('孙小晴', '附件含熟悉度、评阅分数及意见', True))]
        self.worker.collect_suggested(object(), self.account, task['id'], picked)
        self.worker.collect_suggested(object(), self.account, task['id'], picked)
        subs = self.worker._submissions(task['id'])
        self.assertEqual(len(subs), 1)
        self.assertEqual((subs[0]['person'], subs[0]['pending'], subs[0]['manual']), ('孙小晴', 0, 0))
        self.assertIn('AI 自动收录', subs[0]['reason'])
        self.assertIn('/01 已提交/孙小晴/', self.worker.sync_manifest()['files'][0]['path'])

    def test_conflicting_person_still_waits(self):
        task = self.make_task(roster='')
        self.ingest(fake_message(81, '材料', sender='孙小晴', files=('sheet.xlsx',)))
        mid = self.worker.store.messages()[0]['id']
        self.worker.collect_suggested(object(), self.account, task['id'], [(mid, ('张三', '评分材料', True))])
        sub = self.worker._submissions(task['id'])[0]
        self.assertEqual(sub['pending'], 1)
        self.assertIn('不一致', sub['reason'])

    def _competing(self, both_certain):
        task = self.make_task(roster='')
        other = self.make_task(name='学生照片', keywords='照片', roster='')
        self.ingest(fake_message(82, '8-3 孙小晴 6份', sender='孙小晴', files=('sheet.xlsx',)))
        link = type('Link', (), {'queue_collect': lambda obj, tid, items: obj.queued.append((tid, items))})()
        link.queued = []
        self.worker.links[self.account['id']] = link
        calls = []
        self.worker._collection_preview = lambda m: calls.append(m['id']) or '熟悉度 评阅意见 评分'

        def decide(config, messages):
            return {messages[0]['id']: ('孙小晴', '内容匹配', config['name'] != '学生照片' or both_certain)}
        self.worker.ai.task_candidates = decide
        self.worker._night_candidates({'suggested': 0})
        self.assertEqual(len(calls), 1)
        return (link.queued, task, other)

    def test_unique_clear_project_suppresses_weaker_photo_candidate(self):
        queued, task, _ = self._competing(False)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], task['id'])

    def test_two_clear_project_claims_are_not_silently_assigned(self):
        queued, _, _ = self._competing(True)
        self.assertEqual(len(queued), 2)
        self.assertTrue(all((items[0][1][2] is False for _, items in queued)))

    def test_later_ai_failure_does_not_lose_earlier_unqueued_decision(self):
        self.make_task(roster='')
        self.make_task(name='学生照片', keywords='照片', roster='')
        self.ingest(fake_message(84, '材料', sender='孙小晴', files=('sheet.xlsx',)))
        self.worker.links[self.account['id']] = type('Link', (), {'queue_collect': lambda *args: None})()
        self.worker._collection_preview = lambda m: '评分'
        calls = []

        def decide(config, messages):
            calls.append(config['name'])
            if len(calls) == 2:
                raise RuntimeError('temporarily unavailable')
            return {messages[0]['id']: ('孙小晴', '评分内容', True)}
        self.worker.ai.task_candidates = decide
        with self.assertRaises(RuntimeError):
            self.worker._night_candidates({'suggested': 0})
        self.assertTrue(all(('ai_checked_until' not in t['state'] for t in self.worker.tasks())))

    def test_ai_prompt_includes_actual_preview_and_other_task(self):
        task = self.make_task(roster='')
        self.make_task(name='学生照片', keywords='照片', roster='')
        self.ingest(fake_message(83, '材料', sender='孙小晴', files=('sheet.xlsx',)))
        message = self.worker.store.message(self.worker.store.messages()[0]['id'])
        message['collection_preview'] = '评阅意见和熟悉度'
        seen = []

        def chat(system, user, max_tokens):
            seen.extend((system, user))
            return json.dumps({'items': [{'n': 0, 'belongs': True}, {'n': 1, 'belongs': True, 'certain': True, 'person': '孙小晴', 'reason': '评分内容'}]})
        self.worker.ai.chat = chat
        result = self.worker.ai.task_candidates(task['config'], [message])
        self.assertIn('学生照片', seen[0])
        self.assertIn('评阅意见和熟悉度', seen[1])
        self.assertEqual(result[message['id']], ('孙小晴', '评分内容', True))

    def test_ai_prompt_identifies_named_attachment_despite_old_forwarder_mapping(self):
        task = self.make_task(roster='')
        task['config']['senders'] = {'z@x.cn': '刘小雨'}
        m = fake_message(88, '甲 和 乙 的聊天记录', files=('5-5 周小云 12份.xlsx',))
        seen = []
        self.worker.ai.chat = lambda system, user, max_tokens: seen.extend((system, user)) or '{"items":[]}'
        self.worker.ai.task_candidates(task['config'], [m])
        self.assertIn('署名识别：["周小云", ""]', seen[1])
        self.assertIn('主题或附件明确署名优先于邮箱对应', seen[0])

class BatchReplyTests(FakeWorkerTest):

    def test_receipts_to_chosen_submitters_reply_in_their_own_threads(self):
        from superaimail import api as server
        task = self.make_task(roster='')
        self.worker.accounts.save_account(dict(self.account, smtp_host='smtp.example.test', smtp_port=465, secret='x'))
        for uid, person in ((91, '张三'), (92, '李四')):
            self.ingest(fake_message(uid, '%s 评分表' % person, address='%d@example.test' % uid, files=('%s 评分表.xlsx' % person,)))
            mid = next((m['id'] for m in self.worker.store.messages() if person in m['subject']))
            self.worker.collect_suggested(object(), self.account, task['id'], [(mid, (person, '评分表', True))])
        subs = {s['person']: s['id'] for s in self.worker._submissions(task['id'])}
        preview = server.send_preview(self.worker, {'kind': 'receipt', 'task_id': task['id'], 'submission_ids': [subs['张三']], 'subject': 'Re: {主题}', 'body': '{姓名}老师您好：\n\n收到，谢谢！'})
        self.assertEqual(len(preview['messages']), 1)
        sent = preview['messages'][0]
        self.assertEqual((sent['to'], sent['subject'], sent['in_reply_to']), (['91@example.test'], 'Re: 张三 评分表', '<91@x>'))
        self.assertIn('张三老师您好', sent['body'])

    def test_replies_to_mails_chosen_in_the_list(self):
        from superaimail import api as server
        from superaimail.engine import core
        task = self.make_task(roster='')
        self.worker.accounts.save_account(dict(self.account, smtp_host='smtp.example.test', smtp_port=465, secret='x'))
        self.ingest(fake_message(93, '张三 评分表', sender='Zhang', address='93@example.test', files=('张三 评分表.xlsx',)), fake_message(94, '会议安排', sender='王老师', address='94@example.test'))
        ids = {m['subject']: m['id'] for m in self.worker.store.messages()}
        self.worker.collect_suggested(object(), self.account, task['id'], [(ids['张三 评分表'], ('张三', '评分表', True))])
        preview = server.send_preview(self.worker, {'kind': 'replies', 'ids': [ids['张三 评分表'], ids['会议安排']], 'body': '{姓名}您好：收到'})
        sent = {m['to'][0]: m for m in preview['messages']}
        self.assertEqual(sent['93@example.test']['body'], '张三您好：收到')
        self.assertEqual(sent['94@example.test']['body'], '王老师您好：收到')
        self.assertEqual((sent['94@example.test']['subject'], sent['94@example.test']['in_reply_to']), ('Re: 会议安排', '<94@x>'))
        other, _ = self.worker.accounts.save_account({'address': 'other@example.test', 'imap_host': 'imap.example.test', 'secret': 'x', 'enabled': False})
        self.worker.store.execute('UPDATE messages SET account_id=? WHERE id=?', (other['id'], ids['会议安排']))
        with self.assertRaises(core.WorkerError):
            server.send_preview(self.worker, {'kind': 'replies', 'ids': list(ids.values()), 'body': '收到'})
