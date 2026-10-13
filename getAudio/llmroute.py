"""文本任务的省钱路由：内容跟中国议题无关 → 阿里云百炼 DeepSeek V4 Flash；沾边或拿不准 → 原来的 Gemini。

为什么：摘要、补标题标签、问答、打标签、回顾面板这些是每天几千次的机械活，gemini-2.5-flash /
3.5-flash-lite 输出每百万 token 2.5 美元（2.5-flash 还另收思考 token）；DeepSeek V4 Flash 关掉思考
大约输入 1 元、输出 2 元，输出便宜九倍左右。

红线：时政 / 中国议题的内容绝不送阿里云 / DeepSeek（审查，也不该把这类内容交给国内服务商）。
所以判断宁严勿松——
  · 明显的政治词（中共、习近平、六四、新疆、言论审查、马督工……繁简都认）出现一次 → Gemini；
  · 泛中国词（中国、北京、国内、政府……）出现 3 次以上，算「讲的是中国议题」→ Gemini；
  · 科技语境常见的词只认政治搭配：「颠覆」要「颠覆政权 / 颠覆国家」，「审查」要「言论审查 / 审查制度」，
    不然「颠覆式创新」「代码审查」会把创业类内容全挡掉；
  · 扫的是整份 prompt 里的用户内容（转写、文档、卡片、问题），所以一个英文项目里问到中共也会被拦。
阿里云失败（欠费、审核拦截、限流）一律退回 Gemini，不让功能挂掉；欠费 / key 错这种会连着失败的，
停用 30 分钟再试，免得每次都先撞一下墙。

只接管下面 PURPOSES 里的用途。抽证据卡 / 画像 / 镜头 / 预测核对等跟着项目自己选的「分析模型」走，不归这里管。
关掉：.env 里 CHEAP_TEXT_ROUTE=off；换模型：CHEAP_TEXT_MODEL=…

两条路，按顺序：阿里云百炼（原来的），用户在 Settings 里填了自己的 DeepSeek 官方 key 就再加一条官方 DeepSeek——
阿里云没 key、欠费停用时接着走官方，不用一下子全回到 Gemini。两条路各自停用、各自恢复；红线判断两条都一样。
"""
import json
import os
import re
import threading
import time

import config
import usage

PURPOSES = {'ask', 'study', 'tag', 'summary', 'enrich', 'review', 'reflect', 'digest', 'compare',
            'translate', 'speakers', 'beliefs', 'demo', 'notes'}
MODEL = os.environ.get('CHEAP_TEXT_MODEL') or 'deepseek-v4-flash-0731'   # 10-10 百炼下掉了不带日期的 deepseek-v4-flash（404），同一个模型
GENERIC_MIN = 3
PAUSE_S = 30 * 60

_POL_ZH = """中共 共产党 共產黨 习近平 習近平 习主席 習主席 总书记 總書記 毛泽东 毛澤東 邓小平 鄧小平 江泽民 江澤民
胡锦涛 胡錦濤 温家宝 溫家寶 李克强 李克強 王沪宁 王滬寧 党中央 黨中央 政治局 中南海 六四 天安门 天安門 八九民运
法轮功 法輪功 新疆 维吾尔 維吾爾 西藏 达赖 達賴 台独 台獨 臺獨 港独 港獨 反送中 民主化 极权 極權 独裁 獨裁 专制 專制
维权 維權 上访 上訪 言论审查 言論審查 内容审查 內容審查 网络审查 網絡審查 審查制度 审查制度 电影审查 電影審查 新闻审查 新聞審查 防火墙 防火牆 翻墙 翻牆 敏感词 敏感詞 文革 文化大革命 大跃进 大躍進 大饥荒 大饑荒
刘晓波 劉曉波 艾未未 李文亮 白纸运动 白紙運動 清零 封控 人权 人權 言论自由 言論自由 政权 政權 颠覆国家 顛覆國家 颠覆政权 顛覆政權 维稳 維穩
国安 國安 统战 統戰 两岸 兩岸 解放军 解放軍 武统 武統 台海 臺海 战狼 戰狼 小粉红 小粉紅 五毛 润学 潤學 党媒 黨媒
官媒 新闻联播 新聞聯播 人民日报 人民日報 环球时报 環球時報 胡锡进 胡錫進 司马南 司馬南 马督工 馬督工 督工 睡前消息
立党 立黨 王局 夸克说 夸克說 王志安 文昭 墙国 牆國 赵家人 趙家人 体制内 體制內 中宣部 网信办 網信辦 宣传部 宣傳部
共匪 中华人民共和国 中華人民共和國 公安局 国务院 國務院 人大 政协 政協""".split()
_POL_EN = [r'CCP', r'CPC', r'Chinese Communist', r'Communist Party', r'Xi Jinping', r'Tiananmen', r'Uy?gh?urs?',
           r'Uighurs?', r'Xinjiang', r'Tibet(?:an)?', r'Dalai Lama', r'Falun Gong', r'Taiwan independence',
           r'Hong Kong protests?', r'Great Firewall', r'PRC', r'Chinese government', r'Beijing regime']
_GEN_ZH = """中国 中國 大陆 大陸 内地 內地 国内 國內 北京 上海 深圳 广州 廣州 香港 台湾 臺灣 台灣 国家 國家 人民币 人民幣
国企 國企 民营 民營 房价 房價 高考 央行 发改委 發改委 政府""".split()
_GEN_EN = [r'China', r'Beijing', r'Shanghai', r'Hong Kong', r'Taiwan', r'RMB', r'renminbi']

_POLITICAL = re.compile('|'.join(map(re.escape, _POL_ZH)) + r'|\b(?:' + '|'.join(_POL_EN) + r')\b')
_GENERIC = re.compile('|'.join(map(re.escape, _GEN_ZH)) + r'|\b(?:' + '|'.join(_GEN_EN) + r')\b')

_state = {'paused_until': 0.0, 'reason': ''}               # 阿里云那条
_ds_state = {'paused_until': 0.0, 'reason': ''}            # 官方 DeepSeek 那条
_cu_state = {'paused_until': 0.0, 'reason': ''}            # 用户自己的模型那条
_lock = threading.Lock()


def _cheap_on():
    return (os.environ.get('CHEAP_TEXT_ROUTE') or 'on').strip().lower() not in ('off', '0', 'false', 'no')


def _routes():
    """现在能走的路由（没停用的），按顺序：用户自己的模型（开了「日常文字也用它」才有）排第一，
    然后是便宜路由（CHEAP_TEXT_ROUTE 关了就没有）。"""
    now = time.time()
    out = []
    c = config.custom_llm() if config.custom_llm_everyday() else None
    if c and now >= _cu_state['paused_until']:
        out.append({'name': config.custom_llm_provider(c[0]), 'base': c[0], 'key': c[1] or 'none',
                    'model': c[2], 'extra': {}, 'state': _cu_state})
    if not _cheap_on():
        return out
    if config.dashscope_key() and now >= _state['paused_until']:
        out.append({'name': 'aliyun', 'base': config.ALIYUN_COMPAT_BASE, 'key': config.dashscope_key(),
                    'model': MODEL, 'extra': {'enable_thinking': False}, 'state': _state})
    if config.deepseek_key() and now >= _ds_state['paused_until']:
        out.append({'name': 'deepseek', 'base': config.DEEPSEEK_COMPAT_BASE, 'key': config.deepseek_key(),
                    'model': config.deepseek_models()[0], 'extra': {}, 'state': _ds_state})
    return out


def china_related(text):
    """命中的理由（字符串）或 None。宁严勿松：拿不准就算沾边。"""
    text = text or ''
    m = _POLITICAL.search(text)
    if m:
        return m.group(0)
    hits = _GENERIC.findall(text)
    if len(hits) >= GENERIC_MIN:
        return f'{hits[0]}×{len(hits)}'
    return None


def enabled():
    return bool(_routes())


def use_cheap(prompt, purpose):
    """这一次能不能走 DeepSeek。不按内容预判（10-04 用户：直接丢给 DeepSeek，报错了再回 Gemini）——
    词表里「央行 / 政府 / 国家」这类泛词让财经节目几乎期期被判成「跟中国相关」。审核拦截时 text / stream 返回 None，
    调用方接着用 Gemini。china_related 还留着给别处看，不再拿来分流。"""
    return purpose in PURPOSES and enabled()


def _pause(err, route=None):
    """欠费 / key 错 / 模型不存在：会连着失败，停一阵。审核拦截、超时只是这一次，不停。"""
    s = str(err)
    if any(k in s for k in ('data_inspection', 'DataInspection', 'inappropriate', 'timeout', 'Timeout',
                            '429', '500', '502', '503', '504')):
        return
    st = route['state'] if route else _state
    with _lock:
        st.update(paused_until=time.time() + PAUSE_S, reason=s[:200])
    print(f'[llmroute] {route["name"] if route else "aliyun"} paused 30 min: {s[:160]}')


# 本机 Claude Code 排在最前（TEXT_ENGINE=claude，2026-10-13 用户：「开销越少越好，订阅额度必须都用上」）：
# 订阅额度不另花钱。提取、照着材料写的活用 Haiku（墨页实测认字比 Kimi 准，额度极省）；要判断、找主线、写长文的用 Opus。
# 一开始只管 PURPOSES 那几个；同日用户：「画像也可以 Claude 吧（类似所有文字处理的）」——所以凡是经过 _call_gemini
# （ask._llm 的播客 / 幻灯片 / 声纹……）和 analyze._llm（画像合成 / 镜头 / 简报 / 证伪，不管项目选哪家）的文字活都先给 Claude。
# 不走的：联网核实（grounded，要 Google 搜索）、抽证据卡（归 CARDS_ENGINE，ADR 0017）、看图（_call_gemini_mm）。
# Claude 用不了 / 报错（没登录、撞上限停着）就照旧往下走 DeepSeek → Gemini。
OPUS_PURPOSES = {'review', 'reflect', 'digest', 'compare', 'beliefs',
                 'synth', 'lens', 'revise', 'podcast', 'slides', 'xhs'}
CLAUDE_SKIP = {'cards'}
HAIKU = 'claude-haiku-5-5'
BIG_PROMPT = 300_000      # 字符；画像的材料能到 60 万字，超过这个用 1M 上下文的 Opus（claude -p 认 claude-opus-5-5[1m]，10-13 试过）


def _base(purpose):
    return str(purpose or '').removesuffix('-retry')


def _claude_model(purpose, prompt=''):
    import claude_cli
    if _base(purpose) not in OPUS_PURPOSES:
        return HAIKU
    return claude_cli.MODEL + '[1m]' if len(prompt) > BIG_PROMPT else claude_cli.MODEL


def _claude_effort(purpose):
    return 'medium' if _base(purpose) in OPUS_PURPOSES else 'low'


def _claude_ok(purpose):
    import claude_cli
    return _base(purpose) not in CLAUDE_SKIP and claude_cli.text_engine()


def claude_text(prompt, purpose):
    """只试 Claude：开了且成功返回文字，否则 None（analyze._llm 在走 DeepSeek / OpenRouter 之前用）。"""
    import claude_cli
    if not _claude_ok(purpose):
        return None
    try:
        return claude_cli.text(prompt, purpose=purpose, effort=_claude_effort(purpose),
                               model=_claude_model(purpose, prompt)) or None
    except Exception as e:  # noqa: BLE001
        print(f'[llmroute] Claude Code {purpose} failed, falling back: {str(e)[:120]}')
        return None


_claude_text = claude_text


def _claude_stream(prompt, purpose):
    """先拿到第一段字再交出去：还没出字就失败了返回 None（调用方接着走 DeepSeek），不会让前端看到两段拼起来的话。"""
    import claude_cli
    if not _claude_ok(purpose):
        return None
    gen = claude_cli.stream(prompt, purpose=purpose, effort=_claude_effort(purpose), model=_claude_model(purpose, prompt))
    try:
        first = next(gen)
    except Exception as e:  # noqa: BLE001  StopIteration（空回答）也算没拿到
        print(f'[llmroute] Claude Code {purpose} stream failed, falling back: {str(e)[:120]}')
        return None

    def rest():
        yield first
        yield from gen
    return rest()


def text(prompt, purpose):
    """先 Claude（开了的话），再 DeepSeek，返回文字；都不能走或失败返回 None（调用方接着用 Gemini）。"""
    out = _claude_text(prompt, purpose)
    if out:
        return out
    if not use_cheap(prompt, purpose):
        return None
    from analyze import _call_openai_compat
    for r in _routes():
        try:
            out = _call_openai_compat(prompt, r['model'], r['base'], r['key'], extra_payload=r['extra'] or None,
                                      label=r['name'], purpose=purpose)
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            _pause(e, r)
    return None


def stream(prompt, purpose):
    """流式版：返回一个逐段 yield 文字的生成器；不能走或还没出字就失败了返回 None（调用方用 Gemini）。
    已经开始吐字之后再出错就只能往上抛——换模型重来会让前端看到两段拼起来的话。"""
    claude = _claude_stream(prompt, purpose)
    if claude is not None:
        return claude
    if not use_cheap(prompt, purpose):
        return None
    import requests
    for route in _routes():
        try:
            r = requests.post(route['base'].rstrip('/') + '/chat/completions',
                              headers={'Authorization': f'Bearer {route["key"]}'}, stream=True, timeout=(15, 300),
                              json={'model': route['model'], 'messages': [{'role': 'user', 'content': prompt}],
                                    **route['extra'], 'stream': True, 'stream_options': {'include_usage': True}})
        except requests.RequestException as e:
            _pause(e, route)
            continue
        if r.status_code != 200:
            _pause(f'{r.status_code}: {r.text[:200]}', route)
            r.close()
            continue
        return _relay(r, route, prompt, purpose)
    return None


def _relay(r, route, prompt, purpose):
    model, provider = route['model'], route['name']

    def gen():
        got, final = [], None
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get('usage'):
                    final = obj
                for ch in obj.get('choices') or []:
                    t = (ch.get('delta') or {}).get('content') or ''
                    if t:
                        got.append(t)
                        yield t
        finally:
            r.close()
            if final:
                usage.record_openai(final, provider, model, purpose)
            else:                   # 中途被停：按字数粗估一笔，别让这次花费消失
                est = lambda s: len(re.findall(r'[一-鿿]', s)) + len(s) // 4   # noqa: E731
                usage.record(provider, model, purpose, input_tokens=est(prompt), output_tokens=est(''.join(got)))
    return gen()


def status():
    """给设置页 / 排查用：现在走不走、为什么。"""
    return {'model': MODEL, 'enabled': enabled(), 'has_key': bool(config.dashscope_key()),
            'deepseek_direct': bool(config.deepseek_key()),
            'deepseek_paused_until': _ds_state['paused_until'] if _ds_state['paused_until'] > time.time() else 0,
            'paused_until': _state['paused_until'] if _state['paused_until'] > time.time() else 0,
            'pause_reason': _state['reason'] if _state['paused_until'] > time.time() else ''}
