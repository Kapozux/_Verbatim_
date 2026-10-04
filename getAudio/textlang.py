"""内容是中文还是英文，给「输出跟原文同一种语言」用。

提示词大多是中文写的，只写一句「跟原文同一种语言」时，便宜模型（DeepSeek / flash-lite）会跟着提示词写中文——
2026-10-03 All-In 四期英文节目的摘要、标题、笔记本综述全成了中文。所以先在这里判断好，再把「用英文 / 用中文」
明确写进提示词。按汉字数 vs 英文单词数比：中文里夹英文术语、英文里带几个中文地名都判得对。"""
import re

_CJK = re.compile(r'[㐀-鿿豈-﫿]')
_WORD = re.compile(r'[A-Za-z]{2,}')


def detect(text, sample=20000):
    """'zh' / 'en'；几乎没有文字时 None（调用方照旧让模型跟随原文）。"""
    s = (text or '')[:sample]
    cjk, words = len(_CJK.findall(s)), len(_WORD.findall(s))
    if cjk + words < 5:
        return None
    return 'zh' if cjk >= words else 'en'


def rule(text, fields):
    """写进提示词的一句话：fields 是要求的字段（如 'overview / title / summary'）。判断不了时返回原来的「跟随原文」。"""
    lang = detect(text)
    if lang == 'en':
        return f'Write {fields} in English — the source is in English.'
    if lang == 'zh':
        return f'{fields} 全部用中文写（原文是中文）。'
    return f'{fields} 用与原文相同的语言输出——原文是中文就用中文，是英文就用英文。'
