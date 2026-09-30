# Transcription engines

[中文版](zh-CN/engines.md) · [Getting started](getting-started.md) · [Creators](creators.md) · [FAQ](faq.md)

You pick the engine on the **Transcribe** page, under **Engine**. For creators it is under
**Advanced settings — engine, model, language → Transcription engine**. Engines under
**Show mainland-cloud engines ▾** are hidden until you click it.

![The engine list, with the mainland-cloud engines shown](img/en/transcribe-input.png)

Under each engine the page shows how fast it has been on your machine, for example
"≈ 4.9 min per hour of audio · last 12 runs". That comes from your own history, so it changes over time.

## Summary

| Engine (as shown in the app) | Runs where | Key needed | Speaker labels | Good for |
|---|---|---|---|---|
| **Gemini 3.5 Transcribe** (default) | Google cloud | Gemini | Yes | Most things; interviews and call-ins |
| **Gemini** | Google cloud | Gemini | No | Mixed Chinese and English |
| **Whisper** | Your Mac | None | No | Free, offline, private material |
| **Qwen-ASR** | Alibaba Cloud (mainland China) | DashScope | Yes | Chinese audio that isn't sensitive |
| **Precise (diarization)** | Google + Alibaba | Gemini and DashScope | Yes | Older two-engine speaker mode |
| Subtitles (not selectable) | — | None | No | Videos that already have captions |

## Gemini 3.5 Transcribe

- The default engine everywhere.
- Uses Google's `gemini-3.5-transcribe` speech model (a preview model). One call returns the text,
  word-level timing, and speaker labels ("说话人1：", "说话人2：" at the start of each line).
- Long audio is cut into pieces of up to 28 minutes. Speakers are numbered per piece, so in long
  recordings "Speaker 1" in one piece may not be the same person as "Speaker 1" in the next.
- If Google rate-limits you (HTTP 429), all running jobs wait together and then continue. They are not
  marked failed.
- Needs the Gemini key. The summary after each transcript also uses Gemini.
- Cost: the built-in price table has no price for this model, so **Settings → Costs** lists its calls
  as unpriced. Check your Google billing page for the real charge.

## Gemini

- Uses a general Gemini model to transcribe. The default is `gemini-2.5-flash`; you can change it in
  **Settings → Models → Gemini · transcription model**.
- Keeps the original language and does not translate. Good when speakers switch between Chinese and
  English.
- No speaker labels.
- Audio is sent in 15-minute pieces.
- Needs the Gemini key. Its calls are priced in **Settings → Costs**.

## Whisper

- Runs on your Mac. Nothing is uploaded and no key is needed. The engine itself is free.
- The model downloads on first use. The built-in default is `large-v3` (about 3 GB). You can choose a
  smaller model in **Settings → Models → Whisper model**; smaller is faster but less accurate on
  Chinese.
- **Only runs when the Mac is plugged in.** On battery the job fails with a message telling you to plug
  in or use a cloud engine. Whisper uses every performance core, and a long run on battery once drained
  a laptop until it shut down mid-job.
- It is also the fallback: if Gemini blocks a file for content reasons, or a cloud engine returns an
  empty transcript, that file is redone with Whisper automatically. For creators, the **Whisper
  fallback** option extends this to other cloud failures (quota, errors). Fallback is also blocked on
  battery.
- Even with Whisper, the summary and AI title after each transcript are cloud calls (about 1¢ each).
  With no Gemini key they are skipped; the transcript itself still works.

## Qwen-ASR

- Alibaba Cloud speech recognition (currently `qwen-audio-3.1-asr-flash-filetrans`). Strong on
  Chinese. Adds speaker labels.
- Needs the DashScope key (**Settings → DashScope**). Summaries for these transcripts use Qwen.
- **Content is moderated.** Audio goes to Alibaba Cloud in mainland China. Politically sensitive
  material may be refused, garbled, or altered. The app asks you to confirm before using it. For
  sensitive audio, use Whisper or Gemini.
- In the Creators form it is listed as **Alibaba Qwen-ASR (zh ASR · no Gemini filter)**: it does not go
  through Gemini's content filter, which helps with material Gemini keeps blocking, as long as it isn't
  politically sensitive.
- ASR calls are recorded in **Settings → Costs** by audio length but have no built-in price.

## Precise (diarization)

- Runs Gemini for the text and Alibaba for "who spoke when", then merges them.
- Needs both the Gemini and DashScope keys. Has the same content moderation warning as Qwen-ASR.
- Only this engine shows **Expected speakers**. Filling it in can improve accuracy; leave it blank to
  detect automatically.
- Gemini 3.5 Transcribe gives speaker labels in a single call with one key, which is simpler.

## Subtitles (fast path)

Not an engine you pick, but you will see it in transcript details (**Subtitles**).

- On **Transcribe**, **Existing subtitles** is **Auto — the video's own language** by default. If a
  linked video has captions in its own language, Verbatim uses them and skips download and
  transcription. It is fast and costs nothing.
- It never uses a machine translation. Pick a specific language to require that one; if it isn't
  there, the audio is transcribed. **Off — always transcribe the audio** turns this off.
- Subtitles that look cut off are rejected and the audio is transcribed instead.
- Time ranges (`@10:00-25:00`) skip the subtitle check, because subtitle timing doesn't line up with
  a clip.
- In Creators the matching option is **Use existing subtitles when a video has them**. Auto-captions can
  be rougher than a real transcript. Subtitles have no speaker labels, so this option is ignored when
  you ask to tell speakers apart.

## DashScope (Paraformer)

Older records may show **DashScope (Paraformer)** as their engine, and the Library has a filter chip
for it. It was an earlier Alibaba engine. It is no longer offered for new transcripts; those records
still open normally.

## What it costs

- Every model call is recorded. See **Settings → Costs** for totals by provider, purpose, and model.
- Engines without a built-in price (Gemini 3.5 Transcribe, Qwen-ASR) are counted but shown as unpriced.
  Developers can add prices in a `prices.json` file next to `tasks.db`.
- A summary is about 1¢ per transcript. In the author's own log, 1,350 Gemini summary calls came to
  $15.87, a little over 1¢ each.
- Before a creator run, the new-creator form shows an estimate. See [Creators](creators.md).

## Which should I use?

- **Just starting:** Gemini 3.5 Transcribe.
- **No key, or private recordings:** Whisper, with the Mac plugged in.
- **Mixed Chinese and English, no speakers needed:** Gemini.
- **Chinese content Gemini keeps blocking, not politically sensitive:** Qwen-ASR.
- **A YouTube or Bilibili video that has captions:** leave **Existing subtitles** on Auto.
