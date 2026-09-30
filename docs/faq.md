# FAQ

[中文版](zh-CN/faq.md) · [Getting started](getting-started.md) · [Engines](engines.md) · [Creators](creators.md)

- [The app won't open (Gatekeeper)](#the-app-wont-open-gatekeeper)
- [I opened it but nothing shows up](#i-opened-it-but-nothing-shows-up)
- ["API key missing or invalid — check Settings"](#api-key-missing-or-invalid--check-settings)
- ["Can't reach the model API" or "User location is not supported"](#cant-reach-the-model-api-or-user-location-is-not-supported)
- [A download failed / Bilibili blocks me](#a-download-failed--bilibili-blocks-me)
- [Whisper refuses to run on battery](#whisper-refuses-to-run-on-battery)
- ["Content blocked by the model"](#content-blocked-by-the-model)
- [A local file path doesn't work](#a-local-file-path-doesnt-work)
- [A creator run stopped or failed](#a-creator-run-stopped-or-failed)
- [Where is my data?](#where-is-my-data)
- [How do I uninstall?](#how-do-i-uninstall)
- [Where do I see what I've spent?](#where-do-i-see-what-ive-spent)
- [The Xiaohongshu page doesn't work](#the-xiaohongshu-page-doesnt-work)

## The app won't open (Gatekeeper)

Verbatim isn't signed with an Apple developer certificate, so macOS blocks it the first time.

1. In **Applications**, right-click (or Control-click) **Verbatim** and choose **Open**.
2. In the dialog, click **Open**.

On newer macOS versions the dialog may only offer **Done** or **Move to Trash**. Then:

1. Click **Done**.
2. Open **System Settings → Privacy & Security**.
3. Scroll down. Next to the message about Verbatim being blocked, click **Open Anyway**.
4. Confirm with your password, then click **Open**.

If macOS says the app "is damaged", run this in Terminal once, then open it again:

```bash
xattr -dr com.apple.quarantine /Applications/Verbatim.app
```

The DMG is for Apple Silicon Macs (M1 and later). It does not run on Intel Macs.

## I opened it but nothing shows up

That's normal. Verbatim has no window of its own.

1. Look for **◉ Verbatim** in the menu bar at the top of the screen.
2. Click it and choose **Open in Browser** (**在浏览器中打开** on a Chinese system). The page is `http://localhost:5001`.

If you open the app while it is already running, it just opens the browser again and tells you it is
already running. If it fails to start, a dialog explains why. One cause is another program already
using port 5001.

## "API key missing or invalid — check Settings"

A cloud engine or an analysis step needed a key it didn't have, or the key was rejected.

1. Click **Settings** in the sidebar.
2. Open **Gemini** (or **DashScope** for Qwen-ASR and Precise, **OpenRouter** for Claude analysis).
3. Paste the key into **API key**, click **Test connection**, then **Save**.
4. Run the item again.

What needs which key:

| Feature | Key |
|---|---|
| Whisper transcription | none |
| Gemini, Gemini 3.5 Transcribe, summaries, AI titles, everything on Creators | Gemini |
| Qwen-ASR | DashScope |
| Precise | Gemini and DashScope |
| Claude as the creator analysis model | OpenRouter |

To use Verbatim with no key at all, choose **Whisper**. Summaries and AI titles are then skipped.

## "Can't reach the model API" or "User location is not supported"

Gemini's API is not available in every country or region, and it has to be reachable from your
network.

- If you see "User location is not supported", Google is refusing requests from where your traffic
  exits. Use a network or VPN that exits in a supported region, or put a proxy address in
  **Settings → Gemini → Base URL** (optional; leave blank for the official endpoint).
- "Can't reach the model API — network or proxy" means the request didn't get through. Check your
  connection and any proxy or VPN. If the network drops during a batch, Verbatim pauses cloud jobs and
  retries them once it is back.
- "Rate-limited / out of quota — retry later" means Google's limit was hit. Wait and run it again.

## A download failed / Bilibili blocks me

Links are downloaded with yt-dlp. Common causes:

- **Bilibili HTTP 412.** Bilibili rate-limits by IP address, especially for channel (space) pages.
  Verbatim already retries with a pause. If it still fails:
  - Wait a while before trying again. Retrying right away from the same IP makes it worse.
  - When running from source, sign in to bilibili.com in Google Chrome: Verbatim borrows Chrome's
    login cookies when downloading (macOS may ask to allow "Chrome Safe Storage"; allow it). The app
    doesn't borrow browser cookies by default — see below.
  - For a channel, use the plain space link `https://space.bilibili.com/<uid>` rather than
    `…/<uid>/video`.
  - Switching to another network or VPN exit usually clears it.
- **YouTube "Sign in to confirm you're not a bot".** From source, sign in to YouTube in Chrome and
  try again. In the app, try again later or from another network.
- **Browser cookies.** From source, downloads borrow Chrome's cookies (skipped automatically if
  Chrome isn't installed); set `YTDLP_COOKIES_BROWSER` in `getAudio/.env` to another browser
  (`firefox`, `edge`, `brave`…) or to an empty value. The app doesn't borrow any browser's cookies
  unless `YTDLP_COOKIES_BROWSER` is set, so it works without Chrome and never asks for keychain access.
- **"Link not recognised".** Paste the full `https://` address of a video, playlist, or channel.
- **The site changed.** yt-dlp needs updates when sites change. From source, run
  `brew upgrade yt-dlp`. The app bundles its own yt-dlp, so a new app release is needed.

From source you also have `YTDLP_PROXY` (a proxy to retry through when Bilibili returns 412) and
`BILI_SESSDATA` (your Bilibili login cookie, used to read channel lists when yt-dlp is blocked). Put
them in `getAudio/.env`.

## Whisper refuses to run on battery

On purpose. Local Whisper uses every performance core for as long as the job runs. On battery that
can drain a laptop until it shuts down in the middle of a job, which has happened.

- Plug in the Mac and run it again, or
- choose a cloud engine (for example **Gemini 3.5 Transcribe**) for this item.

The same check applies when a cloud engine fails and Verbatim falls back to Whisper.

## "Content blocked by the model"

Gemini sometimes refuses material (for example copyrighted lyrics or sensitive topics). When that
happens, Verbatim redoes that file with local Whisper automatically. If Whisper can't run (on battery),
the item fails. Plug in and run it again, or pick Whisper yourself.

## A local file path doesn't work

macOS doesn't let apps read the **Downloads** and **Desktop** folders by path. Move the file to
**Documents** or another folder, or use **Choose or drop audio / video files** to upload it instead.

## A creator run stopped or failed

Runs don't resume by themselves after the app restarts. They are marked failed and listed under
**Unfinished or failed**.

1. Open the creator.
2. Open **Run details & actions**.
3. Click **Continue**. Finished transcripts and quotes are reused; only what is missing is redone.

If several episodes failed because of your Gemini quota, wait for the quota to reset before
continuing. The **Whisper fallback** option (Advanced settings) makes failed cloud episodes fall back
to local Whisper instead.

## Where is my data?

| How you run it | Data folder |
|---|---|
| Mac app | `~/Library/Application Support/Verbatim` |
| From source | the `getAudio/` folder itself |

Inside:

- `results/`: one folder per transcript (text, summary, metadata).
- `results/_chains/`: creator analyses and collections.
- `uploads/`: downloaded and uploaded media.
- `tasks.db`: the job list. `usage.db`: the cost log.
- `settings.local.json`: your keys and settings.

To open the Mac folder: in Finder, **Go → Go to Folder…** and paste the path.

New transcripts don't keep the audio unless you turn on **Settings → Storage → Keep audio for
playback**. The same pane shows how much space audio takes and can delete it.

If Google Drive for desktop is installed and signed in, transcripts, summaries, and analysis
documents are also copied to `Verbatim_备份` in My Drive (never audio). **Settings → Storage** shows the
backup status and lets you change the folder. The backup only adds files; it never deletes.

Don't delete or rearrange files in the data folder while Verbatim is running.

## How do I uninstall?

1. Click **◉ Verbatim** in the menu bar and choose **Quit Verbatim** (**退出 Verbatim** on a Chinese system).
2. Drag **Verbatim** from **Applications** to the Trash.
3. To remove your data too, delete `~/Library/Application Support/Verbatim`. This deletes every
   transcript and analysis. Copy anything you want to keep first.
4. Whisper models downloaded on first use are stored outside the app, usually in
   `~/.cache/huggingface`. Delete the Whisper model folders there to free the space.
5. The Google Drive backup folder (`Verbatim_备份`) stays until you delete it.

## Where do I see what I've spent?

- **Settings → Costs**: every model call, this month and all time, by provider, by purpose, and by
  model, with token counts.
- A transcript page shows the cost of that transcript in its top line.
- A creator page shows the model cost of that analysis.

Some models have no built-in price (Gemini 3.5 Transcribe, Alibaba ASR). Their calls are counted and
shown as unpriced. From source you can add prices in `prices.json` next to `tasks.db`. For the exact
charge, check your Google, Alibaba, or OpenRouter billing page.

## The Xiaohongshu page doesn't work

The **Xiaohongshu** page needs a separate scraper project that drives a logged-in browser. It is not
included in the Mac app. It only works when running from source with that project set up
(`XHS_PROJECT` environment variable and `uv`).
