"""复习笔记模板：每份来源按用户自己的模板出一份笔记，加来源时自动出（2026-10-06，照 Open Notebook 的 transformation）。

用户唯一被验证过的用途是「课堂录音 + 卷子 → 复习笔记」：每节课的笔记格式要一样，不想每次去工作台再写一遍要求。
一份笔记 = 一份自定义报告（study.start：kind=report、format=custom、prompt=模板、范围只勾这一份来源），
多带一个 revnote 标记记下是哪份来源的——所以不重复出，出处、导出、列表、费用记账都跟报告一样，不另写一套。

配置存在笔记本目录的 revnotes.json：{template, auto}。没保存过 = 没开（不会自动花钱）。
自动出：加来源后的刷新（app._project_refresh）末尾调 auto()；一次最多 AUTO_MAX 份、估价合计不超过 AUTO_BUDGET_USD，
剩下的在设置弹框里写「还有 N 份没出（约 $x）」，用户点了才出。模板改了不回头重出已有的（花钱，要用户点）。"""
import json
import os
import threading

AUTO_MAX = 20
AUTO_BUDGET_USD = 0.5
TEMPLATE_MAX = 2000
OUT_TOKENS = 1500            # 估价用：一份笔记大约这么长
KINDS = ('doc', 'task')      # 代码库不出复习笔记

DEFAULT_TEMPLATE = {
    'zh': ('## 这一份讲了什么\n三到五句话。\n\n'
           '## 考点\n每条一句话，说清楚是什么、为什么重要。\n\n'
           '## 老师强调的原话\n照抄原话。\n\n'
           '## 容易错的地方\n\n'
           '## 可能会考的题\n三道，每道附简短答案。'),
    'en': ('## What this covers\nThree to five sentences.\n\n'
           '## Key points\nOne sentence each: what it is and why it matters.\n\n'
           '## What the teacher stressed\nQuote the exact words.\n\n'
           '## Common mistakes\n\n'
           '## Likely exam questions\nThree, each with a short answer.'),
}

_lock = threading.Lock()


def _path(cdir):
    return os.path.join(cdir, 'revnotes.json')


def config(cdir):
    try:
        with open(_path(cdir), 'r', encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {'template': str(d.get('template') or ''), 'auto': bool(d.get('auto'))}


def save_config(cdir, template, auto):
    template = str(template or '').strip()
    if not template:
        raise ValueError('Write the template first')
    if len(template) > TEMPLATE_MAX:
        raise ValueError(f'The template is too long (max {TEMPLATE_MAX} characters)')
    tmp = _path(cdir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'template': template, 'auto': bool(auto)}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(cdir))
    return config(cdir)


def made(cdir):
    """{来源 id: 产出摘要}：出过的（在出、出好了）。出失败的不算，下次照样会补。"""
    import study
    return {o['revnote']['sid']: o for o in study.list_outputs(cdir)
            if isinstance(o.get('revnote'), dict) and o.get('status') in ('running', 'done')}


def missing(cdir, sources):
    """sources: [(kind, id, 标题)] → 还没出笔记的那几份（只要文档和录音）。"""
    have = made(cdir)
    return [(k, sid, title) for k, sid, title in sources if k in KINDS and sid not in have]


def estimate(kind, sid):
    """一份笔记大约多少钱：原文按问答模型的输入价，加一份笔记的输出。"""
    import ask
    import notes
    import usage
    p = usage._price_for(ask.ASK_MODEL) or {'input': 0.3, 'output': 2.5}
    tok = notes.estimate(kind, sid).get('tokens', 0)
    return round(tok / 1e6 * p.get('input', 0.3) + OUT_TOKENS / 1e6 * p.get('output', 2.5), 4)


def start(cdir, chain_id, items, run=None):
    """给 items（[(kind, id, 标题)]）里还没笔记的各出一份。→ 开始了几份。"""
    import study
    template = config(cdir)['template']
    if not template:
        return 0
    n = 0
    with _lock:                      # 刷新可能连着来好几次：查「出过没有」和登记要一起做
        have = made(cdir)
        for kind, sid, title in items:
            if kind not in KINDS or sid in have:
                continue
            study.start(cdir, chain_id, 'report', scope={'type': 'all', 'sources': [sid]}, fmt='custom',
                        prompt=template, run=run, extra={'revnote': {'kind': kind, 'sid': sid, 'title': title}})
            have[sid] = True
            n += 1
    return n


def auto(cdir, chain_id, sources, run=None):
    """加完来源后调：开了自动就给新来源出，一次最多 AUTO_MAX 份、估价合计不超过 AUTO_BUDGET_USD。"""
    cfg = config(cdir)
    if not (cfg['auto'] and cfg['template']):
        return 0
    todo, budget = [], AUTO_BUDGET_USD
    for item in missing(cdir, sources)[:AUTO_MAX]:
        try:
            usd = estimate(item[0], item[1])
        except Exception:  # noqa: BLE001
            continue
        if usd > budget:
            break
        todo.append(item)
        budget -= usd
    return start(cdir, chain_id, todo, run=run)
