"""
用 Antigravity CLI（agy）跑 Gemini 转写——走用户自己的 Gemini 订阅额度，不花 API 的钱。

2026-11-04：用户想把日常开销都放进订阅里（API key 绑的是家里的卡）。Gemini CLI 6-18 起不再服务个人订阅，
接班的是 Antigravity CLI（`brew install --cask antigravity-cli` → agy），`agy -p … --output-format json` 可以无头跑。
实测（伤逝那节课 44 分钟）：
  · agy_argon（gemini-4-argon-low）：一次过 15.6k 字，时间戳连续，专有名词最准；耗 Argon 周额度约 2.7% / 44 分钟。
  · agy_flash（gemini-3.8-flash-low）：整段送会被安全过滤误拦，按 15 分钟切段就没事；字数约 Argon 的 89%，
    专有名词会听错；时间戳偏长（15 分钟的块标到 16–17 分钟），这里按块长等比拉回；几乎不耗额度。
默认 agy_flash（用户 11-04：「默认 Flash」）。

几个坑（都实测过）：
  · `@文件` 不会把音频附上；要在提示词里让它用 view_file 打开绝对路径，并说「不要运行命令」，
    否则它先去跑 ls，无头模式拒掉就一个字不出。
  · m4a 不认（audio/mp4a-latm）；ogg/opus 读进去只转了最后几秒；mp3 稳。所以这里自己切 mp3，不复用 Gemini 那个切 ogg 的。
  · 额度：`agy -p /usage --output-format json` 给每个桶剩几成（Gemini 桶有 5 小时 + 周，Argon 单独一个周桶）。
    剩得少于 AGY_MIN_REMAINING（默认 5%）就当撞了上限，交给 app 的后备（OpenRouter → Whisper）。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION

import config
import usage
from transcribe_gemini import _get_audio_duration_seconds, parse_timestamped_text, shift_timestamps
from transcribe_openrouter import _STRICT

ENGINES = ('agy_flash', 'agy_argon')
MODELS = {'agy_flash': 'gemini-3.8-flash-low', 'agy_argon': 'gemini-4-argon-low'}
# 每个引擎看哪几个额度桶（/usage 里的 bucket id）
BUCKETS = {'agy_flash': ('gemini-5h', 'gemini-weekly'), 'agy_argon': ('gemini-argon-wtu-weekly',)}

CHUNK_SECONDS = 15 * 60
_TIMEOUT_S = 600
_QUOTA_TTL_S = 300
_TS = re.compile(r'\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]')
_BLOCK_MARKERS = ("blocked by Gemini's filters", 'blocked by gemini', 'is not supported by the current model')

_inflight = {e: threading.Semaphore(config.ENGINE_CONCURRENCY.get(e, 3)) for e in ENGINES}
_quota_cache = {'t': 0.0, 'data': None}
_quota_lock = threading.Lock()


class AgyQuotaOut(RuntimeError):
    pass


class _Blocked(RuntimeError):
    pass


def agy_bin():
    """agy 的路径。launchd 起的 5001 的 PATH 里常常没有 /opt/homebrew/bin，所以 which 找不到再看 Homebrew 默认位置。"""
    if not config.agy_installed():
        return None
    p = (os.environ.get('AGY_BIN') or '').strip()
    if p:
        return p
    found = shutil.which('agy')
    if found:
        return found
    for cand in ('/opt/homebrew/bin/agy', '/usr/local/bin/agy', os.path.expanduser('~/.local/bin/agy')):
        if os.path.isfile(cand):
            return cand
    return None


def available():
    return bool(agy_bin())


def min_remaining():
    try:
        return float(os.environ.get('AGY_MIN_REMAINING') or 0.05)
    except ValueError:
        return 0.05


def _run_json(args, timeout, cwd=None):
    """跑一次 agy，返回它打的那个 JSON（stdout 里最后一个以 { 开头的行）。"""
    b = agy_bin()
    if not b:
        raise RuntimeError('没找到 agy（Antigravity CLI）：brew install --cask antigravity-cli，然后在终端里运行一次 agy 登录')
    proc = subprocess.run([b, *args, '--output-format', 'json'], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout, cwd=cwd)
    out = proc.stdout or ''
    line = next((ln for ln in reversed(out.strip().splitlines()) if ln.lstrip().startswith('{')), None)
    if line is None:
        msg = (proc.stderr or out or '').strip()[:300]
        if 'Authentication required' in msg or 'sign in' in msg.lower():
            raise RuntimeError('agy 没登录：在「终端」App 里运行一次 agy，用有 Gemini 订阅的账号登录')
        raise RuntimeError(f'agy 没有返回结果（exit {proc.returncode}）：{msg}')
    return json.loads(line)


def quota(force=False):
    """{bucket_id: 剩余比例}；五分钟内的读数复用。读不到返回 None（agy 坏了不该挡住转写，转写那步自己会报错）。"""
    with _quota_lock:
        if not force and _quota_cache['data'] is not None and time.time() - _quota_cache['t'] < _QUOTA_TTL_S:
            return _quota_cache['data']
    try:
        d = _run_json(['-p', '/usage'], timeout=60)
        groups = ((d.get('command') or {}).get('data') or {}).get('groups') or []
        data = {b['id']: float(b['remaining_fraction']) for g in groups for b in g.get('buckets') or []
                if 'remaining_fraction' in b}
    except Exception:  # noqa: BLE001
        return None
    with _quota_lock:
        _quota_cache.update(t=time.time(), data=data)
    return data


def check_quota(engine):
    q = quota()
    if not q:
        return
    low = [(b, q[b]) for b in BUCKETS[engine] if b in q and q[b] < min_remaining()]
    if low:
        b, v = low[0]
        raise AgyQuotaOut(f'agy quota: {engine} 的订阅额度 {b} 只剩 {v:.0%}（低于 {min_remaining():.0%}），先让给后备引擎')


def rescale_timestamps(text, chunk_seconds):
    """Flash 标的时间普遍偏长（15 分钟的块标到 16–17 分钟）。最后一个时间戳超过块长时，整块等比拉回：
    最后一行按「块长 − 半个平均行距（最多 30 秒）」放。没超就原样返回。"""
    if not chunk_seconds:
        return text
    ts = [_parse(m) for m in _TS.findall(text)]
    if len(ts) < 2 or ts[-1] <= chunk_seconds:
        return text
    gap = ts[-1] / (len(ts) - 1)
    factor = max(0.5, (chunk_seconds - min(gap / 2, 30)) / ts[-1])

    def repl(m):
        return f'[{_fmt(int(round(_parse(m.group(1)) * factor)))}]'
    return _TS.sub(repl, text)


def _parse(s):
    parts = [int(x) for x in s.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return h * 3600 + m * 60 + sec


def _fmt(sec):
    return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


# 提示词：11-04 实测 Verbatim 通用那份长提示词（[HH:MM:SS]、八条规矩）喂给 agy 的 Flash，第一块 15 分钟只出 5 个时间戳，
# 再强调一次也没用；手动测时这份短的同一块出 34 行。所以 agy 用短的，只补上「别翻译」「说话人」两条。
_AGY_PROMPT = (
    '逐字转写这段音频的全文：从头到尾不要跳过、不要总结、不要省略、不要润色，严格保留原始语言，不要翻译。\n'
    '大约每 30 秒另起一行，行首标时间戳 [mm:ss]（从这段音频的开头算起）。\n'
    '如果有不止一个人说话，在每段开头标出说话人，格式为「说话人1：」「说话人2：」；只有一个人说话就不用标。\n'
    '静音和没人说话的地方直接跳过。只输出转写，不要任何解释。'
)


def _prompt(path, strict=False):
    return (f'用 view_file 工具打开这个音频文件：{path}\n'
            '不要运行任何命令，不要读别的文件，只用 view_file 打开这一个。\n\n' + _AGY_PROMPT
            + (_STRICT if strict else ''))


def _ask(model, path, cwd, strict=False):
    d = _run_json(['-p', _prompt(path, strict), '--model', model, '--print-timeout', f'{_TIMEOUT_S}s'],
                  timeout=_TIMEOUT_S + 60, cwd=cwd)
    u = d.get('usage') or {}
    usage.record('agy', model, 'transcribe', input_tokens=u.get('input_tokens') or 0,
                 output_tokens=u.get('output_tokens') or 0, thinking_tokens=u.get('thinking_tokens') or 0,
                 cost_usd=0.0)
    text = d.get('response') or ''
    if d.get('denied_actions') and not text.strip():
        # 例：音频不在 agy 的工作目录里，read_file 被拒，它不报错只回空
        raise RuntimeError(f"agy 被拒了权限、没转出字：{[a.get('action') for a in d['denied_actions']]}")
    low = text.lower()
    if any(m.lower() in low for m in _BLOCK_MARKERS):
        raise _Blocked(text.strip()[:200])
    if str(d.get('status') or '').upper() != 'SUCCESS':
        raise RuntimeError(f"agy 状态 {d.get('status')}: {text[:200]}")
    if 'quota' in low and any(w in low for w in ('exceed', 'exhaust', 'reached', 'limit')) and not _TS.search(text):
        raise AgyQuotaOut(f'agy quota: {text.strip()[:200]}')
    return text


def _transcribe_chunk(engine, path, chunk_seconds, cwd):
    """转一块。Flash 被安全过滤误拦就这一块先再试一次 Flash、还拦再换 Argon；还拦就当内容拦截报上去（SAFETY）。
    时间戳太稀就带提醒再要一次。"""
    model = MODELS[engine]
    with _inflight[engine]:
        try:
            text = _ask(model, path, cwd)
            # 时间戳太稀（11-04 实测：Flash 第一块 15 分钟只打了一个）→ 带「再强调一次」再要一遍，取时间戳多的那份
            if chunk_seconds and chunk_seconds >= 120 and len(_TS.findall(text)) < chunk_seconds / 90:
                try:
                    again = _ask(model, path, cwd, strict=True)
                    if len(_TS.findall(again)) > len(_TS.findall(text)):
                        text = again
                except _Blocked:
                    pass
        except _Blocked as e:
            if engine != 'agy_flash':
                raise RuntimeError(f'agy 内容被安全过滤 SAFETY：{e}') from e
            try:
                # Flash 的误拦有随机性（11-04 同一块连跑两次：一次拦、一次正常 12 个时间戳），先原样再来一次，省 Argon 的周额度
                text = _ask(model, path, cwd)
            except _Blocked:
                try:
                    check_quota('agy_argon')
                    text = _ask(MODELS['agy_argon'], path, cwd)
                except _Blocked as e2:
                    raise RuntimeError(f'agy 内容被安全过滤 SAFETY（Flash 两次、Argon 一次都拦）：{e2}') from e2
    return rescale_timestamps(text, chunk_seconds)


def split_mp3(filepath, chunk_seconds, out_dir):
    """切成 mp3 块（agy 只稳定认 mp3），返回 [(路径, 起点秒)]。尾巴不到 30 秒就并进上一块。"""
    duration = _get_audio_duration_seconds(filepath) or 0
    chunks, start, i = [], 0, 0
    while True:
        remaining = duration - start
        this_len = chunk_seconds if remaining > chunk_seconds + 30 else max(remaining, 0) + 1
        p = os.path.join(out_dir, f'chunk_{i:04d}.mp3')
        subprocess.run([config.FFMPEG_BIN, '-y', '-v', 'error', '-ss', str(start), '-t', str(this_len),
                        '-i', filepath, '-vn', '-ac', '1', '-b:a', '64k', p],
                       check=True, capture_output=True, text=True, timeout=600)
        chunks.append((p, int(start)))
        start += this_len
        i += 1
        if start >= duration:
            break
    return chunks, duration


def transcribe_audio(filepath, engine='agy_flash', progress_callback=None):
    """转写整个文件，返回 (segments, full_text)，跟 transcribe_openrouter.transcribe_audio 一样。"""
    if engine not in ENGINES:
        raise ValueError(f'不认识的 agy 引擎：{engine}')
    check_quota(engine)
    tmp = tempfile.mkdtemp(prefix='agy_chunks_')
    try:
        chunks, duration = split_mp3(filepath, CHUNK_SECONDS, tmp)
        total = len(chunks)

        def _chunk_len(idx):
            end = chunks[idx + 1][1] if idx + 1 < total else int(duration)
            return end - chunks[idx][1] if end > chunks[idx][1] else None

        parts = [None] * total
        done = [0]
        lock = threading.Lock()

        def _one(idx):
            path, offset = chunks[idx]
            n_len = _chunk_len(idx)
            text = _transcribe_chunk(engine, path, n_len, tmp)
            parts[idx] = shift_timestamps(text, offset, chunk_seconds=n_len).strip()
            if progress_callback:
                with lock:
                    done[0] += 1
                    n = done[0]
                progress_callback(min(99, 5 + int(90 * n / total)))

        if progress_callback:
            progress_callback(5)
        if total == 1:
            _one(0)
        else:
            pool = ThreadPoolExecutor(max_workers=min(3, total))
            try:
                futures = [pool.submit(usage.bound(_one), i) for i in range(total)]
                wait(futures, return_when=FIRST_EXCEPTION)
                failed = next((f for f in futures if f.done() and f.exception()), None)
                if failed is not None:
                    raise failed.exception()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        full_text = '\n'.join(p for p in parts if p)
        if progress_callback:
            progress_callback(100)
        return parse_timestamped_text(full_text), full_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
