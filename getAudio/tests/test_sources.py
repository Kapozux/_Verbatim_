"""sources.py：文档存储、转换（墨页 / 本机兜底）、切段。
墨页是真实本机服务（fast 模式只读文字层）——没开就跳过那一项。测试用的 PDF / Word 现生成（macOS 的
cupsfilter / textutil），不碰任何个人文件；这些工具没有也跳过。"""
import os
import shutil
import subprocess
import time

from _support import Checks, isolate

TMP = isolate('sources')
import sources as S  # noqa: E402

t = Checks()
assert S.DOCS_DIR.startswith(TMP)


def wait(doc_id, limit=180):
    end = time.time() + limit
    while time.time() < end:
        m = S.doc_meta(doc_id)
        if m['status'] != 'converting':
            return m
        time.sleep(1)
    return S.doc_meta(doc_id)


def have(tool):
    return shutil.which(tool) is not None


# 1 粘贴文字：按小标题切段
m = S.create_doc(text='# 三、文言文\n\n重点掌握《赤壁赋》的主客问答结构。\n\n## 背诵篇目\n\n《劝学》《师说》《赤壁赋》全文背诵。',
                 title='考试提纲')
ps = S.doc_passages(m['id'])
t.check('粘贴文字按小标题切段', m['status'] == 'ready' and len(ps) == 2
        and ps[0]['heading'] == '三、文言文' and ps[1]['heading'] == '背诵篇目')

PDF = os.path.join(TMP, 'lecture.pdf')
if have('cupsfilter'):
    txt = os.path.join(TMP, 'lecture.txt')
    with open(txt, 'w') as f:
        f.write('\f'.join(f'Lecture {n}\n\nToday we read the Red Cliff ode. The host and the guest argue about change '
                          f'and permanence. Point {n}: the moon and the water.\n' for n in range(1, 11)))
    with open(PDF, 'wb') as out:
        subprocess.run(['cupsfilter', txt], stdout=out, stderr=subprocess.DEVNULL)

pdf_ok = os.path.exists(PDF) and os.path.getsize(PDF) > 0

# 2 PDF 走墨页
if not pdf_ok:
    t.skip('PDF 走墨页', '没有 cupsfilter，生成不了测试 PDF')
elif not S.moye_alive():
    t.skip('PDF 走墨页', '墨页没开')
else:
    m = wait(S.create_doc(filename='lecture.pdf', data=open(PDF, 'rb').read())['id'])
    ps = S.doc_passages(m['id'])
    t.check('PDF 走墨页、段落带页码', m['status'] == 'ready' and m['converter'].startswith('moye') and m['pages']
            and ps and all(p['page'] for p in ps))

# 3 PDF 本机兜底（假装墨页没开）
real = S.moye_alive
S.moye_alive = lambda timeout=2: False
if pdf_ok and have('pdftotext'):
    m = wait(S.create_doc(filename='PHY.pdf', data=open(PDF, 'rb').read())['id'])
    ps = S.doc_passages(m['id'])
    t.check('墨页没开时 PDF 用 pdftotext 兜底', m['status'] == 'ready' and m['converter'] == 'pdftotext'
            and ps and all(p['page'] for p in ps))
else:
    t.skip('墨页没开时 PDF 用 pdftotext 兜底', '没有 cupsfilter / pdftotext')

# 4 Word 本机兜底
if have('textutil'):
    note = os.path.join(TMP, 'note.txt')
    with open(note, 'w') as f:
        f.write('Lecture 3 notes\n\nThe narrator uses a dialogue between host and guest to argue about impermanence.'
                '\n\nKey quote: everything changes.')
    subprocess.run(['textutil', '-convert', 'docx', note, '-output', os.path.join(TMP, 'note.docx')], check=True)
    m = wait(S.create_doc(filename='note.docx', data=open(os.path.join(TMP, 'note.docx'), 'rb').read())['id'])
    t.check('Word 用 textutil 兜底', m['status'] == 'ready' and m['converter'] == 'textutil' and S.doc_passages(m['id']))
else:
    t.skip('Word 用 textutil 兜底', '没有 textutil（不是 macOS）')

# 5 没有文字层的 PDF、墨页没开：报一句说得清的错
blank = os.path.join(TMP, 'blank.pdf')
if have('cupsfilter'):
    with open(blank, 'wb') as out:
        subprocess.run(['cupsfilter', '-m', 'application/pdf', '/dev/null'], stdout=out, stderr=subprocess.DEVNULL)
if os.path.exists(blank) and os.path.getsize(blank) > 100:
    m = wait(S.create_doc(filename='scan.pdf', data=open(blank, 'rb').read())['id'])
    t.check('扫描件 + 墨页没开：错误里点名墨页', m['status'] == 'failed' and 'Moye' in (m.get('error') or ''))
else:
    t.skip('扫描件 + 墨页没开：错误里点名墨页', '生成不了空白 PDF')
S.moye_alive = real

# 6 不支持的 / 空的：拒绝
rejected = 0
for bad in [dict(filename='x.exe', data=b'MZ'), dict(filename='a.pdf', data=b''), dict(text='   ')]:
    try:
        S.create_doc(**bad)
    except ValueError:
        rejected += 1
t.check('不支持的类型、空文件、空文字都拒绝', rejected == 3)

# 7 长段落会被拆开
d = S.create_doc(text='x' * 10 + '。' + '这是一句很长的话，' * 400, title='long')
long_ps = S.doc_passages(d['id'])
t.check('长段落拆成小块', len(long_ps) > 3 and max(S._size(p['text']) for p in long_ps) < S.PASSAGE_CHARS * 1.5)
t.finish()
