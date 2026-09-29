"""
导出：把带出处的 Markdown 转成 Word（.docx）。

不引第三方库：.docx 就是一个 zip，里面几份 XML。只支持导出用得到的那几样——
# 标题（1~3 级）、段落、「- 」列表、「> 」引用、**粗体**、[文字](链接)。
链接做成真正能点的超链接（Word 里 Ctrl/⌘ + 点击跳到视频那一秒）。
"""

import io
import re
import zipfile
from xml.sax.saxutils import escape

_INLINE = re.compile(r'(\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))')


def _run(text, bold=False, italic=False, color=None, size=None, link=False):
    props = []
    if link:
        props.append('<w:rStyle w:val="Hyperlink"/>')
    if bold:
        props.append('<w:b/>')
    if italic:
        props.append('<w:i/>')
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/>')
    rpr = f'<w:rPr>{"".join(props)}</w:rPr>' if props else ''
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


class _Doc:
    def __init__(self):
        self.body = []
        self.links = []          # (rId, url)

    def _inline(self, text, **kw):
        out = []
        for part in _INLINE.split(text):
            if not part:
                continue
            if part.startswith('**') and part.endswith('**'):
                out.append(_run(part[2:-2], bold=True, **kw))
                continue
            m = re.fullmatch(r'\[([^\]]+)\]\(([^)\s]+)\)', part)
            if m and re.match(r'https?://', m.group(2)):
                rid = f'rId{100 + len(self.links)}'
                self.links.append((rid, m.group(2)))
                out.append(f'<w:hyperlink r:id="{rid}" w:history="1">'
                           f'{_run(m.group(1), link=True, color="BE5D3E")}</w:hyperlink>')
                continue
            out.append(_run(part, **kw))
        return ''.join(out)

    def para(self, text, style=None, **kw):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
        self.body.append(f'<w:p>{ppr}{self._inline(text, **kw)}</w:p>')

    def build(self, title):
        doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
               f'<w:body>{"".join(self.body)}'
               '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
               '<w:pgMar w:top="1300" w:right="1300" w:bottom="1300" w:left="1300" w:header="708" w:footer="708" w:gutter="0"/>'
               '</w:sectPr></w:body></w:document>')
        rels = ''.join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                       f'Target="{escape(url, {chr(34): "&quot;"})}" TargetMode="External"/>' for rid, url in self.links)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml',
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                       '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                       '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                       '</Types>')
            z.writestr('_rels/.rels',
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                       '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                       '</Relationships>')
            z.writestr('docProps/core.xml',
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                       'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>' + escape(title) +
                       '</dc:title><dc:creator>Verbatim</dc:creator></cp:coreProperties>')
            z.writestr('word/_rels/document.xml.rels',
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                       + rels + '</Relationships>')
            z.writestr('word/styles.xml', _STYLES)
            z.writestr('word/document.xml', doc)
        return buf.getvalue()


_FONT = ('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="PingFang SC" w:cs="Calibri"/>')
_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:docDefaults><w:rPrDefault><w:rPr>{_FONT}<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
           '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
           '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
           '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:spacing w:before="320" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:spacing w:before="280" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:ind w:left="480" w:hanging="240"/></w:pPr></w:style>'
           '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
           '<w:pPr><w:ind w:left="480"/><w:pBdr><w:left w:val="single" w:sz="12" w:space="8" w:color="F0D8CC"/></w:pBdr></w:pPr>'
           '<w:rPr><w:color w:val="555555"/></w:rPr></w:style>'
           '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:rPr><w:u w:val="single"/></w:rPr></w:style>'
           '</w:styles>')


def markdown_to_docx(md, title='Verbatim'):
    d = _Doc()
    for raw in (md or '').replace('\r\n', '\n').split('\n'):
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r'^(#{1,3})\s+(.*)$', line)
        if m:
            level = len(m.group(1))
            d.para(m.group(2), 'Title' if level == 1 else f'Heading{level - 1}')
            continue
        m = re.match(r'^\s*[-*]\s+(.*)$', line)
        if m:
            d.para('• ' + m.group(1), 'ListBullet')
            continue
        m = re.match(r'^\s*(\d+)[.)]\s+(.*)$', line)
        if m:
            d.para(f'{m.group(1)}. {m.group(2)}', 'ListBullet')
            continue
        m = re.match(r'^>\s?(.*)$', line)
        if m:
            d.para(m.group(1), 'Quote')
            continue
        d.para(line)
    return d.build(title)
