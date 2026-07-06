# Verbatim

**Audio → transcript → insight.** A local, self-hosted web app that turns hours of audio/video into
timestamped text — and then into structured, cross-referenced opinion research.

Built as a personal tool for studying Chinese commentators/KOLs: drop files or paste a channel URL,
and Verbatim transcribes every episode, writes a per-episode analysis, and synthesizes one document.
The interface is English; the transcribed content keeps its original language.

> The app lives in **[`getAudio/`](getAudio/)** and runs entirely on your machine (Flask, `localhost:5001`).
> Transcription can be local (Whisper) or cloud (Gemini / DashScope); nothing is shared unless you
> configure a cloud engine.

---

## What it does

- **Multi-engine transcription** — pick per job:
  | Engine | Backend | Notes |
  |---|---|---|
  | **Whisper** | `faster-whisper` (int8/CPU, VAD), auto-detect language; falls back to `openai-whisper` | Local · free · offline |
  | **Gemini** | Gemini 2.5 Pro, 15-min chunking, retry + safety-filter diagnostics | Cloud · high quality |
  | **DashScope** | Aliyun Paraformer-v2 (async REST) | Cheap · Chinese-tuned · native timestamps |
  | **Precise** | Gemini text **+** DashScope speaker diarization, merged by Gemini per 10-min window | Speaker labels · slow |

- **Batch transcription** — drag-drop many files; each runs as an independent task with its own progress row.
- **Channel Pipeline** — paste a video / playlist / channel URL →
  `yt-dlp download → transcribe → per-episode AI analysis → cross-episode synthesis`.
  Downloads and transcription overlap; a clickable episode grid shows per-video status with thumbnails.
- **AI layers** — structured summaries (overview + timestamped sections) and auto-generated card metadata
  (title / one-liner / tags) for every transcript.
- **Library** — full-text search across titles, filenames, and transcript text; engine filters; a
  rendered Markdown reading view for analysis documents.
- **Personal dashboard** — bottom-left panel: total hours transcribed, characters produced, a cumulative-hours
  line chart, and the topics you follow (from tags).
- **Exports** — TXT, SRT subtitles, and downloadable Markdown documents.

---

## Architecture

```
Browser (SPA: tabs, SSE progress, self-drawn charts, client-side Markdown renderer)
   │  fetch / SSE
Flask (getAudio/app.py)
   ├─ ThreadPoolExecutor + per-engine Semaphores      ── transcription concurrency
   ├─ global download / analysis Semaphores            ── pipeline throttling (shared across all chains)
   ├─ SQLite (taskdb.py)                                ── task state + restart recovery
   └─ filesystem: results/<uuid>/{audio, transcript.json, summary.json, meta.json}
                  results/_chains/<id>/{chain.json, 分析_*.md, 总分析.md}
```

**Design principles baked in**

- **Global, not per-request, throttling.** Download and analysis concurrency is capped by *shared* semaphores,
  so running 5 pipelines at once produces the same external load as running 1 — they queue, they don't compound.
- **Overlap I/O and compute.** In a pipeline, each video starts transcribing the moment it finishes downloading
  (wall-clock ≈ the slower of the two, not their sum).
- **Persist everything.** Tasks live in SQLite; a restart re-queues unfinished work and cleans orphaned uploads.
  (Chains keep their state in `chain.json` but do not yet auto-resume — see Roadmap.)
- **Safe by construction.** `task_id` is UUID-validated before any filesystem join (path-traversal guard);
  document reads reject `..` / non-`.md` names; optional token auth gates the whole app.

---

## Setup

**Requirements:** Python 3.9+, `ffmpeg` and `yt-dlp` on `PATH` (Homebrew recommended on macOS).

```bash
brew install ffmpeg yt-dlp          # yt-dlp MUST be the system binary, not a pip package
python3 -m venv venv && source venv/bin/activate
pip install -r getAudio/requirements.txt
```

Create `getAudio/.env`:

```ini
GEMINI_API_KEY=...          # for Gemini transcription, summaries, and pipeline analysis
DASHSCOPE_API_KEY=...       # for the Aliyun (DashScope) engine
# Optional:
# GETAUDIO_TOKEN=...             # enable token auth (blank = open, local use)
# YTDLP_COOKIES_BROWSER=chrome   # borrow browser cookies (fixes Bilibili 412, raises YouTube limits)
# WHISPER_LANGUAGE=zh            # force a language for local Whisper (blank = auto-detect)
```

Run:

```bash
bash getAudio/run.sh          # → http://localhost:5001
```

---

## Configuration (`getAudio/config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `ENGINE_CONCURRENCY` | whisper 2, gemini 8, dashscope 9, precise 4 | Global per-engine transcription concurrency |
| `CHAIN_DOWNLOAD_CONCURRENCY` | 4 | Global cap on simultaneous downloads (all pipelines) |
| `CHAIN_ANALYSIS_CONCURRENCY` | 4 | Global cap on simultaneous analysis calls |
| `YTDLP_LANG` | `zh-CN` | yt-dlp metadata language (Chinese titles) |
| `YTDLP_COOKIES_FROM_BROWSER` | `chrome` | Browser to borrow cookies from (`''` disables) |
| `WHISPER_MODEL_SIZE` | `small` | faster-whisper model |
| `MAX_CONTENT_LENGTH` | 500 MB | Upload size limit |

---

## Key endpoints

| Method / Path | What |
|---|---|
| `POST /upload`, `POST /upload_batch` | Submit file(s) for transcription |
| `GET  /stream/<task_id>` | SSE progress stream |
| `GET  /api/history`, `/api/history/<id>`, `.../audio` | List / fetch / play saved transcripts |
| `GET  /api/search?q=` | Full-text search |
| `POST /api/enrich_all` | Backfill AI titles/tags |
| `POST /api/chain` | Start a Channel Pipeline |
| `GET  /api/chains`, `/api/chain/<id>`, `.../files`, `.../file?name=` | Pipeline status & documents |
| `GET  /api/stats` | Aggregated dashboard data |

---

## Project layout

```
getAudio/
├─ app.py                  # Flask app: routes, workers, pipeline orchestration, auth, stats
├─ config.py               # config + concurrency/throttle params
├─ taskdb.py               # SQLite task persistence + restart recovery
├─ downloader.py           # yt-dlp probe/download (cookies, zh-CN titles, thumbnails)
├─ analyze.py              # per-episode analysis + cross-episode synthesis
├─ summarize.py            # AI content summary (Gemini / Qwen)
├─ enrich.py               # AI card metadata (title / one-liner / tags)
├─ transcribe_whisper.py   # local engine (faster-whisper → openai-whisper fallback)
├─ transcribe_gemini.py    # Gemini engine
├─ transcribe_dashscope.py # Aliyun Paraformer engine
├─ transcribe_precise.py   # Gemini text + DashScope diarization merge
├─ templates/index.html    # single-page UI
├─ static/{app.js, style.css}
├─ results/                # per-task outputs (+ _chains/ for pipelines) — git-ignored
└─ requirements.txt
```

Full source is also concatenated into [`getAudio/完整源代码_Verbatim.md`](getAudio/完整源代码_Verbatim.md).

---

## Design language

The UI follows an Anthropic/Claude-inspired system: warm ivory ground (`#FAF9F5`), Claude coral accent
(`#D97757`), **serif display titles over small Apple-system body text**. Status is carried by color +
uppercase labels rather than emoji. All theming lives in CSS custom properties at the top of
`getAudio/static/style.css`.

---

## Known limitations / roadmap

- **Pipelines don't auto-resume** after a server restart (transcripts do; chain state is saved but not re-run).
- **`--cookies-from-browser`** requires the named browser installed locally.
- Gemini may return empty on safety-filtered content; the pipeline marks that episode failed and continues.
- **Planned:** route pipeline *analysis* to the Claude API for higher-quality reasoning (Gemini stays for
  cheap transcription); Gemini **Batch API** for 50%-cheaper, rate-limit-free analysis; pipeline auto-resume;
  SQLite FTS index for search at scale.

---

*Personal project. Transcription/analysis of third-party content is for private study; respect the source
platforms' terms and creators' rights.*
