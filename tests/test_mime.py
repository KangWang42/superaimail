import base64
import os
import sys
import unittest
from superaimail.engine import mime
SAMPLE = [(b'7 (UID 1757000328 RFC822.SIZE 381234 INTERNALDATE "23-Oct-2026 14:31:43 +0800" BODYSTRUCTURE ((("text" "plain" ("charset" "utf-8") NIL NIL "base64" 1200 16 NIL NIL NIL)("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" 3000 40 NIL NIL NIL) "alternative" ("boundary" "b2") NIL NIL)("application" "vnd.openxmlformats-officedocument.spreadsheetml.sheet" ("name" "=?utf-8?B?5byg5LiJX+ivhOWIhuihqC54bHN4?=") NIL NIL "base64" 4000 NIL ("attachment" ("filename*" "utf-8\'\'%E5%BC%A0%E4%B8%89_%E8%AF%84%E5%88%86%E8%A1%A8.xlsx")) NIL)("image" "png" ("name" "logo.png") "<logo@x>" NIL "base64" 800 NIL ("inline" ("filename" "logo.png")) NIL) "mixed" ("boundary" "b1") NIL NIL) BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {60}', b'Subject: =?utf-8?B?5byg5LiJK+ivhOWIhuihqA==?=\r\nFrom: a@b.cn\r\n\r\n'), b')']

class ParseFetch(unittest.TestCase):

    def test_structure_and_literal(self):
        items = mime.parse_fetch(SAMPLE)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['UID'], '1757000328')
        self.assertEqual(mime.internal_date(item['INTERNALDATE']), 1792737103)
        self.assertIn(b'Subject', bytes(item['BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)]']))
        parts = mime.walk(item['BODYSTRUCTURE'])
        self.assertEqual([p['part'] for p in parts], ['1.1', '1.2', '2', '3'])
        files = mime.attachments(parts)
        self.assertEqual(files[0]['name'], '张三_评分表.xlsx')
        self.assertFalse(files[0]['inline'])
        self.assertEqual(files[1]['name'], 'logo.png')
        self.assertTrue(files[1]['inline'])
        self.assertEqual(mime.body_part(parts)['part'], '1.1')

    def test_single_part_message_is_part_one(self):
        items = mime.parse_fetch([b'1 (UID 5 BODYSTRUCTURE ("text" "plain" ("charset" "gbk") NIL NIL "7bit" 10 1 NIL NIL NIL))'])
        parts = mime.walk(items[0]['BODYSTRUCTURE'])
        self.assertEqual(parts[0]['part'], '1')
        self.assertEqual(parts[0]['charset'], 'gbk')

    def test_continued_rfc2231_name(self):
        items = mime.parse_fetch([b'1 (UID 9 BODYSTRUCTURE ("application" "pdf" NIL NIL NIL "base64" 10 NIL ("attachment" ("filename*0*" "utf-8\'\'%E8%AF%84" "filename*1*" "%E5%88%86.pdf")) NIL))'])
        self.assertEqual(mime.attachments(mime.walk(items[0]['BODYSTRUCTURE']))[0]['name'], '评分.pdf')

class Decoding(unittest.TestCase):

    def test_base64_in_slices_matches_whole(self):
        data = os.urandom(5000)
        encoded = base64.encodebytes(data)
        decoder, out = (mime.Decoder('base64'), b'')
        for start in range(0, len(encoded), 777):
            out += decoder.feed(encoded[start:start + 777])
        out += decoder.finish()
        self.assertEqual(out, data)

    def test_quoted_printable_in_slices(self):
        import quopri
        data = ('评分表 =  line\n' * 300).encode('utf-8')
        encoded = quopri.encodestring(data)
        decoder, out = (mime.Decoder('quoted-printable'), b'')
        for start in range(0, len(encoded), 101):
            out += decoder.feed(encoded[start:start + 101])
        self.assertEqual(out + decoder.finish(), data)

    def test_text_charsets(self):
        self.assertEqual(mime.decode_text('你好'.encode('gb18030'), '8bit', 'gbk'), '你好')
        self.assertEqual(mime.decode_text(base64.b64encode('你好'.encode()), 'base64', ''), '你好')

class Names(unittest.TestCase):

    def test_safe_name(self):
        self.assertEqual(mime.safe_name('a<b>:c"/d\\e|f?g*.xlsx'), 'a_b__c__d_e_f_g_.xlsx')
        self.assertEqual(mime.safe_name('..'), '未命名')
        self.assertEqual(mime.safe_name('CON.txt'), '_CON.txt')
        self.assertEqual(mime.safe_name('a. '), 'a')
        long = mime.safe_name('张' * 300 + '.xlsx', 100)
        self.assertTrue(long.endswith('.xlsx') and len(long) <= 100)

    def test_folder_names_and_codes(self):
        self.assertEqual(mime.verification_code('您的验证码是 482913，5 分钟内有效'), '482913')
        self.assertTrue(mime.has_link_attachment('从网易企业邮箱发来的超大附件'))
        self.assertFalse(mime.has_link_attachment('附件见评分表'))
if __name__ == '__main__':
    unittest.main()
