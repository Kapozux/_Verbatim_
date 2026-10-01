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
"""
import json
import os
import re
import threading
import time

import config
import usage

PURPOSES = {'ask', 'study', 'tag', 'summary', 'enrich', 'review', 'reflect', 'digest', 'compare',
            'translate', 'speakers', 'beliefs', 'demo'}
MODEL = os.environ.get('CHEAP_TEXT_MODEL') or 'deepseek-v4-flash'
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

_state = {'paused_until': 0.0, 'reason': ''}
_lock = threading.Lock()


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
    if (os.environ.get('CHEAP_TEXT_ROUTE') or 'on').strip().lower() in ('off', '0', 'false', 'no'):
        return False
    if not config.dashscope_key():
        return False
    return time.time() >= _state['paused_until']


def use_cheap(prompt, purpose):
    """这一次能不能走 DeepSeek。"""
    return purpose in PURPOSES and enabled() and china_related(prompt) is None


def _pause(err):
    """欠费 / key 错 / 模型不存在：会连着失败，停一阵。审核拦截、超时只是这一次，不停。"""
    s = str(err)
    if any(k in s for k in ('data_inspection', 'DataInspection', 'inappropriate', 'timeout', 'Timeout',
                            '429', '500', '502', '503', '504')):
        return
    with _lock:
        _state.update(paused_until=time.time() + PAUSE_S, reason=s[:200])
    print(f'[llmroute] DeepSeek paused 30 min: {s[:160]}')


def text(prompt, purpose):
    """能走就走 DeepSeek，返回文字；不能走或失败返回 None（调用方接着用 Gemini）。"""
    if not use_cheap(prompt, purpose):
        return None
    from analyze import _call_openai_compat
    try:
        return _call_openai_compat(prompt, MODEL, config.ALIYUN_COMPAT_BASE, config.dashscope_key(),
                                   extra_payload={'enable_thinking': False}, label='DeepSeek',
                                   purpose=purpose) or None
    except Exception as e:  # noqa: BLE001
        _pause(e)
        return None


def stream(prompt, purpose):
    """流式版：返回一个逐段 yield 文字的生成器；不能走或还没出字就失败了返回 None（调用方用 Gemini）。
    已经开始吐字之后再出错就只能往上抛——换模型重来会让前端看到两段拼起来的话。"""
    if not use_cheap(prompt, purpose):
        return None
    import requests
    url = config.ALIYUN_COMPAT_BASE.rstrip('/') + '/chat/completions'
    try:
        r = requests.post(url, headers={'Authorization': f'Bearer {config.dashscope_key()}'}, stream=True,
                          timeout=(15, 300),
                          json={'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
                                'enable_thinking': False, 'stream': True,
                                'stream_options': {'include_usage': True}})
    except requests.RequestException as e:
        _pause(e)
        return None
    if r.status_code != 200:
        _pause(f'{r.status_code}: {r.text[:200]}')
        r.close()
        return None

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
                usage.record_openai(final, 'aliyun', MODEL, purpose)
            else:                   # 中途被停：按字数粗估一笔，别让这次花费消失
                est = lambda s: len(re.findall(r'[一-鿿]', s)) + len(s) // 4   # noqa: E731
                usage.record('aliyun', MODEL, purpose, input_tokens=est(prompt), output_tokens=est(''.join(got)))
    return gen()


def status():
    """给设置页 / 排查用：现在走不走、为什么。"""
    return {'model': MODEL, 'enabled': enabled(), 'has_key': bool(config.dashscope_key()),
            'paused_until': _state['paused_until'] if _state['paused_until'] > time.time() else 0,
            'pause_reason': _state['reason'] if _state['paused_until'] > time.time() else ''}
