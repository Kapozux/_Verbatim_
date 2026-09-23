<img src="docs/logo-512.png" width="72" alt="">
-Kerwin 
# Verbatim

Transfer audio and video to text with time-stamps, and then organize tens of videos of a Youtuber into a referenced source document. Runs locally, Flask, open localhost:5001 and you can use it.

I made it because I have a lot of class audio and Chinese Youtuber videos I want to analyze. The current tools are either expensive or have a bad understanding of the audio language. So I just made one. I have used it for about half a year, already processed 1340 files, 533 hours of audio.

![Verbatim](docs/verbatim.png)

## What can it do?

**Transcribe.** Drag an audio/video file into the section, or paste a video link (any site yt-dlp supports), or paste a local file path. Add `@10:00-25:00` after a link to only transcribe that section. A collection or playlist link is automatically broken down into single videos. Every video after transcribing gets an automatically generated summary, title and tags. You can search in full text, and export as Markdown and SRT.

Engines:

| Engine | |
|---|---|
| Whisper | Local, free, uses Apple Silicon GPU |
| Gemini | Cloud, good for Chinese-English mixed speech |
| Gemini 3.5 Transcribe | Cloud, can distinguish between different speakers |
| Qwen-ASR | Alibaba Cloud, strong on Chinese |
| Precise | Gemini gives the text, Alibaba gives the speakers, then combine them |

A cloud engine automatically falls back to Whisper when it hits content scrutiny.

**Creator analysis.** Paste a channel link. It will download the videos, transcribe them, then extract evidence cards from each one (an observation of a fact in the video, the quote from the video, and the timestamp), and finally compress them into a portrait of the Youtuber. Every comment in the portrait can be traced to its source. It also lets the model check the mistakes inside the portrait and delete the ones without evidence. The same evidence cards can be re-read from different perspectives: Roast, speaking style, watchability, golden sentences, worldview. You can use models like Gemini, Claude (via OpenRouter) or DeepSeek / Kimi / GLM / Qwen.

**Xiaohongshu / Rednote.** Enter a keyword. It will open a browser to search, grab pictures and comments, then use Gemini to read the pictures and comments and form a report. It needs another repo to support this function.

**Reflection.** In the bottom-left there's a button where you can see what you were doing in 1 month or 3 months, check which day you transcribed the most hours of audio, and what topics you were listening to.

## How to run

Needs Python 3.9+, ffmpeg and yt-dlp in PATH.

```bash
brew install ffmpeg yt-dlp
python3 -m venv venv && source venv/bin/activate
pip install -r getAudio/requirements.txt
```

In `getAudio/.env` fill in the keys (if you only use Whisper you don't need any):

```ini
GEMINI_API_KEY=...
DASHSCOPE_API_KEY=...     # Alibaba engine, optional
OPENROUTER_API_KEY=...    # Claude for analysis, optional
```

```bash
bash getAudio/run.sh      # http://localhost:5001
```

It also has a Docker version (`docker compose up -d`). There are build scripts for macOS / Windows desktop apps in `getAudio/packaging`, but no downloadable builds yet.

## For AI agents

It has an MCP server: `pipx install verbatim-transcribe-mcp`. Claude Code and other agents can then call transcription and creator analysis directly.

## Known issues

1. The analysis pipeline will not auto-continue after the server restarts. You need to manually click "Continue". Finished parts are not redone.
2. Search is linear. It will get slow when there are many more files.
3. When Gemini hits content scrutiny it falls back to Whisper, which is slower.
4. Xiaohongshu relies on an independent scraper and isn't fully integrated.

## Notes

Personal project, MIT license. Transcribing and analyzing third-party content is for personal study only. Please respect the platforms' terms and the creators' rights.
