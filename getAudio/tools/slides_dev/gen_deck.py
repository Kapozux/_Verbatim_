"""调幻灯片排版用：走 slides.py 的真流程出一份大纲 + .pptx，写到 OUT 目录（不进项目工作台）。
用法（在 getAudio 目录、用 venv 的 python）：
  python tools/slides_dev/gen_deck.py <chain_id> <detailed|presenter> <页数> <名字>   # 调一次模型（几美分）
  python tools/slides_dev/gen_deck.py --render <名字>                                # 只按存下的大纲重排，不花钱
输出：$OUT/<名字>.json（大纲）、$OUT/<名字>.pptx；OUT 默认 /tmp/slides_dev。看效果配合 render_sheet.sh。"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO); os.chdir(REPO)
import app, citations, slides, study  # noqa: E402,F401
OUT = os.environ.get('OUT', '/tmp/slides_dev'); os.makedirs(OUT, exist_ok=True)
if sys.argv[1] == '--render':
    d = json.load(open(f'{OUT}/{sys.argv[2]}.json', encoding='utf-8'))
    print(slides.render(d['slides'], f'{OUT}/{sys.argv[2]}.pptx', d['used'], d['slides'][0]['title'], True, d['eyebrow']))
    sys.exit()
cid, style, n, name = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
corpus, pool, cards, thinned, lng = slides._setup(app._chain_dir(cid), None, '', None)
cites = citations.Citations().add(corpus, cards)
raw = slides._llm_obj(study._prompt(corpus, cards, lng, '', '') + slides.SLIDES.format(n=n, style=style, icons=', '.join(slides.ICONS)), 'slides')
ss = slides.check(raw.get('slides'), corpus, cites, True)
eyebrow = f"{corpus.get('author')} · {len({c['ep'] for c in pool})} 个来源"
json.dump({'slides': ss, 'used': cites.used, 'eyebrow': eyebrow}, open(f'{OUT}/{name}.json', 'w', encoding='utf-8'), ensure_ascii=False)
slides.render(ss, f'{OUT}/{name}.pptx', cites.used, ss[0]['title'], True, eyebrow)
print([(s['layout'], s['title']) for s in ss])
