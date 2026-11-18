#!/usr/bin/env python3
"""
clip-watcher - clip a Twitch stream while it is still live.

  1. streamlink records the stream the moment it goes live
  2. Twitch chat is read anonymously (no account, no token)
  3. chat messages are counted every N seconds and spikes are marked
  4. ffmpeg cuts a clip around each spike
  5. faster-whisper writes the captions (.srt, optionally burned in)

Usage:  python clip_watcher.py <channel> [options]      (see --help)
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import os
import queue
import random
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def find_ffmpeg() -> str:
    here = Path(__file__).parent
    for exe in (here / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",  # portable copy from setup.ps1
                shutil.which("ffmpeg"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"):
        if exe and Path(exe).is_file():
            return str(exe)
    sys.exit("ffmpeg not found. Run setup again (setup.bat on Windows, ./setup.sh on Mac/Linux).")


def open_file(path: Path) -> None:
    """Open an image with the system viewer (best effort, all platforms)."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --------------------------------------------------------------------------- 1. record
@dataclass
class Part:
    """One continuous recording file. Clip offsets into it are measured from t0."""
    index: int
    path: Path
    t0: float = 0.0       # wall-clock time the first bytes landed on disk
    done: bool = False


def is_live(channel: str) -> bool:
    from streamlink import Streamlink
    try:
        return bool(Streamlink().streams(f"https://www.twitch.tv/{channel}"))
    except Exception as e:  # network hiccup, plugin error, ...
        log(f"live check failed ({e}) - treating as offline")
        return False


def start_recording(channel: str, quality: str, part: Part) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "streamlink", "--loglevel", "warning", "--force",
           "-o", str(part.path), f"https://www.twitch.tv/{channel}", quality]
    proc = subprocess.Popen(cmd)
    # offset 0 in the file == the moment the first data arrives
    while proc.poll() is None and not (part.path.exists() and part.path.stat().st_size > 0):
        time.sleep(0.2)
    part.t0 = time.time()
    return proc


# --------------------------------------------------------------------------- 2. chat
class ChatReader(threading.Thread):
    """Anonymous Twitch IRC reader: 'justinfanNNNNN' needs no account or token."""
    HOST, PORT = "irc.chat.twitch.tv", 6667
    KEEP_SECONDS = 600  # how long messages are kept for the per-clip chat logs

    def __init__(self, channel: str, stop: threading.Event):
        super().__init__(daemon=True)
        self.channel, self.stop = channel, stop
        self.lock = threading.Lock()
        self.count = 0
        self.recent: collections.deque[tuple[float, str, str]] = collections.deque()
        self.joined = False

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self._session()
            except OSError as e:
                self.joined = False
                log(f"chat disconnected ({e}) - reconnecting in 5s")
                self.stop.wait(5)

    def _session(self) -> None:
        nick = f"justinfan{random.randint(10000, 99999)}"
        with socket.create_connection((self.HOST, self.PORT), timeout=30) as sock:
            sock.settimeout(400)  # Twitch PINGs roughly every 5 minutes
            sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            buf = b""
            while not self.stop.is_set():
                data = sock.recv(65536)
                if not data:
                    raise ConnectionError("server closed the connection")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    self._handle(sock, raw.decode("utf-8", "replace"))

    def _handle(self, sock: socket.socket, line: str) -> None:
        if line.startswith("PING"):
            sock.sendall(("PONG" + line[4:] + "\r\n").encode())
        elif " PRIVMSG #" in line:
            prefix, _, rest = line.partition(" PRIVMSG #")
            user = prefix.lstrip(":").split("!", 1)[0]
            text = rest.partition(" :")[2]
            with self.lock:
                self.count += 1
                self.recent.append((time.time(), user, text))
        elif " 366 " in line and not self.joined:  # end of NAMES list = join confirmed
            self.joined = True
            log(f"chat connected to #{self.channel} (anonymous, no account)")

    def take_window(self) -> int:
        """Return the number of messages since the last call and reset the counter."""
        with self.lock:
            n, self.count = self.count, 0
            cutoff = time.time() - self.KEEP_SECONDS
            while self.recent and self.recent[0][0] < cutoff:
                self.recent.popleft()
        return n

    def between(self, a: float, b: float) -> list[tuple[float, str, str]]:
        with self.lock:
            return [m for m in self.recent if a <= m[0] <= b]


# --------------------------------------------------------------------------- 3. spikes
class SpikeDetector:
    """A window is a spike when it clears all three bars:
       >= min_msgs, >= ratio x baseline mean, and >= mean + sigma x stdev."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.history: collections.deque[int] = collections.deque(maxlen=args.baseline)
        self.last_spike = float("-inf")

    def feed(self, count: int, now: float) -> tuple[bool, float, float | None]:
        a, h = self.args, self.history
        base = statistics.fmean(h) if h else 0.0
        threshold = None
        spike = False
        if len(h) >= a.warmup:
            sd = statistics.pstdev(h)
            threshold = max(a.min_msgs, base * a.ratio, base + a.sigma * max(sd, 1.0))
            spike = count >= threshold and now - self.last_spike >= a.cooldown
        if spike:
            self.last_spike = now
        h.append(count)
        return spike, base, threshold


# --------------------------------------------------------------------------- 4. cut
@dataclass
class ClipJob:
    n: int
    part: Part
    start: float   # wall-clock
    end: float     # wall-clock
    count: int
    base: float


class Cutter(threading.Thread):
    def __init__(self, ffmpeg: str, clips_dir: Path, chat: ChatReader, captioner: "Captioner"):
        super().__init__(daemon=True)
        self.ffmpeg, self.clips_dir, self.chat, self.captioner = ffmpeg, clips_dir, chat, captioner
        self.q: queue.Queue[ClipJob | None] = queue.Queue()
        self.csv_path = clips_dir.parent / "spikes.csv"
        if not self.csv_path.exists():
            self.csv_path.write_text("clip,time,messages_in_window,baseline,recording,offset_s\n", encoding="utf-8")

    def run(self) -> None:
        while (job := self.q.get()) is not None:
            # wait until the recording actually contains the end of the clip
            while not job.part.done and time.time() < job.end + 5:
                time.sleep(0.5)
            try:
                self.cut(job)
            except Exception as e:
                log(f"clip {job.n}: cut failed ({e})")

    def cut(self, job: ClipJob) -> None:
        ss = max(0.0, job.start - job.part.t0)
        dur = job.end - job.start
        stamp = dt.datetime.fromtimestamp(job.end).strftime("%H%M%S")
        mp4 = self.clips_dir / f"clip_{job.n:03d}_{stamp}.mp4"
        # A/V sync: video can only be decoded from a keyframe (~every 2 s on Twitch), and seeking
        # in a .ts lands imprecisely - start exactly at `ss` and the video begins up to ~2 s after
        # the audio (most players and uploads then play the audio early). So seek a few seconds
        # early (fast, lands before a keyframe), then trim the rest exactly while re-encoding,
        # so audio and video both start at 0.
        pre = min(5.0, ss)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{ss - pre:.3f}", "-i", str(job.part.path),
               "-ss", f"{pre:.3f}", "-t", f"{dur:.3f}",
               "-map", "0:v:0?", "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not mp4.exists():
            log(f"clip {job.n}: ffmpeg failed: {r.stderr.strip()[-300:]}")
            return

        msgs = self.chat.between(job.start, job.end)
        with open(mp4.with_suffix(".chat.txt"), "w", encoding="utf-8") as f:
            for t, user, text in msgs:
                f.write(f"+{t - job.start:5.1f}s  {user}: {text}\n")
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([mp4.name, dt.datetime.fromtimestamp(job.end).isoformat(timespec="seconds"),
                                    job.count, f"{job.base:.1f}", job.part.path.name, f"{ss:.1f}"])
        log(f"clip {job.n}: saved {mp4.name} ({dur:.0f}s, {len(msgs)} chat msgs)")
        self.captioner.q.put(mp4)

    def finish(self) -> None:
        self.q.put(None)
        self.join()


# --------------------------------------------------------------------------- 5. captions
def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


class Captioner(threading.Thread):
    def __init__(self, args: argparse.Namespace, ffmpeg: str):
        super().__init__(daemon=True)
        self.args, self.ffmpeg = args, ffmpeg
        self.q: queue.Queue[Path | None] = queue.Queue()
        self.model = None
        self.broken = False

    def run(self) -> None:
        while (mp4 := self.q.get()) is not None:
            try:
                srt = self.caption(mp4) if not self.args.no_captions else None
                if self.args.vertical or (self.args.burn and srt):
                    self.render(mp4, srt)
            except Exception as e:
                log(f"{mp4.name}: captions failed ({e})")

    def load(self):
        if self.model is None and not self.broken:
            log(f"loading whisper model '{self.args.model}' (first time downloads it)...")
            try:
                from faster_whisper import WhisperModel
                compute = "float16" if self.args.device == "cuda" else "int8"
                self.model = WhisperModel(self.args.model, device=self.args.device, compute_type=compute)
            except Exception as e:
                self.broken = True
                log(f"whisper unavailable ({e}) - clips will be saved without captions")
        return self.model

    def caption(self, mp4: Path) -> Path | None:
        model = self.load()
        if model is None:
            return None
        # decode with ffmpeg (16 kHz mono float) instead of faster-whisper's PyAV decoder,
        # which breaks whenever PyAV changes its API
        import numpy as np
        pcm = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(mp4),
                              "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        if audio.size == 0:
            log(f"{mp4.name}: no audio track, no captions")
            return None
        segments, _ = model.transcribe(audio, language=self.args.language,
                                       word_timestamps=True, vad_filter=True)
        words = [w for s in segments for w in (s.words or [])]
        if not words:
            log(f"{mp4.name}: no speech found, no captions")
            return None
        # short, punchy cues (a few words each) - the short-form caption style
        cues, cur = [], []
        for w in words:
            cur.append(w)
            if len(cur) >= self.args.words_per_caption or w.word.rstrip().endswith((".", "?", "!", ",")):
                cues.append(cur)
                cur = []
        if cur:
            cues.append(cur)
        srt = mp4.with_suffix(".srt")
        with open(srt, "w", encoding="utf-8") as f:
            for i, cue in enumerate(cues, 1):
                text = "".join(w.word for w in cue).strip()
                f.write(f"{i}\n{srt_time(cue[0].start)} --> {srt_time(cue[-1].end)}\n{text}\n\n")
        log(f"{mp4.name}: captions written ({len(cues)} cues)")
        return srt

    def render(self, mp4: Path, srt: Path | None) -> None:
        extra = []
        if self.args.burn and srt:
            size, margin = (16, 70) if self.args.vertical else (16, 20)
            style = (f"FontName=Arial,FontSize={size},Bold=1,PrimaryColour=&H00FFFFFF,"
                     f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV={margin}")
            extra.append(f"subtitles={srt.name}:force_style='{style}'")
        out = mp4.with_name(mp4.stem + "_final.mp4")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", mp4.name,
               "-filter_complex", video_graph(self.args, extra), "-map", "[v]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-c:a", "copy", "-movflags", "+faststart", out.name]
        # run inside the clips folder so the subtitles filter gets a plain relative path
        r = subprocess.run(cmd, cwd=mp4.parent, capture_output=True, text=True)
        if r.returncode == 0:
            log(f"{mp4.name}: rendered {out.name}")
        else:
            log(f"{mp4.name}: render failed: {r.stderr.strip()[-300:]}")

    def finish(self) -> None:
        self.q.put(None)
        self.join()


# --------------------------------------------------------------------------- layout
FACECAM_PRESETS = {  # x, y, w, h as fractions of the frame (a typical ~25% corner cam)
    "top-left": (0.0, 0.0, 0.25, 0.25), "top-right": (0.75, 0.0, 0.25, 0.25),
    "bottom-left": (0.0, 0.75, 0.25, 0.25), "bottom-right": (0.75, 0.75, 0.25, 0.25),
}


FACECAMS_FILE = Path(__file__).parent / "facecams.json"


def load_facecams() -> dict:
    try:
        return json.loads(FACECAMS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_facecam(channel: str, box: tuple[float, float, float, float]) -> None:
    cams = load_facecams()
    cams[channel] = [round(v, 4) for v in box]
    FACECAMS_FILE.write_text(json.dumps(cams, indent=2), encoding="utf-8")


def parse_facecam(value: str) -> tuple[float, ...]:
    if value.lower() == "none":
        return ()  # explicitly no facecam -> plain center crop
    if value.lower() in FACECAM_PRESETS:
        return FACECAM_PRESETS[value.lower()]
    try:
        x, y, w, h = (float(v) for v in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"use a corner ({', '.join(FACECAM_PRESETS)}) or x,y,w,h fractions like 0,0.49,0.27,0.30")
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-9 and 0 < h <= 1 - y + 1e-9):
        raise argparse.ArgumentTypeError("x,y,w,h must be fractions of the frame (0-1) and stay inside it")
    return x, y, w, h


def video_graph(args: argparse.Namespace, extra: list[str]) -> str:
    """ffmpeg filter_complex taking [0:v] and producing [v].
       vertical + facecam: facecam stacked on top, gameplay center-cropped below (1080x1920)."""
    tail = "".join("," + f for f in extra)
    if args.vertical and args.facecam:
        x, y, w, h = args.facecam
        top = int(1920 * args.cam_height) // 2 * 2
        bot = 1920 - top
        return (f"[0:v]split[a][b];"
                f"[a]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
                f"scale=1080:{top}:force_original_aspect_ratio=increase,crop=1080:{top}[cam];"
                f"[b]scale=1080:{bot}:force_original_aspect_ratio=increase,crop=1080:{bot}[game];"
                f"[cam][game]vstack,setsar=1{tail}[v]")
    if args.vertical:
        return f"[0:v]crop=trunc(ih*9/16/2)*2:ih,scale=1080:1920,setsar=1{tail}[v]"
    return f"[0:v]{','.join(extra) or 'null'}[v]"


def grab_frame(args: argparse.Namespace, ffmpeg: str) -> Path:
    """One frame from --frame-from (a clip/recording) or else from the live stream."""
    out_dir = Path(args.out) / args.channel
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = out_dir / "snapshot.png"
    frame.unlink(missing_ok=True)
    if args.frame_from:
        src = Path(args.frame_from)
        if not src.is_file():
            sys.exit(f"not a file: {src}")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "3", "-i", str(src),
                        "-frames:v", "1", str(frame)])
    else:
        if not is_live(args.channel):
            sys.exit(f"{args.channel} is offline. Use --frame-from <a clip or recording> to pick from a saved video.")
        log("grabbing a frame from the live stream...")
        sl = subprocess.Popen([sys.executable, "-m", "streamlink", "--loglevel", "error", "--stdout",
                               f"https://www.twitch.tv/{args.channel}", "best"], stdout=subprocess.PIPE)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                        "-frames:v", "1", str(frame)], stdin=sl.stdout)
        sl.stdout.close()
        sl.terminate()
    if not frame.exists():
        sys.exit("could not grab a frame")
    return frame


def layout_preview(args: argparse.Namespace, ffmpeg: str, frame: Path) -> Path:
    args.vertical = True
    layout = frame.with_name("snapshot_layout.png")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame),
                    "-filter_complex", video_graph(args, []), "-map", "[v]", str(layout)], check=True)
    return layout


def pick_facecam(args: argparse.Namespace, ffmpeg: str) -> None:
    """Show a frame, let the user drag a box around the facecam, save it for this channel."""
    frame = grab_frame(args, ffmpeg)
    try:
        import cv2
    except ImportError:
        sys.exit("the picker needs OpenCV: .venv\\Scripts\\python -m pip install opencv-python")
    img = cv2.imread(str(frame))
    h, w = img.shape[:2]
    scale = min(1.0, 1280 / w)  # fit on screen
    disp = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img
    dh, dw = disp.shape[:2]
    win = "Drag a box around the facecam, then press ENTER  (C = cancel)"
    print("\n  A window opened: drag a box around the streamer's camera, then press ENTER.\n"
          "  (Drag again to redo it. Press C to cancel.)\n", flush=True)
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)
    x, y, bw, bh = cv2.selectROI(win, disp, showCrosshair=False, fromCenter=False)
    cv2.destroyAllWindows()
    if bw < 5 or bh < 5:
        sys.exit("cancelled - nothing saved")
    box = (x / dw, y / dh, bw / dw, bh / dh)
    save_facecam(args.channel, box)
    log(f"saved facecam for {args.channel}: {','.join(f'{v:.4f}' for v in box)}  (in {FACECAMS_FILE.name})")
    args.facecam = box
    layout = layout_preview(args, ffmpeg, frame)
    log(f"preview of the vertical layout: {layout}")
    open_file(layout)
    print("  If the preview looks wrong, run --pick-facecam again.", flush=True)


def reframe(args: argparse.Namespace, ffmpeg: str) -> None:
    """Re-render vertical versions of clips that were already saved (e.g. after picking the facecam)."""
    target = Path(args.reframe)
    clips = [target] if target.is_file() else sorted(
        p for p in target.glob("*.mp4") if not p.stem.endswith("_final"))
    if not clips:
        sys.exit(f"no clips found in {target}")
    args.vertical = True
    args.burn = not args.no_captions
    cap = Captioner(args, ffmpeg)
    for mp4 in clips:
        srt = mp4.with_suffix(".srt")
        cap.render(mp4, srt if srt.exists() else None)
    log(f"reframed {len(clips)} clip(s)")


def snapshot(args: argparse.Namespace, ffmpeg: str) -> None:
    """Grab one frame, save it with a 10% grid (+ the facecam box), and a layout preview."""
    frame = grab_frame(args, ffmpeg)
    out_dir = frame.parent
    grid = out_dir / "snapshot_grid.png"
    vf = "drawgrid=w=iw/10:h=ih/10:t=2:c=yellow@0.8"
    if args.facecam:
        x, y, w, h = args.facecam
        vf += f",drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:c=red:t=6"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame),
                    "-vf", vf, str(grid)], check=True)
    log(f"saved {grid}  (yellow lines every 10% - read the facecam's x,y,w,h off it)")
    shown = grid
    if args.facecam:
        layout = layout_preview(args, ffmpeg, frame)
        log(f"saved {layout}  (what the vertical clips will look like)")
        shown = layout
    open_file(shown)


# --------------------------------------------------------------------------- main
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record a Twitch stream live, clip chat spikes, caption them.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("channel", help="Twitch channel name or URL")
    p.add_argument("--out", default=str(Path(__file__).parent / "output"), help="output folder")
    p.add_argument("--quality", default="best", help="streamlink quality, e.g. '720p60,720p,best'")
    p.add_argument("--poll", type=float, default=30, help="seconds between 'is it live yet?' checks")
    p.add_argument("--end-grace", type=float, default=300,
                   help="after the stream drops, keep checking this long in case it comes back")
    p.add_argument("--forever", action="store_true", help="after the stream ends, wait for the next one")
    p.add_argument("--duration", type=float, default=0, help="stop after this many minutes (0 = no limit)")
    g = p.add_argument_group("spike detection")
    g.add_argument("--window", type=float, default=10, help="seconds per chat counting window")
    g.add_argument("--baseline", type=int, default=30, help="windows used for the baseline (30 x 10s = 5 min)")
    g.add_argument("--warmup", type=int, default=6, help="windows to observe before any spike can fire")
    g.add_argument("--ratio", type=float, default=2.0, help="spike must be >= ratio x baseline")
    g.add_argument("--sigma", type=float, default=3.0, help="spike must be >= baseline + sigma x stdev")
    g.add_argument("--min-msgs", type=int, default=5, help="spike must have at least this many messages")
    g.add_argument("--cooldown", type=float, default=30, help="min seconds between two spikes")
    c = p.add_argument_group("clips")
    c.add_argument("--clip-length", type=float, default=30, help="clip length in seconds")
    c.add_argument("--lead", type=float, default=20,
                   help="seconds of video kept before chat started spiking (chat reacts late)")
    c.add_argument("--vertical", action="store_true", help="also render a 1080x1920 version")
    f = p.add_argument_group("facecam (vertical layout: cam on top, game below)")
    f.add_argument("--pick-facecam", action="store_true",
                   help="drag a box around the streamer's camera with the mouse; saved for this channel")
    f.add_argument("--frame-from", metavar="VIDEO",
                   help="with --pick-facecam/--snapshot: use a saved clip or recording instead of the live stream")
    f.add_argument("--reframe", metavar="CLIPS",
                   help="re-render vertical versions of already-saved clips (a clips folder or one .mp4), then exit")
    f.add_argument("--facecam", type=parse_facecam, default=None,
                   help="override the saved facecam: none, top-left/top-right/bottom-left/bottom-right, or x,y,w,h")
    f.add_argument("--cam-height", type=float, default=0.35,
                   help="share of the vertical frame the facecam takes")
    f.add_argument("--snapshot", action="store_true",
                   help="grab one frame with a 10%% grid (and a layout preview if a facecam is set), then exit")
    w = p.add_argument_group("captions")
    w.add_argument("--no-captions", action="store_true", help="skip whisper")
    w.add_argument("--burn", action="store_true", help="burn the captions into a *_final.mp4")
    w.add_argument("--model", default="small", help="whisper model: tiny, base, small, medium, large-v3")
    w.add_argument("--language", default=None, help="force a language code, e.g. en (default: auto)")
    w.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    w.add_argument("--words-per-caption", type=int, default=4)
    args = p.parse_args()
    ch = args.channel.strip().lower()
    if "twitch.tv/" in ch:
        ch = ch.split("twitch.tv/", 1)[1]
    args.channel = ch.strip("/#").split("/")[0].split("?")[0]
    return args


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args()
    ffmpeg = find_ffmpeg()
    if args.pick_facecam:
        pick_facecam(args, ffmpeg)
        return
    if args.facecam is None:  # not given on the command line -> use the one saved for this channel
        saved = load_facecams().get(args.channel)
        args.facecam = tuple(saved) if saved else None
        if saved:
            log(f"using saved facecam for {args.channel}")
    if args.snapshot:
        snapshot(args, ffmpeg)
        return
    if args.reframe:
        reframe(args, ffmpeg)
        return
    if args.vertical and not args.facecam:
        log(f"no facecam saved for {args.channel} - vertical clips will be a center crop. "
            f"Run 'clip {args.channel} --pick-facecam' to set it (you can --reframe the clips later).")
    ch = args.channel
    session = Path(args.out) / ch / dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    clips_dir = session / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    log(f"clip-watcher for twitch.tv/{ch} -> {session}")

    stop = threading.Event()
    chat = ChatReader(ch, stop)
    chat.start()
    captioner = Captioner(args, ffmpeg)
    captioner.start()
    cutter = Cutter(ffmpeg, clips_dir, chat, captioner)
    cutter.start()
    detector = SpikeDetector(args)

    deadline = time.time() + args.duration * 60 if args.duration else float("inf")
    proc: subprocess.Popen | None = None
    part: Part | None = None
    n_parts = n_clips = 0
    try:
        while time.time() < deadline:
            if not is_live(ch):
                log(f"{ch} is offline - checking every {args.poll:.0f}s")
                while time.time() < deadline and not is_live(ch):
                    time.sleep(args.poll)
                if time.time() >= deadline:
                    break

            n_parts += 1
            part = Part(n_parts, session / f"recording_part{n_parts}.ts")
            proc = start_recording(ch, args.quality, part)
            log(f"LIVE - recording to {part.path.name}")
            chat.take_window()  # discard anything counted before recording began

            next_tick = time.time() + args.window
            while proc.poll() is None and time.time() < deadline:
                time.sleep(max(0.0, next_tick - time.time()))
                now = time.time()
                next_tick += args.window
                count = chat.take_window()
                spike, base, thr = detector.feed(count, now)
                bar = "#" * min(50, round(count / max(base, 1.0) * 5))
                status = "warming up" if thr is None else f"spike at >= {thr:.0f}"
                print(f"   chat {count:5d}/{args.window:.0f}s  baseline {base:7.1f}  "
                      f"({status})  {bar}{'  <<< SPIKE' if spike else ''}", flush=True)
                if spike:
                    n_clips += 1
                    start = (now - args.window) - args.lead
                    cutter.q.put(ClipJob(n_clips, part, start, start + args.clip_length, count, base))
                    log(f"SPIKE #{n_clips}: {count} msgs vs baseline {base:.1f} - clip queued")

            if proc.poll() is None:  # hit --duration
                proc.terminate()
                proc.wait()
            part.done = True
            if time.time() >= deadline:
                break
            log("recording stopped - stream ended or dropped")
            back = False
            grace_end = time.time() + args.end_grace
            while time.time() < grace_end:
                if is_live(ch):
                    back = True
                    break
                time.sleep(args.poll)
            if not back and not args.forever:
                break
            if back:
                log("stream is back - starting a new recording part")
    except KeyboardInterrupt:
        log("Ctrl+C - stopping (press again to force quit)")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait()
        if part:
            part.done = True
        stop.set()
        log("finishing queued clips and captions...")
        cutter.finish()
        captioner.finish()
        log(f"done: {n_clips} clip(s) in {clips_dir}")


if __name__ == "__main__":
    main()
