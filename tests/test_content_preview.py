import io
import unittest
import zipfile
from superaimail.engine import content_preview

class PreviewTests(unittest.TestCase):

    def workbook(self, sheet, shared=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('xl/worksheets/sheet1.xml', sheet)
            if shared:
                z.writestr('xl/sharedStrings.xml', shared)
        return out.getvalue()

    def test_sparse_cells_and_no_formula_execution(self):
        result = content_preview.xlsx_preview(self.workbook('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="C1"><v>42</v></c><c r="E1"><f>HYPERLINK("https://example.org")</f></c></row></sheetData></worksheet>'))
        self.assertEqual(result['sheets'][0]['rows'], [['', '', '42', '', '（公式无缓存值）']])

    def test_entities_rejected(self):
        with self.assertRaises(ValueError):
            content_preview.xlsx_preview(self.workbook('<!DOCTYPE a [<!ENTITY x "bad">]><a/>'))
        with self.assertRaises(ValueError):
            content_preview.xlsx_preview(self.workbook('<!DOCTYPE a [<!ENTITY x "bad">]><a/>'.encode('utf-16')))

    def test_external_links_require_https_no_credentials(self):
        self.assertEqual(content_preview.action_links('login javascript:alert(1)', '<http://example.org>'), [])
        self.assertEqual(content_preview.https_url('https://user:pass@example.org/path'), '')
        self.assertEqual(content_preview.https_url('https://example.org/' + 'a' * 4000), '')
        links = content_preview.action_links('登录 https://example.org/magic?token=123', '<https://example.org/unsubscribe>')
        self.assertEqual([x['kind'] for x in links], ['login', 'unsubscribe'])
if __name__ == '__main__':
    unittest.main()
