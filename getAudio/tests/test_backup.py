"""内置备份要备到花钱、花时间才做出来、丢了补不回的东西。
2026-10-06 查出来：备份只看 results/ 下第一层、只认 .json/.md/.txt/.srt，名字以 _ 开头的目录整个跳过——
所以 results/_docs/ 里用户上传的 PDF / PPTX / DOCX 原件和解析结果一份都没备过（154 MB），
笔记本 studio/ 里生成的播客 mp3、幻灯片 pptx 也没备，声纹和向量（.npz，花钱 / 花时间算的）也没备。
照旧不备：转写的音频（有意的，见 backup.py 开头）、播客原声片段缓存 _clips、幻灯片预览图、代码库快照 _repos。
跑生产代码：backup.run。没有桩：只读写临时目录。"""
import os

from _support import Checks, isolate, write_json

TMP = isolate('backup')
DEST_PARENT = os.path.join(TMP, 'drive')
os.makedirs(DEST_PARENT)
os.environ['BACKUP_DIR'] = os.path.join(DEST_PARENT, 'Verbatim_备份')
import backup  # noqa: E402
import config  # noqa: E402

t = Checks()
R = config.RESULTS_FOLDER
DEST = os.environ['BACKUP_DIR']


def put(rel, data=b'x'):
    p = os.path.join(R, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)


def backed(rel):
    return os.path.isfile(os.path.join(DEST, rel))


TID = '11111111-2222-3333-4444-555555555555'
write_json(os.path.join(R, TID, 'meta.json'), {'id': TID})
write_json(os.path.join(R, TID, 'transcript.json'), [])
put(f'{TID}/voices.npz')
put(f'{TID}/audio.m4a')
put(f'{TID}/voice.m4a')
write_json(os.path.join(R, '_docs', 'doc1', 'meta.json'), {'id': 'doc1'})
put('_docs/doc1/original.pdf', b'%PDF-1.7')
put('_docs/doc1/text.md')
put('_docs/doc2/original.pptx')
write_json(os.path.join(R, '_chains', 'c1', 'chain.json'), {'id': 'c1'})
put('_chains/c1/embeddings.npz')
put('_chains/c1/studio/abc.json')
put('_chains/c1/studio/abc.mp3')
put('_chains/c1/studio/def.pptx')
put('_chains/c1/studio/def_slides/1.png')
put('_clips/x_0_17.m4a')
put('_repos/r1/snapshot/main.py')

backup.run(R)
t.check('转写照旧备文字', backed(f'{TID}/meta.json') and backed(f'{TID}/transcript.json'))
t.check('上传的文档原件（PDF / PPTX）和解析结果都备上', backed('_docs/doc1/original.pdf')
        and backed('_docs/doc1/text.md') and backed('_docs/doc1/meta.json') and backed('_docs/doc2/original.pptx'))
t.check('笔记本 studio 里的播客 mp3、幻灯片 pptx、产出 json 都备上', backed('_chains/c1/studio/abc.mp3')
        and backed('_chains/c1/studio/def.pptx') and backed('_chains/c1/studio/abc.json'))
t.check('花钱 / 花时间算的向量和声纹（.npz）备上', backed('_chains/c1/embeddings.npz') and backed(f'{TID}/voices.npz'))
t.check('照旧不备：转写音频、原声片段缓存、幻灯片预览图、代码库快照',
        not backed(f'{TID}/audio.m4a') and not backed(f'{TID}/voice.m4a') and not backed('_clips/x_0_17.m4a')
        and not backed('_chains/c1/studio/def_slides/1.png') and not backed('_repos/r1/snapshot/main.py'))
st = backup.run(R)
t.check('第二次跑：没变的不再复制', st.get('copied_last_run') == 0, f'{st.get("copied_last_run")}')
os.remove(os.path.join(R, '_docs', 'doc1', 'original.pdf'))
backup.run(R)
t.check('只增不删：应用里删了，备份里还在', backed('_docs/doc1/original.pdf'))
t.finish()
