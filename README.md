# clip-watcher

Clips a Twitch stream **while it's live**, so you post before everyone who waits for the VOD.

1. **streamlink** records the stream the moment it goes live
2. **Twitch chat** is read anonymously (no account, no token)
3. chat messages are counted every 10 s and **spikes** are marked
4. **ffmpeg** cuts 30 s around each spike
5. **whisper** (faster-whisper) writes the captions

## Install (any PC)

**Windows:** unzip the folder anywhere and double-click **`setup.bat`**.
Or skip that step: the first `clip ...` command runs setup automatically.

**Mac / Linux:** in a terminal, inside the folder, run `bash setup.sh`, then use `./clip.sh` wherever this guide says `clip`.

Setup checks for what's needed and **asks before installing** anything that's missing:

| needed | if missing |
|---|---|
| Python 3.10-3.13 | Windows: winget, or a download from python.org (no admin needed). Mac: Homebrew. Linux: apt/dnf/pacman |
| ffmpeg | Windows: winget, or a portable copy downloaded into `tools\ffmpeg`. Mac/Linux: package manager |
| Python libraries | installed into a private `.venv` folder in this tool's folder, pinned to tested versions |

If you answer "no", setup tells you where to download that app yourself. Re-running setup is safe.
`setup.bat -Yes` installs without asking.
The first captioned clip downloads the Whisper speech model (about 500 MB for `small`).

## Run it

```
cd clip-watcher
clip <channel> --vertical --burn
```

Start it before the stream begins. It waits until the channel goes live, records, and clips
as it goes. It stops about 5 minutes after the stream ends (`--forever` keeps waiting for the next one). Ctrl+C stops it and finishes any queued clips.

## Facecam layout (cam on top, gameplay below)

Every streamer puts their camera somewhere different. Mark it once per streamer with the mouse:

```
clip <channel> --pick-facecam
```

A window shows a frame from their stream. Drag a box around the camera and press **Enter**.
A preview of the vertical layout then opens. The box is saved in `facecams.json`, and every
`clip <channel> --vertical` uses it automatically from then on. Run it again if they move their cam.

**You don't have to do it first.** Raw clips are always saved, so you can mark the camera later and re-render:

```
clip <channel> --pick-facecam --frame-from "output\<channel>\<session>\clips\clip_001_....mp4"
clip <channel> --reframe "output\<channel>\<session>\clips"
```

`--frame-from` picks from a saved clip instead of the live stream, so it works after the stream ends.

Other options:
- `--facecam none` for a plain center crop.
- `--facecam top-left` (or another corner) for a rough corner guess.
- `--facecam x,y,w,h` for exact fractions of the frame.
- `--cam-height 0.35` sets how much of the vertical frame the cam takes.

If the streamer moves their cam mid-stream, the raw clip is unaffected. Only the vertical version would be off.

## Output — `output\<channel>\<date_time>\`

| file | what |
|---|---|
| `recording_partN.ts` | the full recording (a new part if the stream drops and comes back) |
| `spikes.csv` | every spike: time, message count, baseline |
| `clips\clip_001_HHMMSS.mp4` | the raw 30 s clip (re-encoded so audio and video start together) |
| `clips\clip_001_HHMMSS.srt` | captions |
| `clips\clip_001_HHMMSS.chat.txt` | what chat said during the clip, to help pick the best ones |
| `clips\clip_001_HHMMSS_final.mp4` | 1080x1920 crop with burned captions (`--vertical --burn`) |

## Tuning

- **Too many clips:** raise `--ratio` (default 2.0) or `--sigma` (3.0). **Too few:** lower them.
- **Clip starts too late or early:** `--lead` (20 s of video kept before chat started reacting).
- **Disk:** a 1080p60 recording is about 3 GB/hour. `--quality 720p60,720p,best` roughly halves that.
- **Captions:** `--model small` is the default. `base` is faster; `medium` is more accurate but slow on CPU.
  Use `--language en` to skip language detection, or `--device cuda` on an NVIDIA GPU with CUDA/cuDNN installed.
- See all options: `clip --help`

## The catch

It only works while the stream is live, because it needs live chat to find the moments.
You can't rewind a stream that already ended.
