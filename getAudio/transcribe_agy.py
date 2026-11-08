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
  · 11-05 接进共用的闸（claude_gate 第三家 agy）：开了 CLAUDE_GATE 就由闸管额度（默认给用户留四成，他平时自己也用 Gemini；
    CLAUDE_GATE_AGY_KEEP 改）、占并发位子、记进共用账本，撞上限三边一起停；上面 AGY_MIN_REMAINING 那套只在没开闸时用。
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

import claude_gate
import config
import usage
from transcribe_gemini import _get_audio_duration_seconds, parse_timestamped_text, shift_timestamps
from transcribe_openrouter import _STRICT

ENGINES = ('agy_flash', 'agy_argon')
MODELS = {'agy_flash': 'gemini-3.8-flash-low', 'agy_argon': 'gemini-4-argon-low'}
# 每个引擎看哪几个额度桶（/usage 里的 bucket id）
BUCKETS = {'agy_flash': ('gemini-5h', 'gemini-weekly'), 'agy_argon': ('gemini-argon-wtu-weekly',)}

CHUNK_SECONDS = 15 * 60
_TIMEOUT_S = int(os.environ.get('AGY_TIMEOUT_S') or 900)   # 11-06：5 路并发时 15 分钟一片偶尔 600 秒写不完，回半截稿 + ERROR
_QUOTA_TTL_S = 300
# 卡住就掐（11-07，用户：「15 分钟的一个切片是不是有一个固定的转写时间…超过 1.5 倍时间，自动删了重试」）：
# gate.db 1319 次 Flash 实测，15 分钟一片中位 40–50 秒、九成在 80 秒内；卡住的（思考绕圈、输出上万 token）
# 要 300–500 秒才回 ERROR。按 80 秒的 1.5 倍：每分钟音频给 8 秒（15 分钟 = 120 秒，最少 30 秒），到点掐掉马上重来。
# 11-08 改：以前掐满 2 次就不计时、按 900 秒干等，实测一期卡住 + ERROR 轮着来，第 3 次一等 15 分钟。
# 现在每次都计时、每掐一次限时放宽 1.5 倍（120 → 180 → 270），真慢的片也有机会跑完；
# 这一片 Flash 卡住 + 回 status ERROR 合计 3 次就换 Argon 转这一片（跟被安全过滤拦时一样），不再死磕 Flash。
STUCK_GROWTH = 1.5
FLASH_GIVE_UP = 3
_STUCK_MIN_S = 30


def stuck_limit(chunk_seconds):
    """Flash 这一片最多等几秒算卡住；AGY_STUCK_PER_MIN=0 关掉（返回 None）。"""
    try:
        per_min = float(os.environ.get('AGY_STUCK_PER_MIN') or 8)
    except ValueError:
        per_min = 8.0
    if per_min <= 0:
        return None
    return max(_STUCK_MIN_S, int(round((chunk_seconds or CHUNK_SECONDS) / 60 * per_min)))
_TS = re.compile(r'\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]')
_BLOCK_MARKERS = ("blocked by Gemini's filters", 'blocked by gemini', 'is not supported by the current model')

# 全局同时几个切片（所有视频加起来；config.AGY_CHUNKS）。Flash 被拦改走 Argon 时占的是 Argon 的位子
_inflight = {e: threading.Semaphore(config.AGY_CHUNKS.get(e, 3)) for e in ENGINES}
_quota_cache = {'t': 0.0, 'data': None}
_quota_lock = threading.Lock()


class AgyQuotaOut(RuntimeError):
    pass


# 额度用到线时怎么办（2026-11-06，用户：「等额度恢复再转」，不改走付费的 OpenRouter）：
# AGY_QUOTA_WAIT=on → 停下来，每 WAIT_POLL_S 秒看一次，恢复了接着转；链条被停了就不再等；最多等 WAIT_MAX_S。
# 没开（默认）→ 照旧马上抛 AgyQuotaOut，交给 app 的后备（OpenRouter → Whisper）。
WAIT_POLL_S = 300
WAIT_MAX_S = 48 * 3600
on_wait = None            # 测试 / 日志用：on_wait(原因)，每轮开始等的时候调一次


def quota_wait_on():
    return (os.environ.get('AGY_QUOTA_WAIT') or '').strip().lower() in ('on', '1', 'true', 'yes')


class Stopped(RuntimeError):
    pass


def _nap(should_stop):
    """睡一轮（WAIT_POLL_S），中途链条被停了就抛 Stopped。"""
    end = time.time() + WAIT_POLL_S
    while True:
        if should_stop and should_stop():
            raise Stopped('已随链条停止（当时在等 agy 额度恢复）')
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(5.0, left))


def _waiting(why, t0):
    """开始 / 继续等一轮：开关没开就抛 AgyQuotaOut；等太久也抛。"""
    why = str(why)
    if not why.startswith('agy quota'):
        why = f'agy quota: {why}'
    if not quota_wait_on():
        raise AgyQuotaOut(why)
    if time.time() - t0 > WAIT_MAX_S:
        raise AgyQuotaOut(f'{why}（等了 {WAIT_MAX_S // 3600} 小时还没恢复）')
    if on_wait:
        on_wait(why)
    else:
        print(f'[agy] 额度不够，等 {WAIT_POLL_S // 60} 分钟再看：{why[:160]}', flush=True)


def wait_quota(engine, should_stop=None):
    """转之前看额度；不够就按 AGY_QUOTA_WAIT 的规矩等（或马上抛）。"""
    t0 = time.time()
    while True:
        try:
            check_quota(engine)
            return
        except AgyQuotaOut as e:
            _waiting(e, t0)
            _nap(should_stop)


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
    if claude_gate.enabled():                      # 闸开着：额度由闸看（给用户留几成、三边共用一份读数）
        short = claude_gate.agy_short(MODELS[engine])
        if short:
            raise AgyQuotaOut(f'agy quota: {short}')
        return
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


# agy 偶尔连 Google 断线（11-06 邵艾伦频道实测：日志 `loadCodeAssist: EOF`，回 status ERROR、内容为空），
# 以前一次就交给付费的 OpenRouter 后备。现在这一块先隔一会儿再试几次（订阅，不花钱），都不行才往上抛。
FLAKY_WAITS = (20, 60, 180)


class _Flaky(RuntimeError):
    partial = False      # 回了 status ERROR 但带半截稿（模型那边绕圈 / 被掐断），不是断线


class _Stuck(RuntimeError):
    pass


class _GiveUp(RuntimeError):
    """Flash 这一片卡住 / 回 ERROR 太多次，换 Argon。"""


def _ask(model, path, cwd, strict=False, should_stop=None, limit=None):
    """limit：这一片超过几秒算卡住（只有 Flash 传）。卡住掐掉马上重来，每次限时放宽 STUCK_GROWTH 倍；
    卡住 + 回 status ERROR 合计 FLASH_GIVE_UP 次抛 _GiveUp，由调用方换 Argon。"""
    flaky = stuck = bad = 0
    name = os.path.basename(path)
    while True:
        lim = int(round(limit * STUCK_GROWTH ** stuck)) if limit else None
        try:
            return _ask_once(model, path, cwd, strict, should_stop, limit=lim)
        except _Stuck as e:
            stuck += 1
            bad += 1
            if bad >= FLASH_GIVE_UP:
                print(f'[agy] {name} 超过 {lim} 秒没回，Flash 已卡住 {stuck} 次、出错 {bad - stuck} 次，换 Argon', flush=True)
                raise _GiveUp(f'卡住 {stuck} 次、出错 {bad - stuck} 次') from e
            print(f'[agy] {name} 超过 {lim} 秒没回（卡住了），掐掉重来（第 {stuck} 次）', flush=True)
        except _Flaky as e:
            if limit and e.partial:      # 回了 ERROR + 半截稿：跟卡住一样算 Flash 这片不行（空 ERROR 是断线，换 Argon 也没用）
                bad += 1
                if bad >= FLASH_GIVE_UP:
                    print(f'[agy] {name} Flash 已卡住 {stuck} 次、出错 {bad - stuck} 次（{str(e)[:60]}），换 Argon', flush=True)
                    raise _GiveUp(f'卡住 {stuck} 次、出错 {bad - stuck} 次') from e
            if flaky >= len(FLAKY_WAITS):
                raise RuntimeError(str(e)) from e
            w = FLAKY_WAITS[flaky]
            flaky += 1
            print(f'[agy] {os.path.basename(path)} 第 {flaky} 次出错（{str(e)[:80]}），{w} 秒后重试', flush=True)
            _sleep(w, should_stop)


def _sleep(secs, should_stop):
    end = time.time() + secs
    while time.time() < end:
        if should_stop and should_stop():
            raise Stopped('已随链条停止（当时在等 agy 重试）')
        time.sleep(min(1.0, max(0.0, end - time.time())))


def _ask_once(model, path, cwd, strict=False, should_stop=None, limit=None):
    w0 = time.time()
    while True:
        t0 = time.time()
        try:
            with claude_gate.slot('verbatim', 'transcribe', 'normal', provider='agy', model=model):
                d = _run_json(['-p', _prompt(path, strict), '--model', model, '--print-timeout', f'{_TIMEOUT_S}s'],
                              timeout=limit or _TIMEOUT_S + 60, cwd=cwd)
            break
        except claude_gate.Paused as e:          # 闸说额度要留给用户 / 三边一起停着：按规矩等或马上抛
            _waiting(e, w0)
            _nap(should_stop)
        except FileNotFoundError:
            raise                                # 切片 / 工作目录没了（这一期已经放弃），重试没用
        except subprocess.TimeoutExpired as e:   # subprocess.run 到点会杀掉 agy
            if limit:
                raise _Stuck(str(e)[:120]) from e
            raise _Flaky(f'agy 没回结果：{type(e).__name__}: {str(e)[:120]}') from e
        except (json.JSONDecodeError, OSError) as e:
            raise _Flaky(f'agy 没回结果：{type(e).__name__}: {str(e)[:120]}') from e
        except RuntimeError as e:                # 多个 agy 同时起，偶尔读不到登录（日志 You are not logged into Antigravity）
            if str(e).startswith(('agy 没有返回结果', 'agy 没登录')):
                raise _Flaky(str(e)[:200]) from e
            raise
    claude_gate.record('verbatim', 'transcribe', model, d, time.time() - t0, 'normal', provider='agy')
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
    quota_txt = 'quota' in low and any(w in low for w in ('exceed', 'exhaust', 'reached', 'limit'))
    if str(d.get('status') or '').upper() != 'SUCCESS':
        if not quota_txt:
            e = _Flaky(f"agy 状态 {d.get('status')}: {text[:200]}")
            e.partial = bool(text.strip())
            raise e
        raise RuntimeError(f"agy 状态 {d.get('status')}: {text[:200]}")
    if 'quota' in low and any(w in low for w in ('exceed', 'exhaust', 'reached', 'limit')) and not _TS.search(text):
        claude_gate.pause(time.time() + 1800, text.strip()[:200], 'verbatim', provider='agy')    # 三边一起停半小时
        _waiting(text.strip()[:200], w0)
        _nap(should_stop)
        return _ask_once(model, path, cwd, strict, should_stop)
    return text


def _transcribe_chunk(engine, path, chunk_seconds, cwd, should_stop=None):
    """转一块。Flash 被安全过滤误拦就这一块先再试一次 Flash、还拦再换 Argon；还拦就当内容拦截报上去（SAFETY）。
    时间戳太稀就带提醒再要一次。换 Argon 时先让出 Flash 的位子、再排 Argon 的（别占着一个干等）。"""
    model = MODELS[engine]
    lim = stuck_limit(chunk_seconds) if engine == 'agy_flash' else None    # Argon 没实测过多久、额度也金贵，不掐
    blocked = gave_up = None
    with _inflight[engine]:
        try:
            text = _ask(model, path, cwd, should_stop=should_stop, limit=lim)
            # 时间戳太稀（11-04 实测：Flash 第一块 15 分钟只打了一个）→ 带「再强调一次」再要一遍，取时间戳多的那份
            if chunk_seconds and chunk_seconds >= 120 and len(_TS.findall(text)) < chunk_seconds / 90:
                try:
                    again = _ask(model, path, cwd, strict=True, should_stop=should_stop, limit=lim)
                    if len(_TS.findall(again)) > len(_TS.findall(text)):
                        text = again
                except (_Blocked, _GiveUp):           # 已经有一份稿了，再要一遍不成就用这份
                    pass
        except _GiveUp as e:
            gave_up = e
        except _Blocked as e:
            if engine != 'agy_flash':
                raise RuntimeError(f'agy 内容被安全过滤 SAFETY：{e}') from e
            try:
                # Flash 的误拦有随机性（11-04 同一块连跑两次：一次拦、一次正常 12 个时间戳），先原样再来一次，省 Argon 的周额度
                text = _ask(model, path, cwd, should_stop=should_stop, limit=lim)
            except _Blocked as e2:
                blocked = e2
            except _GiveUp as e2:
                gave_up = e2
    if gave_up is not None and blocked is None:
        wait_quota('agy_argon', should_stop)
        with _inflight['agy_argon']:
            try:
                text = _ask(MODELS['agy_argon'], path, cwd, should_stop=should_stop)
            except _Blocked as e3:
                raise RuntimeError(f'agy 内容被安全过滤 SAFETY（Flash {gave_up}、Argon 拦了）：{e3}') from e3
    if blocked is not None:
        wait_quota('agy_argon', should_stop)
        with _inflight['agy_argon']:
            try:
                text = _ask(MODELS['agy_argon'], path, cwd, should_stop=should_stop)
            except _Blocked as e3:
                raise RuntimeError(f'agy 内容被安全过滤 SAFETY（Flash 两次、Argon 一次都拦）：{e3}') from e3
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


def transcribe_audio(filepath, engine='agy_flash', progress_callback=None, should_stop=None):
    """转写整个文件，返回 (segments, full_text)，跟 transcribe_openrouter.transcribe_audio 一样。"""
    if engine not in ENGINES:
        raise ValueError(f'不认识的 agy 引擎：{engine}')
    wait_quota(engine, should_stop)
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

        gone = threading.Event()             # 一片彻底失败、整期放弃 → 其它片别再转 / 重试（临时目录马上删）

        def stop():
            return gone.is_set() or bool(should_stop and should_stop())

        def _one(idx):
            path, offset = chunks[idx]
            n_len = _chunk_len(idx)
            try:
                text = _transcribe_chunk(engine, path, n_len, tmp, stop)
            except BaseException:
                gone.set()
                raise
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
            # 有几片开几路，真正同时跑几片由 _inflight（全局切片位子）管：别的视频没在用时一个视频能用满
            pool = ThreadPoolExecutor(max_workers=min(config.AGY_CHUNKS.get(engine, 3), total))
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
