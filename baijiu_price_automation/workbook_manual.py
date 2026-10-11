"""Move the mapping into the manual without reserializing historical workbook cells.

The styled panel is authored with Artifact Tool. Native XLSX patching preserves all
unaffected package members, including historical formula caches and Excel objects.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import posixpath
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS = {'m': MAIN}
TITLE = '各渠道酒价名称对照汇总表'
OWNED = 'BaijiuMappingManual'
ET.register_namespace('', MAIN)
ET.register_namespace('r', REL)

def q(name): return '{'+MAIN+'}'+name

def sheet_paths(z):
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    targets = {r.get('Id'): posixpath.normpath('xl/'+r.get('Target')) if not r.get('Target').startswith('/') else r.get('Target').lstrip('/') for r in rels}
    w = ET.fromstring(z.read('xl/workbook.xml'))
    return {s.get('name'): targets[s.get('{'+REL+'}id')] for s in w.find(q('sheets'))}

def strings(z):
    if 'xl/sharedStrings.xml' not in z.namelist(): return []
    return [''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml'))]

def value(cell, shared):
    if cell.get('t') == 'inlineStr': return ''.join(cell.find(q('is')).itertext())
    v = cell.find(q('v'))
    if v is None: return ''
    return shared[int(v.text)] if cell.get('t') == 's' else v.text or ''

def put_text(cell, text):
    for node in list(cell): cell.remove(node)
    cell.set('t','inlineStr')
    node=ET.SubElement(ET.SubElement(cell,q('is')),q('t'));node.text=str(text)
    node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')

def shift_ref(ref, offset):
    return re.sub(r'(\$?[A-Z]+\$?)(\d+)', lambda m:m[1]+str(int(m[2])+offset), ref)

def merge_styles(dst, src):
    maps={}
    for section in ['fonts','fills','borders','cellStyleXfs','cellXfs']:
        target=dst.find(q(section)); origin=src.find(q(section)); lookup={}
        for i,item in enumerate(origin):
            node=deepcopy(item)
            if section in {'cellStyleXfs','cellXfs'}:
                for attr,group in [('fontId','fonts'),('fillId','fills'),('borderId','borders'),('xfId','cellStyleXfs')]:
                    if attr in node.attrib and group in maps: node.set(attr,str(maps[group][int(node.get(attr))]))
                if int(node.get('numFmtId','0')) >= 164: raise ValueError('对照表面板不应包含自定义数字格式')
            signature=ET.tostring(node)
            index=next((j for j,old in enumerate(target) if ET.tostring(old)==signature),None)
            if index is None: index=len(target);target.append(node)
            lookup[i]=index
        target.set('count',str(len(target)));maps[section]=lookup
    return maps['cellXfs']


def remove_legacy_account_mapping(sheet, shared, config):
    """Remove only an account label and its adjacent, identified product notes.

    Some original templates contain a separate 酒价参考 block inside the dated
    price grid. Removing the later summary does not remove that earlier block.
    Never remove entire rows or columns: they also contain dates and quotations.
    """
    data = sheet.find(q('sheetData'))
    cells = {c.get('r'): c for row in data for c in row}
    compact = lambda s: re.sub(r'\s+', '', unicodedata.normalize('NFKC', s))
    products = {compact(name) for name in config['products']}
    removed = set()
    for address, header in cells.items():
        if value(header, shared) not in config['accounts']:
            continue
        column, row = re.fullmatch(r'([A-Z]+)(\d+)', address).groups()
        number = 0
        for letter in column:
            number = number * 26 + ord(letter) - ord('A') + 1
        number += 1
        right = ''
        while number:
            number, digit = divmod(number - 1, 26)
            right = chr(ord('A') + digit) + right
        notes, names = [], set()
        for offset in range(len(products) + 1):
            candidate = cells.get(f'{right}{int(row) + offset}')
            if candidate is None or candidate.find(q('f')) is not None:
                break
            text = value(candidate, shared)
            parts = re.split(r'[:：]', text, maxsplit=1)
            if len(parts) != 2 or compact(parts[0]) not in products or not parts[1].strip():
                break
            names.add(compact(parts[0]))
            notes.append(candidate.get('r'))
        # An account name beside one ordinary note is insufficient evidence.
        if len(names) >= 3:
            removed.update([address, *notes])
    for row in data:
        for cell in list(row):
            if cell.get('r') in removed:
                row.remove(cell)
    return sorted(removed)


def clean_legacy_mappings(source: Path, destination: Path, config: dict):
    """Narrow package patch, retaining all other cells and cached formula values."""
    source, destination = Path(source), Path(destination)
    removed, patched = {}, {}
    with ZipFile(source) as original:
        shared = strings(original)
        for name, member in sheet_paths(original).items():
            if name == '更新手册':
                continue
            sheet = ET.fromstring(original.read(member))
            addresses = remove_legacy_account_mapping(sheet, shared, config)
            if addresses:
                removed[name] = addresses
                patched[member] = ET.tostring(sheet, encoding='utf-8')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.legacy.tmp')
        with ZipFile(temporary, 'w', ZIP_DEFLATED) as result:
            for info in original.infolist():
                result.writestr(info, patched.get(info.filename, original.read(info.filename)))
    temporary.replace(destination)
    return removed

def normalize_manual(source: Path, destination: Path, config: dict):
    source,destination=Path(source),Path(destination)
    asset=Path(__file__).resolve().parent/'config/manual_mapping.xlsx'
    with ZipFile(source) as z, ZipFile(asset) as panel:
        paths=sheet_paths(z)
        if '更新手册' not in paths: raise ValueError('数据库缺少“更新手册”工作表，请使用同格式数据库')
        shared=strings(z);template_shared=strings(panel)
        workbook=ET.fromstring(z.read('xl/workbook.xml'))
        manual=ET.fromstring(z.read(paths['更新手册']))
        data=manual.find(q('sheetData'))
        names=workbook.find(q('definedNames'))
        owned=next((n for n in names if n.get('name')==OWNED),None) if names is not None else None
        existing=next((c for c in data.findall('.//'+q('c')) if value(c,shared)==TITLE),None)
        start=int(re.search(r'\d+',existing.get('r'))[0]) if existing is not None else max([int(r.get('r')) for r in data]+[0])+3
        offset=start-1
        if owned is not None:
            match=re.fullmatch(r"'?更新手册'?!\$?A\$?(\d+):\$?G\$?(\d+)",owned.text or '')
            if not match: raise ValueError('更新手册对照表区域标记异常')
            start=int(match[1]);offset=start-1
        panel_xml=ET.fromstring(panel.read(sheet_paths(panel)['对照表']))
        panel_data=panel_xml.find(q('sheetData'))
        height=max(int(r.get('r')) for r in panel_data)
        # Only the owned panel is replaced. Original manual notes above it remain.
        for row in list(data):
            if start <= int(row.get('r')) < start+height:
                for cell in list(row):
                    col=re.match('[A-Z]+',cell.get('r'))[0]
                    if col in 'ABCDEFG': row.remove(cell)
                if not len(row): data.remove(row)
        merges=manual.find(q('mergeCells'))
        if merges is None:
            merges=ET.Element(q('mergeCells'))
            # mergeCells follows sheetData and optional protection/filter blocks.
            later={q(x) for x in ['phoneticPr','conditionalFormatting','dataValidations','hyperlinks','printOptions','pageMargins','pageSetup','headerFooter','drawing','legacyDrawing','extLst']}
            index=next((i for i,x in enumerate(manual) if x.tag in later),len(manual));manual.insert(index,merges)
        for m in list(merges):
            bounds=[int(n) for n in re.findall(r'\d+',m.get('ref'))]
            if bounds and min(bounds)>=start and max(bounds)<start+height: merges.remove(m)
        styles=ET.fromstring(z.read('xl/styles.xml'));style_map=merge_styles(styles,ET.fromstring(panel.read('xl/styles.xml')))
        panel_rows=deepcopy(list(panel_data))
        entries=list(config['products'].items())
        for row in panel_rows:
            local=int(row.get('r'));row.set('r',str(local+offset))
            for c in row:
                original=c.get('r');c.set('r',shift_ref(original,offset));c.set('s',str(style_map[int(c.get('s','0'))]))
                if c.get('t')=='s': put_text(c,value(c,template_shared))
                if 5 <= local < 5+len(entries):
                    name,spec=entries[local-5];col=ord(re.match('[A-Z]+',original)[0])-ord('A')
                    put_text(c,name if col==0 else '；'.join(spec['aliases'].get(config['accounts'][col-1],[])) or '—')
            found=next((r for r in data if r.get('r')==row.get('r')),None)
            if found is None: data.append(row)
            else:
                found.attrib.update(row.attrib)
                for c in row:found.append(c)
        data[:]=sorted(data,key=lambda r:int(r.get('r')))
        for m in panel_xml.find(q('mergeCells')):
            node=deepcopy(m);node.set('ref',shift_ref(node.get('ref'),offset));merges.append(node)
        merges.set('count',str(len(merges)))
        cols=manual.find(q('cols'))
        if cols is not None: manual.remove(cols)
        cols=deepcopy(panel_xml.find(q('cols')));manual.insert(list(manual).index(data),cols)
        # Existing long instructions get wrapping across the same seven columns.
        for row in data:
            if int(row.get('r'))>=start: continue
            cells=[c for c in row if value(c,shared)]
            if len(cells)==1 and cells[0].get('r').startswith('A') and len(value(cells[0],shared))>25:
                row.set('ht','34');row.set('customHeight','1')
                put_text(cells[0],value(cells[0],shared))
                cells[0].set('s',str(style_map[int(panel_data[1][0].get('s','0'))]))
                ref=f"A{row.get('r')}:G{row.get('r')}"
                if not any(m.get('ref')==ref for m in merges): ET.SubElement(merges,q('mergeCell'),ref=ref)
        merges.set('count',str(len(merges)))
        dim=manual.find(q('dimension'))
        if dim is not None:dim.set('ref',f'A1:G{max(int(r.get("r")) for r in data)}')
        if names is None:
            names=ET.Element(q('definedNames'));calc=workbook.find(q('calcPr'));workbook.insert(list(workbook).index(calc) if calc is not None else len(workbook),names)
        if owned is None:owned=ET.SubElement(names,q('definedName'),name=OWNED)
        owned.text=f"'更新手册'!$A${start}:$G${start+height-1}"
        patched={paths['更新手册']:ET.tostring(manual,encoding='utf-8'),'xl/styles.xml':ET.tostring(styles,encoding='utf-8'),'xl/workbook.xml':ET.tostring(workbook,encoding='utf-8')}
        removed=[]
        if '茅台' in paths:
            brand=ET.fromstring(z.read(paths['茅台']));brand_data=brand.find(q('sheetData'))
            marker=next((c for c in brand_data.findall('.//'+q('c')) if value(c,shared)==TITLE),None)
            if marker is not None:
                footer=int(re.search(r'\d+',marker.get('r'))[0]);col=re.match('[A-Z]+',marker.get('r'))[0]
                if col!='C': raise ValueError('对照表位置与模板不一致，未清除任何数据')
                labels={value(c,shared) for r in brand_data if footer+3<=int(r.get('r'))<=footer+16 for c in r if c.get('r').startswith('C')}
                if labels!=set(config['products']): raise ValueError('原对照表品类不完整，未清除任何数据')
                for r in brand_data:
                    if footer<=int(r.get('r'))<=footer+16:
                        outside=[c for c in r if re.match('[A-Z]+',c.get('r'))[0] not in list('ACDEFGHI') and value(c,shared)]
                        if outside: raise ValueError('原对照表区域旁存在其他数据，未移动对照表')
                        for c in list(r):
                            column=re.match('[A-Z]+',c.get('r'))[0]
                            scaffold=column=='A' and c.find(q('f')) is not None and re.fullmatch(r'A\d+\+1',c.find(q('f')).text or '')
                            if column in list('CDEFGHI') or scaffold: removed.append(c.get('r'));r.remove(c)
                patched[paths['茅台']]=ET.tostring(brand,encoding='utf-8')
        destination.parent.mkdir(parents=True,exist_ok=True)
        temp=destination.with_name(destination.name+'.manual.tmp')
        with ZipFile(temp,'w',ZIP_DEFLATED) as result:
            for info in z.infolist(): result.writestr(info,patched.get(info.filename,z.read(info.filename)))
    temp.replace(destination)
    legacy_removed = clean_legacy_mappings(destination, destination, config)
    removed.extend(legacy_removed.get('茅台', []))
    return {'manual_range':owned.text,'removed_cells':removed,'legacy_removed_cells':legacy_removed,'products':len(config['products']),'channel_pairs':sum(len(p['aliases']) for p in config['products'].values())}
