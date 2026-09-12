# Verbatim

**Audio/video → transcript → insight.** A local, self-hosted web app that turns hours of audio, video,
and Xiaohongshu notes into timestamped text — and then into structured, evidence-anchored research.

Built as a personal tool for studying Chinese commentators/KOLs: drop a file, paste a video/collection
link, or point it at a creator's channel, and Verbatim transcribes, analyzes, and synthesizes. The
interface is English; transcribed/analyzed content keeps its original language (or pick English/中文
output explicitly).

> Runs entirely on your machine (Flask, `localhost:5001`). Transcription can be local (Whisper, GPU-
> accelerated on Apple Silicon) or cloud (Gemini / Alibaba); nothing leaves your machine unless you
> configure and use a cloud engine.

![Verbatim](../docs/verbatim.png)

---

## What it does

### Transcribe
Four ways in, one Library out:
- **Drag-drop files** — batch upload, each file its own progress row.
- **Paste links** — one or many, mixed freely with files in the same box. A single video downloads
  and transcribes; a **channel / playlist / Bilibili 合集** link is expanded into its videos (capped by
  "videos per collection") and each is transcribed independently. Add `@10:00-25:00` after a link to
  clip just that range — cheaper, and timestamps stay at the real video position.
- **Local file paths** — for files already on the machine, paste the path instead of uploading:
  Flask reads it in place (symlinked into `uploads/`, original never touched, never deleted), so a
  multi-GB video costs no upload time. (macOS blocks reading `~/Downloads` and `~/Desktop` from
  background processes — keep source files in `Documents` or elsewhere.)
- **Engines**: Whisper (local), Gemini, Alibaba Qwen-ASR, or Precise (Gemini text + Alibaba
  speaker diarization, merged with per-window timestamp validation). A cloud engine's *deterministic*
  content-block (safety filter, copyright refusal) auto-falls-back to local Whisper for that file —
  transient failures (rate limits, network) don't, so retrying the same engine still makes sense there.
- Every transcription also gets a **structured AI summary** (overview + timestamped sections) and
  auto-generated card metadata (title / one-liner / tags) — separate from, and lighter than, the
  cross-episode synthesis Creators does.
- **Exports** — every transcript downloads as TXT or SRT subtitles; every analysis document as Markdown.

### Xiaohongshu
Keyword search → scrape → multimodal analysis, three steps, one tab:
- Enter keywords (searched separately, merged, de-duped) and a note/comment cap; Verbatim drives a
  real browser (via a companion scraper project) to search, pull full-resolution images, and collect
  top-level comments — interruptible, every note saved as it lands.
- **Analyze this batch**: Gemini reads each note's images *and* comments, extracts structured
  per-note findings, then aggregates into one research report. Scoped to the keywords that produced
  the batch, so topics never mix even when the dataset holds several unrelated scrapes.

### Creators
Paste a channel, playlist, or single video → full pipeline:
`download → transcribe → per-episode evidence-card extraction → cross-episode synthesis (portrait)`.
- **Evidence-anchored analysis**: each episode is reduced to neutral "cards" (atomic observation +
  verbatim quote + timestamp + evidence layer — self-evident / his claim / externally verified) before
  any judgment happens. The portrait is built *only* from cards; every evaluative sentence must trace
  back to a quote. Rhetoric metrics (hype/hedge/tradeoff counts) are summed in Python, not estimated
  by the model. For creators with dozens of episodes, cards are map-reduced into batch briefs before
  synthesis rather than truncated.
- **Analysis "brain"**: Gemini (default), Alibaba (DeepSeek / Qwen / Kimi / GLM — cheap, content-
  moderated, only for non-sensitive creators), or Claude Opus 4.6 / 5 via OpenRouter (unmoderated,
  higher cost, scales with episode count).
- **Self-verify**: after synthesis, extract every evaluative claim, send each to an adversarial
  skeptic against the evidence cards, cut what the cards don't support and soften what's overstated —
  with an appendix of what changed and why.
- **Web fact-check**: ground external claims (papers, models, real-world events) in live search before
  judging them, so the model doesn't call something real "fabricated" just because it's past its
  training cutoff.
- **Lenses** — re-read the same evidence cards through a different synthesis prompt, near-zero
  marginal cost: 🔥 Roast · ✍️ Craft · 😂 Watchability · 💬 Quotes · 🖼 Worldview. Same grounding rule:
  every jab must cite a quote or it doesn't get written.
- **Prefer subtitles** skips download + transcription entirely when a video already has captions.
  **Continue** resumes an interrupted/failed pipeline reusing everything already done (including
  after a server restart — chains don't auto-resume, but nothing already finished is re-spent).
- Output language: auto (follow content) / English / 中文, applied to every generated document.
- Creator cards show real avatar, follower count, and total views (backfilled from the platform, not
  guessed).

### Library
- **Transcripts**: full-text search across titles/filenames/transcript text; filter by engine, and by
  **source** — 🙋 things you transcribed yourself vs 🎬 output of a Creators pipeline run (tagged with
  which creator), so your own work doesn't get buried once a pipeline has produced hundreds of episodes.
- **Analyses**: rendered-Markdown reading view for every portrait/lens document, downloadable.
- **Stats dashboard** (bottom-left) — total hours transcribed, characters produced, a cumulative-hours
  chart, and the topics you follow (from AI-generated tags).
- **Settings** — manage Gemini / DashScope / OpenRouter API keys from the UI, test each with a live
  call before saving.

---

## Architecture

```
Browser (SPA: 4 tabs, SSE progress, self-drawn charts, client-side Markdown renderer, XSS-safe rendering)
   │  fetch / SSE
Flask (app.py, ~3000 lines)
   ├─ ThreadPoolExecutor + per-engine Semaphores        ── transcription concurrency
   ├─ global download / analysis Semaphores             ── pipeline throttling (shared across all chains)
   ├─ subprocess (isolated py3.12 venv)                 ── Xiaohongshu scraper, streamed via a background thread
   ├─ SQLite (taskdb.py)                                ── task state + restart recovery
   └─ filesystem: results/<uuid>/{audio, transcript.json, summary.json, meta.json}
                  results/_chains/<id>/{chain.json, 分析_*.md, 总分析.md, 镜头_*.md}
                  xhs_dataset/notes/<id>/{images, comments.csv, meta.json}
```

**Design principles baked in**

- **Control flow lives in Python, not the model.** `harness.py` provides two primitives — `fanout`
  (concurrent, order-preserving map; one failure doesn't sink the batch) and `agent` (one LLM call,
  retries, optional JSON-schema validation) — and every multi-step pipeline is a plain Python loop over
  them. No autonomous "agent decides what to do next": that's expensive, hard to bound, and prone to
  drift.
- **Global, not per-request, throttling.** Download and analysis concurrency is capped by *shared*
  semaphores, so running 5 pipelines at once produces the same external load as running 1.
- **Overlap I/O and compute.** In a pipeline, each video starts transcribing the moment it finishes
  downloading (wall-clock ≈ the slower of the two, not their sum).
- **Persist everything, atomically.** Tasks live in SQLite; a restart re-queues unfinished uploads and
  cleans orphaned files. `chain.json` has a single writer path (reentrant lock + tmp-file + `os.replace`)
  so concurrent updates from a running pipeline, a single-video retry, and a metadata backfill can't
  tear the file or silently lose each other's writes.
- **Never trust a single ASR/LLM signal.** `sanitize.py` treats every segment as guilty until proven
  innocent — filler-run collapse, verbatim-loop dedup, malformed-timestamp repair, silence-masked
  hallucination removal — but only acts when multiple signals agree, and never deletes short-but-real
  content. A model can flag suspect transcript spans for review; only deterministic code deletes them,
  and a single review window can't wipe more than 40% of its own segments (a hallucinated "drop
  everything" verdict can't nuke real content).
- **Safe by construction.** `task_id`/`chain_id` are validated before any filesystem join (path-
  traversal guard); local-file ingest reads via a symlink so the original can never be deleted; all
  user-influenced strings (video titles, authors, URLs) are HTML-escaped before rendering; the optional
  token auth uses a constant-time comparison.

---

## Setup

**Requirements:** Python 3.9+, `ffmpeg` and `yt-dlp` on `PATH` (Homebrew recommended on macOS).

```bash
brew install ffmpeg yt-dlp          # yt-dlp MUST be the system binary, not a pip package
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt     # mlx-whisper auto-skips on non-Apple-Silicon
```

Create `getAudio/.env`:

```ini
GEMINI_API_KEY=...          # Gemini transcription, summaries, pipeline analysis, XHS image analysis
DASHSCOPE_API_KEY=...       # Alibaba engine (Qwen-ASR transcription; DeepSeek/Qwen/Kimi/GLM analysis)
OPENROUTER_API_KEY=...      # optional: Claude Opus analysis brain (unmoderated, higher cost)
# Optional:
# GETAUDIO_TOKEN=...             # enable token auth (blank = open, local use)
# YTDLP_COOKIES_BROWSER=chrome   # borrow browser cookies (fixes Bilibili 412, raises YouTube limits)
# WHISPER_LANGUAGE=zh            # force a language for local Whisper (blank = auto-detect)
# WHISPER_MODEL_SIZE=large-v3    # small/medium/large-v3 — runtime-read, no restart needed
# WHISPER_CONCURRENCY=4          # parallel local-transcription slots
# MAX_UPLOAD_MB=4096             # upload size cap
# XHS_PROJECT=/path/to/XHS-Downloader   # Xiaohongshu tab needs this companion scraper project + `uv`
```

Run:

```bash
bash run.sh          # → http://localhost:5001
```

---

## Configuration (`config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `ENGINE_CONCURRENCY` | whisper 4, gemini 12, dashscope/qwenasr 9, precise 4 | Global per-engine transcription concurrency |
| `CHAIN_DOWNLOAD_CONCURRENCY` | 4 | Global cap on simultaneous downloads (all pipelines) |
| `CHAIN_ANALYSIS_CONCURRENCY` | 4 | Global cap on simultaneous analysis calls |
| `ANALYSIS_PRESETS` | gemini / deepseek / qwen / kimi / glm / opus46 / opus5 | Analysis "brain" → (provider, extract model, synth model) |
| `WHISPER_MODEL_SIZE` | `large-v3` | faster-whisper / mlx-whisper model (runtime-overridable) |
| `WHISPER_CPU_THREADS` × `ENGINE_CONCURRENCY['whisper']` | 3 × 4 ≈ 12 | Sized to fit performance cores on the CPU fallback path |
| `DASHSCOPE_ASR_MODEL` | `qwen-audio-3.0-asr-flash-filetrans` | Alibaba ASR model |
| `YTDLP_LANG` | `zh-CN` | yt-dlp metadata language (Chinese titles) |
| `YTDLP_COOKIES_FROM_BROWSER` | `chrome` | Browser to borrow cookies from (`''` disables) |
| `MAX_UPLOAD_MB` | 4096 | Upload size limit |

---

## Key endpoints

| Method / Path | What |
|---|---|
| `POST /upload`, `/upload_batch` | Submit file(s) for transcription |
| `POST /api/transcribe_urls` | Video/collection link(s) → download + transcribe (Transcribe tab) |
| `POST /api/transcribe_local` | Local file path → zero-upload transcription |
| `GET  /stream/<task_id>` | SSE progress stream |
| `GET  /api/history`, `/api/history/<id>`, `.../audio` | List / fetch / play saved transcripts (tagged `source`: mine / pipeline) |
| `GET  /api/search?q=` | Full-text search |
| `POST /api/enrich_all` | Backfill AI titles/tags |
| `POST /api/chain` | Start a Creators pipeline |
| `GET  /api/chains`, `/api/chain/<id>`, `.../files`, `.../file` | Pipeline status & documents |
| `POST /api/chain/<id>/reanalyze`, `/retry`, `/stop` | Rerun analysis only / resume / cancel a pipeline |
| `POST /api/chain/<id>/lens`, `GET .../lens/<lens>` | Generate / fetch a lens document |
| `POST /api/xhs/scrape`, `/stop`, `GET /status` | Xiaohongshu keyword scrape lifecycle |
| `POST /api/xhs/analyze`, `GET /analyze_status`, `/report` | Xiaohongshu batch analysis → report |
| `GET  /api/stats` | Aggregated dashboard data |
| `GET/POST /api/settings`, `POST /api/settings/test` | API keys — view (masked) / save / test a live call |

---

## Project layout

```
getAudio/
├─ app.py                    # Flask app: routes, workers, pipeline + XHS orchestration, auth, stats
├─ config.py                 # engines, concurrency/throttle params, analysis-brain presets
├─ taskdb.py                 # SQLite task persistence + restart recovery
├─ downloader.py              # yt-dlp probe/download (cookies, zh-CN titles, collection URL
│                             #   normalization, Bilibili festival-page auto-recovery, retry/backoff)
├─ harness.py                 # fanout/agent primitives — deterministic orchestration, zero deps
├─ analyze.py                  # evidence cards, map-reduce synthesis, self-verify, lenses, XHS analysis
├─ sanitize.py                 # anti-hallucination transcript cleaning + timeline repair
├─ summarize.py                # AI content summary
├─ enrich.py                    # AI card metadata (title / one-liner / tags)
├─ audioutil.py                  # post-transcription Opus compression of archived audio
├─ transcribe_whisper.py          # local engine: mlx (Apple Silicon GPU) → faster-whisper (CPU) →
│                                 #   openai-whisper, in that fallback order
├─ transcribe_gemini.py            # Gemini engine: chunking, retry, safety-filter diagnostics
├─ transcribe_dashscope.py          # Alibaba Qwen-ASR engine (async REST)
├─ transcribe_precise.py             # Gemini text + Alibaba diarization, per-window merge validation
├─ templates/index.html               # single-page UI (4 tabs)
├─ static/{app.js, style.css}
├─ results/                            # per-task outputs (+ _chains/ for pipelines)
└─ requirements.txt

# One level up (sibling to getAudio/, not part of this package):
../xhs_pipeline.py     # Xiaohongshu scraper entrypoint — a separate project, invoked as a subprocess
../xhs_dataset/notes/  # scraped notes (images + comments), read by analyze.py's XHS functions
```

Full source of the `getAudio/` package is also concatenated into
[`完整源代码_Verbatim.md`](完整源代码_Verbatim.md).

---

## Design language

The UI follows an Anthropic/Claude-inspired system: warm ivory ground (`#FAF9F5`), Claude coral accent
(`#D97757`), **serif display titles over small Apple-system body text**. Status is carried by color +
uppercase labels rather than emoji (emoji are used deliberately for lenses/badges, not as status icons).
All theming lives in CSS custom properties at the top of `style.css`.

---

## Known limitations / roadmap

- **Pipelines don't auto-resume** after a server restart — they're marked failed with a note to click
  **Continue**, which reuses everything already transcribed/analyzed and only redoes what's missing.
- **`--cookies-from-browser`** requires the named browser installed locally.
- Gemini may content-block on sensitive material; that video auto-falls-back to local Whisper (see
  Transcribe above) rather than just failing.
- The **Xiaohongshu tab** needs a companion scraper project (`XHS_PROJECT` env var) and `uv` — it's not
  a pip dependency, since it drives a real logged-in browser session.
- **Not yet built:** SQLite FTS index for search at scale (currently linear substring scan); pipeline
  auto-resume across restarts; Gemini Batch API for cheaper, rate-limit-free bulk analysis.

---

*Personal project. Transcription/analysis of third-party content is for private study; respect the source
platforms' terms and creators' rights.*
