import unittest
from xml.etree import ElementTree as ET
from workbook_manual import q, remove_legacy_account_mapping, value


class LegacyMappingTests(unittest.TestCase):
    def fixture(self):
        sheet = ET.Element(q('worksheet'))
        data = ET.SubElement(sheet, q('sheetData'))
        for n in range(507, 510):
            row = ET.SubElement(data, q('row'), r=str(n))
            date = ET.SubElement(row, q('c'), r=f'A{n}')
            ET.SubElement(date, q('f')).text = f'A{n-1}+1'
            ET.SubElement(date, q('v')).text = str(46000+n)
            price = ET.SubElement(row, q('c'), r=f'U{n}')
            ET.SubElement(price, q('v')).text = '1830'
            note = ET.SubElement(row, q('c'), r=f'T{n}', t='inlineStr', s='3')
            ET.SubElement(ET.SubElement(note, q('is')), q('t')).text = f'品类{n-506}：旧别名'
            if n == 507:
                header = ET.SubElement(row, q('c'), r='S507', t='inlineStr', s='8')
                ET.SubElement(ET.SubElement(header, q('is')), q('t')).text = '酒价参考'
        config = {'accounts': ['酒价参考'], 'products': {'品类1': {}, '品类2': {}, '品类3': {}}}
        return sheet, config

    def test_removes_notes_and_header_but_preserves_dates_prices_and_caches(self):
        sheet, config = self.fixture()
        before = {c.get('r'): ET.tostring(c) for c in sheet.findall('.//'+q('c'))}
        removed = remove_legacy_account_mapping(sheet, [], config)
        self.assertEqual(set(removed), {'S507', 'T507', 'T508', 'T509'})
        for cell in sheet.findall('.//'+q('c')):
            self.assertEqual(ET.tostring(cell), before[cell.get('r')])
        self.assertEqual(remove_legacy_account_mapping(sheet, [], config), [])

    def test_account_header_and_unrelated_note_are_not_sufficient(self):
        sheet, config = self.fixture()
        config['products'] = {'品类1': {}}
        before = ET.tostring(sheet)
        self.assertEqual(remove_legacy_account_mapping(sheet, [], config), [])
        self.assertEqual(ET.tostring(sheet), before)

    def test_numeric_quote_in_mapping_column_is_preserved(self):
        sheet, config = self.fixture()
        row = ET.SubElement(sheet.find(q('sheetData')), q('row'), r='510')
        cell = ET.SubElement(row, q('c'), r='T510')
        ET.SubElement(cell, q('v')).text = '1780'
        remove_legacy_account_mapping(sheet, [], config)
        self.assertEqual(value(cell, []), '1780')


if __name__ == '__main__':
    unittest.main()
