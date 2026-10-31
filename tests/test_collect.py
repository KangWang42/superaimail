import os
import sys
import unittest
from superaimail.engine import collect
START, DEADLINE = (1792800000, 1793500000)

def config(**changes):
    value = collect.build_config({'name': '2026标书评审-评分表', 'account_id': 'a1', 'keywords': '评分表', 'start': START, 'deadline': DEADLINE}, None, {'a1'})
    value.update(changes)
    return value

def message(subject, sender='', address='x@y.cn', files=('评分表.xlsx',), received=START + 100, **extra):
    return dict({'account_id': 'a1', 'subject': subject, 'from_name': sender, 'from_addr': address, 'received': received, 'attachments': [{'name': n, 'part': str(i + 2), 'inline': False} for i, n in enumerate(files)], 'link_attach': False}, **extra)

class Identify(unittest.TestCase):

    def test_subject_forms(self):
        cfg = config()
        for subject in ('张三+评分表', '张三＋评分表', '回复：张三评分表', 'Re: 评分表-张三', '【张三】评分表', '张三老师 评分表'):
            self.assertEqual(collect.identify(cfg, message(subject))[0], '张三', subject)

    def test_unknown_waits(self):
        person, reason, _ = collect.identify(config(), message('评分表', sender='Reviewer'))
        self.assertEqual(person, '')
        self.assertEqual(reason, '认不出提交人')

    def test_sender_name_is_used_and_flagged(self):
        person, _, flags = collect.identify(config(), message('评分表', sender='李四'))
        self.assertEqual(person, '李四')
        self.assertIn('按发件人名称识别', flags)

    def test_roster_beats_parsing_and_catches_ambiguity(self):
        cfg = config(roster=collect.parse_roster('张三\n李四 lisi@x.cn\n王五'))
        self.assertEqual(collect.identify(cfg, message('评分表', files=('王五-评分表.xlsx',)))[0], '王五')
        self.assertEqual(collect.identify(cfg, message('评分表', address='lisi@x.cn'))[0], '李四')
        person, reason, _ = collect.identify(cfg, message('张三 李四 评分表'))
        self.assertEqual(person, '')
        self.assertIn('多个', reason)
        person, reason, _ = collect.identify(cfg, message('赵六+评分表'))
        self.assertEqual((person, reason), ('', '名单外：赵六'))

    def test_remembered_sender_is_only_a_fallback(self):
        cfg = config(senders={'x@y.cn': '张三'})
        self.assertEqual(collect.identify(cfg, message('材料'))[0], '张三')
        self.assertEqual(collect.identify(cfg, message('李四+评分表'))[0], '李四')

    def test_forwarded_numbered_filename_beats_old_sender_mapping_without_task_keyword(self):
        cfg = config(senders={'x@y.cn': '刘小雨'})
        person, reason, flags = collect.identify(cfg, message('昕昕点灯 和 wang 的聊天记录', files=('5-5 周小云 12份.xlsx',)))
        self.assertEqual((person, reason), ('周小云', ''))
        self.assertIn('按附件名识别', flags)
        multiple = message('聊天记录', files=('5-5 周小云 12份.xlsx', '8-3 孙小晴 6份.xlsx'))
        self.assertIn('多个', collect.identify(cfg, multiple)[1])
        self.assertEqual(collect.identify(cfg, multiple)[0], '')

    def test_explicit_name_beats_roster_email_and_unknown_filename_does_not_become_a_person(self):
        cfg = config(roster=collect.parse_roster('张三 x@y.cn\n李四'))
        self.assertEqual(collect.identify(cfg, message('李四+评分表'))[0], '李四')
        self.assertEqual(collect.identify(config(), message('聊天记录', files=('第二组汇总.xlsx',)))[0], '')

    def test_words_that_are_not_names_wait_without_a_roster(self):
        for subject in ('面板测试评分表', '第二组评分表', '最终评分表'):
            person, reason, _ = collect.identify(config(), message(subject, address='q@x.cn'))
            self.assertEqual(person, '', subject)
            self.assertTrue(reason.startswith('看不出是姓名') or reason == '认不出提交人', reason)
        self.assertEqual(collect.identify(config(), message('欧阳娜娜+评分表'))[0], '欧阳娜娜')

    def test_name_from_attachment(self):
        person, _, flags = collect.identify(config(), message('材料', files=('王五_评分表.xlsx',)))
        self.assertEqual(person, '王五')
        self.assertIn('按附件名识别', flags)

    def test_sender_name_with_roster(self):
        cfg = config(roster=collect.parse_roster(['张三', '李四']))
        self.assertEqual(collect.identify(cfg, message('评分表', sender='李四'))[0], '李四')
        self.assertEqual(collect.identify(cfg, message('评分表', sender='赵六'))[1], '名单外：赵六')
        self.assertEqual(collect.identify(cfg, message('评分表', sender='Reviewer'))[1], '认不出提交人')

class Matching(unittest.TestCase):

    def test_keyword_in_subject_or_file_and_time(self):
        cfg = config()
        self.assertTrue(collect.matches(cfg, message('张三+评分表', files=())))
        self.assertTrue(collect.matches(cfg, message('材料', files=('张三评分表.xlsx',))))
        self.assertFalse(collect.matches(cfg, message('会议通知', files=('议程.docx',))))
        self.assertFalse(collect.matches(cfg, message('张三+评分表', received=START - 1)))

    def test_flags(self):
        cfg = config()
        late = message('张三评分表', received=DEADLINE + 1)
        flags = collect.submission_flags(cfg, late, '张三', [{'name': 'a.pdf'}])
        self.assertIn(collect.LATE, flags)
        self.assertIn('主题不规范', flags)
        self.assertIn('附件格式：pdf', flags)
        self.assertEqual(collect.submission_flags(cfg, message('张三+评分表'), '张三', [{'name': 'a.xlsx'}]), [])
        self.assertIn('没有附件', collect.submission_flags(cfg, message('张三+评分表', files=()), '张三', []))

class Layout(unittest.TestCase):

    def test_latest_history_pending_and_collisions(self):
        task = {'id': 't1', 'config': config()}
        subs = [{'id': 's1', 'person': '张三', 'pending': 0, 'ignored': 0, 'received': START + 100, 'from_name': '', 'from_addr': 'z@x.cn'}, {'id': 's2', 'person': '张三', 'pending': 0, 'ignored': 0, 'received': START + 9000, 'from_name': '', 'from_addr': 'z@x.cn'}, {'id': 's3', 'person': '', 'pending': 1, 'ignored': 0, 'received': START + 200, 'from_name': 'Reviewer', 'from_addr': 'r@x.cn'}, {'id': 's4', 'person': '李四', 'pending': 0, 'ignored': 1, 'received': START + 300, 'from_name': '', 'from_addr': 'l@x.cn'}]
        files = [{'id': 'f1', 'submission_id': 's1', 'name': '评分表.xlsx', 'sha256': 'a', 'size': 1, 'path': '/p'}, {'id': 'f2', 'submission_id': 's2', 'name': '评分表.xlsx', 'sha256': 'b', 'size': 1, 'path': '/p'}, {'id': 'f3', 'submission_id': 's2', 'name': '评分表.xlsx', 'sha256': 'c', 'size': 1, 'path': '/p'}, {'id': 'f4', 'submission_id': 's3', 'name': 'a/../b.xlsx', 'sha256': 'd', 'size': 1, 'path': '/p'}, {'id': 'f5', 'submission_id': 's4', 'name': 'x.xlsx', 'sha256': 'e', 'size': 1, 'path': '/p'}]
        paths = {e['file_id']: e['path'] for e in collect.layout(task, 'me@mail.example.org', subs, files)}
        base = 'me@mail.example.org/2026标书评审-评分表/'
        self.assertEqual(paths['f2'], base + '01 已提交/张三/张三_评分表.xlsx')
        self.assertEqual(paths['f3'], base + '01 已提交/张三/张三_评分表 (2).xlsx')
        self.assertTrue(paths['f1'].startswith(base + '01 已提交/张三/历史版本/') and paths['f1'].endswith('_张三_评分表.xlsx'))
        self.assertTrue(paths['f4'].startswith(base + '02 待确认/') and '/..' not in paths['f4'] and paths['f4'].endswith('/a_.._b.xlsx'))
        self.assertNotIn('f5', paths)

    def test_rename_modes(self):
        sub = {'id': 's1', 'person': '张三', 'pending': 0, 'received': START}
        item = {'name': '新建 Microsoft Excel 工作表.xlsx', 'display_name': '张三_评分表_第2组.xlsx'}
        self.assertEqual(collect.archive_name(config(rename='off'), sub, item), item['name'])
        self.assertEqual(collect.archive_name(config(rename='ai'), sub, item), '张三_评分表_第2组.xlsx')
        self.assertEqual(collect.archive_name(config(name_template='{任务}-{姓名}-{原文件名}'), sub, {'name': 'a.xls'}), '2026标书评审-评分表-张三-a.xls')
        self.assertEqual(collect.archive_name(config(), dict(sub, pending=1), item), item['name'])
        with self.assertRaises(collect.TaskError):
            collect.build_config({'name': 'x', 'account_id': 'a1', 'keywords': 'k', 'start': START, 'name_template': '{学号}'}, None, {'a1'})

class Status(unittest.TestCase):

    def test_overview_and_summary(self):
        cfg = config(roster=collect.parse_roster('张三\n李四'))
        task = {'id': 't1', 'enabled': 1, 'config': cfg}
        subs = [{'id': 's1', 'message_id': 'm1', 'person': '张三', 'pending': 0, 'ignored': 0, 'manual': 0, 'reason': '', 'flags': '[]', 'received': START + 5, 'subject': '张三+评分表', 'from_name': '', 'from_addr': 'z@x.cn'}, {'id': 's2', 'message_id': 'm2', 'person': '', 'pending': 1, 'ignored': 0, 'manual': 0, 'reason': '认不出提交人', 'flags': '[]', 'received': START + 6, 'subject': '评分表', 'from_name': '', 'from_addr': 'q@x.cn'}]
        view = collect.overview(task, subs, [], now=START)
        self.assertEqual(view['counts']['submitted'], 1)
        self.assertEqual(view['counts']['missing'], 1)
        self.assertEqual(view['counts']['pending'], 1)
        text = collect.summary_text(view, False)
        self.assertIn('已收到 1 人（名单 2 人）', text)
        self.assertIn('未交：李四', text)

    def test_reminders_once_each(self):
        task = {'enabled': True, 'config': config(remind_hours=48), 'state': {}}
        self.assertEqual(collect.due_reminders(task, DEADLINE - 49 * 3600), [])
        self.assertEqual(collect.due_reminders(task, DEADLINE - 47 * 3600), ['before'])
        task['state'] = {'before': 1}
        self.assertEqual(collect.due_reminders(task, DEADLINE + 60), ['final'])
        task['state']['final'] = 1
        self.assertEqual(collect.due_reminders(task, DEADLINE + 120), [])

    def test_config_validation(self):
        with self.assertRaises(collect.TaskError):
            collect.build_config({'name': 'x', 'account_id': 'a1', 'keywords': '', 'start': START}, None, {'a1'})
        with self.assertRaises(collect.TaskError):
            collect.build_config({'name': 'x', 'account_id': 'a1', 'keywords': 'k', 'start': START, 'deadline': START - 1}, None, {'a1'})
        roster = collect.parse_roster('张三 <zs@x.cn>\n张三\n李四,ls@x.cn')
        self.assertEqual(roster, [{'name': '张三', 'email': 'zs@x.cn'}, {'name': '李四', 'email': 'ls@x.cn'}])
if __name__ == '__main__':
    unittest.main()
