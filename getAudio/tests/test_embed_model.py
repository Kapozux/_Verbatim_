"""向量文件记下模型名和维度（借 Open Notebook：维度不同的向量不参与检索）：
换了 EMBED_MODEL 之后，旧向量不能和新问题的向量混着点积，要当作没有、整份重算。

跑生产代码：ask.embed_chain / load_embeddings / semantic_rank。
桩（只在模型边界）：ask._embed_texts（确定性向量，记下每次算了几条）。
数据：一个分析过的博主（2 期 5 张卡）；一份手写的旧格式 embeddings.npz（没有 model / dim 字段）。
"""
import os

from _support import Checks, fake_embed, isolate, make_creator

TMP = isolate('embed_model')
import numpy as np  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402

t = Checks()
embedded = []


def counting_embed(texts, task=None):
    embedded.append(len(texts))
    return fake_embed(texts, task)


ask._embed_texts = counting_embed

cid = make_creator(A, 'Ann', 'ann', [['small teams win', 'ship weekly', 'talk to users'],
                                    ['hire slowly', 'fire fast']])
d = A._chain_dir(cid)
path = os.path.join(d, ask._EMB_FILE)

# ---- 1 新写的文件带模型名和维度 ----
r = ask.embed_chain(d)
z = np.load(path, allow_pickle=False)
t.check('写入时记下模型名', 'model' in z.files and str(z['model']) == ask.EMBED_MODEL,
        f'files={z.files}')
t.check('写入时记下维度', 'dim' in z.files and int(z['dim']) == ask.EMBED_DIM)
t.check('同一个模型再跑一次：不重算', ask.embed_chain(d)['embedded'] == 0)

# ---- 2 旧格式文件（没有 model / dim）按默认模型认，不白花钱重算 ----
ids, hashes = z['ids'], z['hashes']
np.savez(path, ids=ids, hashes=hashes, vecs=z['vecs'])
ask._emb_cache.clear()
t.check('旧格式文件按默认模型读得出来', ask.load_embeddings(d) is not None)
t.check('旧格式文件：同一个模型不重算', ask.embed_chain(d)['embedded'] == 0)

# ---- 3 换了模型：旧向量不参与检索，整份重算 ----
orig = ask.EMBED_MODEL
ask.EMBED_MODEL = 'some-other-embedding'
ask._emb_cache.clear()
t.check('换模型后旧向量当作没有', ask.load_embeddings(d) is None)
corpus = ask.load(d, passages=True)
t.check('换模型后按意思排返回 None（只走关键词）',
        ask.semantic_rank(corpus, 'teams', corpus['cards'], 3) is None)
embedded.clear()
r = ask.embed_chain(d)
t.check('换模型后整份重算', r['embedded'] == r['total'] and sum(embedded) == r['total'],
        f'{r} {embedded}')
z = np.load(path, allow_pickle=False)
t.check('重算后记下新模型名', str(z['model']) == 'some-other-embedding')
ask.EMBED_MODEL = orig

# ---- 4 维度对不上（模型名一样，比如改了 EMBED_DIM）也算对不上 ----
np.savez(path, ids=ids, hashes=hashes, vecs=np.zeros((len(ids), 256), dtype=np.float16),
         model=np.array(ask.EMBED_MODEL), dim=np.array(256))
ask._emb_cache.clear()
t.check('维度不同当作没有', ask.load_embeddings(d) is None)

t.finish()
