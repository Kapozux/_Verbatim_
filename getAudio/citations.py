"""出处（见 GLOSSARY：出处）：给模型看的 id、把模型写回来的出处认成真 id、收集用到的卡。

一次回答 / 生成 / 对比 / 同步摘要里，模型只能引用给它看过的卡和段落。它写回来的出处五花八门——
[@4-35, @4-36]、[#3-1, #3-2]、裸的 [3-12]、[_#4-9]、字母写错的 [#C:5-8]、没合上的 [#B:2-84, …、
段落短别名 t4-16、前面加「引文：」——这里统一认成真 id；编造的删掉并记下来。

用法：
    cit = Citations().add(corpus, cards)            # 给模型看过哪些卡
    line = f'[#{prompt_id(card, corpus)}] | …'      # 提示词里卡片写成什么 id
    text = cit.clean(model_output)                   # → 只剩真 id 的 [#…]
    cit.used        # {真 id: 出处卡片（给前端）}，按第一次出现的顺序
    cit.dropped_ids # 编造的 id

id 的写法只在这里定义一份（ID）；前端从页面模板拿同一份（app.py 注入 window.CITE_ID）。
"""
import re

# 卡片 3-12（文件编号-下标；老文件带 _哈希）、段落 t302223-16 / d77-2、代码库卡 r4660-3、引用博主的 B:3-12（见 ADR-0001）
ID = r'(?:[A-Z]:)?[dtr]?\d+(?:_[0-9a-f]{8})?-\d+'
MARK = re.compile(r'\[#(' + ID + r')\]')

_BAD = re.compile(r'\[#[^\]\n]{0,80}\]')                       # 走样到认不出的「出处」
_GROUP = re.compile(r'\[\s*((?:[_\\]*[#@]?\s*' + ID + r'\s*[,，;、]?\s*)+)\]')
_ONE = re.compile(ID)
_DANGLING = re.compile(r'\[#(' + ID + r')\s*[,，;、]\s*(?=\[)')
_ALIAS_TOKEN = re.compile(r'(?<![\w:-])((?:[A-Z]:)?[dtr]?\d+-\d+)(?![\w-])')
_TAGGED = re.compile(r'\[#([A-Z]):([dtr]?\d+(?:_[0-9a-f]{8})?-\d+)\]')
_LEAD = re.compile(r'(?:引文|出处|来源|参考|引用|Sources?|Citations?|References?)\s*[:：]\s*(?=\[#)', re.I)
_SPACE_BEFORE_PUNCT = re.compile(r'[ \t]+([.,;:!?。，；：！？)])')


def prompt_id(card, corpus):
    """提示词里这张卡写成什么 id。卡片就用真 id；原文段落用跟它的标签对得上的短别名（t4-16 = EP4 第 16 段，
    d2-3 = DOC2 第 3 段）——真 id（t302223-16）又长又看不出规律，模型会自作主张缩短，被当成编造的删掉。
    引用博主的段落再带上他的字母：B:t4-16。"""
    ep = corpus['episodes'][card['ep']]
    if card.get('layer') != 'source' and ep.get('kind') != 'repo':
        return card['id']
    no = re.sub(r'\D', '', ep.get('ui_label') or ep.get('label') or '') or str(card['ep'] + 1)
    tag = f"{ep['person_tag']}:" if ep.get('person_tag') else ''
    kind = {'doc': 'd', 'repo': 'r'}.get(ep.get('kind'), 't')
    return f"{tag}{kind}{no}-{card['id'].rsplit('-', 1)[1]}"


def strip(text):
    """去掉全部出处标记（聊天记录喂回模型、判断回答语言时用）。"""
    return MARK.sub('', text or '')


def find(text):
    """文字里出现的出处 id，按出现顺序。"""
    return MARK.findall(text or '')


def keep(text, wanted):
    """只留 wanted(id) 为真的出处，其余删掉。"""
    return MARK.sub(lambda m: m.group(0) if wanted(m.group(1)) else '', text or '')


class Citations:
    """一次回答 / 生成里模型能引用的全部卡片，以及它真正引用了哪些。
    view(card, corpus, prefix) 把卡片变成给前端的出处（默认 ask.card_view）。"""

    def __init__(self, view=None):
        self._view = view
        self._cards = {}          # 真 id → (卡片, corpus, 前缀, 人名)
        self._aliases = {}        # 提示词里的短别名 → 真 id
        self.used = {}
        self.dropped_ids = []
        self.dropped = 0          # 编造的 + 格式坏到认不出的

    def add(self, corpus, cards, prefix='', creator=None):
        """登记给模型看过的卡。prefix：对比时每个人的卡加 A:/B:，好在同一段文字里分开；creator：出处上显示的人名。"""
        for c in cards:
            real = prefix + c['id']
            self._cards[real] = (c, corpus, prefix, creator)
            alias = prompt_id(c, corpus)
            if alias != c['id']:                       # 原文段落、代码库卡给模型看的是短别名
                self._aliases[prefix + alias] = real
        return self

    def __contains__(self, cid):
        return cid in self._cards

    def resolve(self, token):
        """单个 id（短别名、带 #、带方括号都行）→ 真 id，并记为用到；认不出返回 None。"""
        tok = str(token or '').strip().strip('[]').lstrip('#').strip()
        real = self._aliases.get(tok) or (tok if tok in self._cards else None)
        if real:
            self._use(real)
        return real

    def clean(self, text):
        """模型原文 → 只剩真 id 的 [#…]；编造的删掉（记进 dropped_ids）。"""
        out = _LEAD.sub('', str(text or ''))              # 「引文：」「Source:」——芯片自己就说明是出处
        out = _DANGLING.sub(r'[#\1]', out)                 # [#B:2-84, [#B:2-32] → 先把没合上的补齐
        out = self._unalias(out)
        out = self._fix_tags(out)
        out = _GROUP.sub(self._normalize, out)

        def mark(m):
            cid = m.group(1)
            if cid in self._cards:
                self._use(cid)
                return f'\x00{cid}\x00'                    # 先占位，免得被下面清走样引用那步误删
            self.dropped += 1
            self.dropped_ids.append(cid)
            return ''
        out = MARK.sub(mark, out)
        out, n = _BAD.subn('', out)                        # [##1-0 through #4-236] 这种点不回去，删掉
        self.dropped += n
        out = re.sub(r'\x00([^\x00]+)\x00', r'[#\1]', out)
        return _SPACE_BEFORE_PUNCT.sub(r'\1', out).strip()

    # ---------------- 内部 ----------------

    def _use(self, real):
        if real in self.used:
            return
        card, corpus, prefix, creator = self._cards[real]
        view = self._view
        if view is None:
            from ask import card_view as view               # 晚一点 import：ask 也要 import 这个模块
        v = view(card, corpus, prefix)
        if creator:
            v['creator'] = creator
        self.used[real] = v

    def _unalias(self, text):
        """方括号里的短别名 → 真 id（正文里的数字不碰）。模型连 t/d 都省了（4-16）、又没有这张卡时，按 t4-16 / d4-16 认。"""
        if not self._aliases:
            return text

        def fix(m):
            tok = m.group(1)
            real = self._aliases.get(tok)
            if real is None and tok not in self._cards:
                pre, raw = (tok[:2], tok[2:]) if tok[1:2] == ':' else ('', tok)
                real = self._aliases.get(pre + 't' + raw) or self._aliases.get(pre + 'd' + raw)
            return m.group(0).replace(tok, real) if real else m.group(0)
        return re.sub(r'\[[^\]\n]{1,200}\]', lambda b: _ALIAS_TOKEN.sub(fix, b.group(0)), text)

    def _fix_tags(self, text):
        """好几个人时模型偶尔把字母写错（讲 YC 的句子引成 [#C:5-8]，其实是 B:5-8）。
        只在它写的字母下没有这张卡、而换个字母恰好只有一张时才改；有歧义就不动，交给后面当编造的删掉。"""
        by_raw = {}
        for v in self._cards:
            if v[1:2] == ':':
                by_raw.setdefault(v[2:], []).append(v)
        if not by_raw:
            return text

        def fix(m):
            cid = f'{m.group(1)}:{m.group(2)}'
            if cid in self._cards:
                return m.group(0)
            alt = by_raw.get(m.group(2)) or []
            return f'[#{alt[0]}]' if len(alt) == 1 else m.group(0)
        return _TAGGED.sub(fix, text)

    def _normalize(self, m):
        """[@4-35, @4-36]、[#3-1, #3-2]、[3-12]、[_#4-9] → [#4-35][#4-36]。没带 #/@ 的裸方括号只在里面
        全是真 id 时才认，免得把正文里的 [2026-09] 当出处删掉。"""
        ids = _ONE.findall(m.group(1))
        if not re.search(r'[#@]', m.group(1)) and not all(i in self._cards for i in ids):
            return m.group(0)
        return ''.join(f'[#{i}]' for i in ids)
