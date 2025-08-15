#!/usr/bin/env python3
"""
Batch-builder: fetches the first **N** *HD* portrait clips from Pexels and turns each into a
≤ 10 s hook-video with a **random trending YouTube Music preview** as soundtrack
(looked up via iTunes Search) and the caption
«KITCHEN-1988 -> DREAM KITCHEN FOR $3,500» (details — see comments 👇).

What changed vs Freesound version
---------------------------------
* ❌ Removed Freesound. No FREESOUND_API_KEY needed.
* ✅ Picks a **random** track from a pool of top/trending YouTube Music items.
* ✅ Audio is saved as **audio-only** (ffmpeg `-vn`) — no «video disguised as .mp3».

Dependencies
------------
`pip install moviepy pillow pilmoji ytmusicapi requests python-dotenv`
Also install ffmpeg: `sudo apt-get install -y ffmpeg` (recommended).

Env vars (mandatory): `PEXELS_API_KEY`.

NEW — self-destruct download server (no extra deps):
----------------------------------------------------
Run:  `python3 script.py --serve --port 8000 [--once]`
Curl: `curl -O http://localhost:8000/dl/hooked_01.mp4`
After a successful GET the file is deleted from the project folder.
"""

from __future__ import annotations
import os, random, shutil, pathlib, typing as _t, requests, numpy as np
from dotenv import load_dotenv
from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip, AudioFileClip
from moviepy.audio.fx.audio_loop import audio_loop
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from ytmusicapi import YTMusic
from urllib.parse import urlparse, unquote
import subprocess

# === NEW: standard-lib HTTP server that deletes served files ===================
import http.server, socketserver, threading
from typing import Optional

ALLOWED_EXTS = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mov", ".mkv", ".aac", ".flac", ".ogg"}
PROJECT_ROOT = os.path.abspath(os.getcwd())
SERVE_PREFIX = os.getenv("SERVE_PREFIX", "/dl/")  # URL prefix for downloads

class SelfDestructHandler(http.server.BaseHTTPRequestHandler):
    """Serves files under /dl/<relpath> and deletes them right after sending."""
    once: bool = False  # if True, the server shuts down after first successful send

    def do_GET(self):
        if not self.path.startswith(SERVE_PREFIX):
            self.send_error(404, "Unknown route")
            return

        rel = unquote(self.path[len(SERVE_PREFIX):]).lstrip("/")
        abs_path = os.path.abspath(os.path.join(PROJECT_ROOT, rel))

        # Security: stay inside project root
        if not abs_path.startswith(PROJECT_ROOT):
            self.send_error(403, "Forbidden")
            return

        ext = os.path.splitext(abs_path)[1].lower()
        if not os.path.exists(abs_path):
            self.send_error(404, "Not found")
            return
        if ext not in ALLOWED_EXTS:
            self.send_error(415, "Unsupported media type")
            return

        # Guess simple content type
        ctype = (
            "video/mp4" if ext == ".mp4" else
            "audio/mpeg" if ext in {".mp3", ".mp2"} else
            "audio/mp4"  if ext in {".m4a", ".aac"} else
            "video/webm" if ext == ".webm" else
            "application/octet-stream"
        )

        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.path.getsize(abs_path)))
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(abs_path)}"')
            self.send_header("X-Deleted", "1")
            self.end_headers()
            with open(abs_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except BrokenPipeError:
            # Client aborted; still try to delete
            pass
        finally:
            try:
                os.remove(abs_path)
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[WARN] Delete failed for {abs_path}: {e}", flush=True)

        if SelfDestructHandler.once:
            # shutdown in a separate thread to avoid deadlock in handler thread
            def _shutdown(server):
                try:
                    server.shutdown()
                except Exception:
                    pass
            threading.Thread(target=_shutdown, args=(self.server,), daemon=True).start()

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] " + (fmt % args), flush=True)

def serve_and_delete(host: str = "0.0.0.0", port: int = 8000, once: bool = False):
    """Start a simple HTTP server that deletes files after download."""
    SelfDestructHandler.once = once
    with socketserver.ThreadingTCPServer((host, port), SelfDestructHandler) as httpd:
        print(f"Serving self-destruct files at http://{host}:{port}{SERVE_PREFIX}<filename>")
        print(f"Root: {PROJECT_ROOT} | Allowed extensions: {', '.join(sorted(ALLOWED_EXTS))}")
        if once:
            print("Mode: one-shot (server will stop after first successful download)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
# ==============================================================================

# ─── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
if not PEXELS_API_KEY:
    raise SystemExit("PEXELS_API_KEY missing → https://www.pexels.com/api/")

SEARCH_QUERY   = "home remodeling"
CAPTION_TEXT   = (
    "KITCHEN-1988 -> DREAM KITCHEN FOR $3,500\n"
    "(details — see comments 👇)"
)
VIDEOS_COUNT   = 3      # how many clips per batch
MAX_DURATION   = 10      # s — hard cap for Shorts/Reels
MIN_WIDTH      = 720     # px — guards against muddy sources
MIN_HEIGHT     = 1280
YT_COUNTRY     = "US"    # charts storefront
POOL_SIZE      = 100     # how many previews to try to collect
CHART_PLAYLISTS = 1      # how many YT chart playlists to combine

FONT_DIR   = "fonts"
OUT_TMPL   = "hooked_{:02d}.mp4"
TMP_VIDEO  = "__tmp_vid__.mp4"
PEXELS_HEADERS = {"Authorization": PEXELS_API_KEY}
ARROW_FALLBACK = "->"
SYSTEM_FONT    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# ─── GLYPH-SANITISER ───────────────────────────────────────────────────────────
_CHAR_MAP = {
    "–": "-", "—": "-", "―": "-", "-": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "…": "...", "•": "*",
    "\u00A0": " ", "\u202F": " ",
    "→": "->",
}

def sanitise(txt: str) -> str:
    return "".join(_CHAR_MAP.get(c, c) for c in txt)

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def dl(url: str, path: str):
    with requests.get(url, stream=True, timeout=60) as r, open(path, "wb") as f:
        r.raise_for_status(); shutil.copyfileobj(r.raw, f)


def pexels_hd_portrait_mp4s(query: str, n: int) -> list[str]:
    collected: list[str] = []
    page = 1
    while len(collected) < n:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers=PEXELS_HEADERS,
            params={"query": query, "orientation": "portrait", "per_page": 80, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        vids = r.json().get("videos", [])
        if not vids:
            break
        for v in vids:
            best: _t.Optional[dict] = None
            for f in v.get("video_files", []):
                if (
                    f.get("file_type") == "video/mp4"
                    and f.get("quality") == "hd"
                    and f.get("width", 0) >= MIN_WIDTH
                    and f.get("height", 0) >= MIN_HEIGHT
                ):
                    if best is None or f["width"] < best["width"]:
                        best = f
            if best:
                collected.append(best["link"])
                if len(collected) == n:
                    break
        page += 1
    if not collected:
        raise RuntimeError("No suitable HD portrait videos found :(")
    return collected


def pick_font(text: str) -> str:
    chars = {c for c in text if not c.isspace()}
    pool = list(pathlib.Path(FONT_DIR).glob("*.[ot]tf"))
    random.shuffle(pool)
    for p in pool:
        f = ImageFont.truetype(str(p), 24)
        if all(f.getmask(c).getbbox() for c in chars):
            return str(p)
    return SYSTEM_FONT

# ─── CAPTION ───────────────────────────────────────────────────────────────────

def make_caption(raw: str, W: int, H: int) -> ImageClip:
    txt = sanitise(raw)
    font_path = pick_font(txt)
    base = int(H * 0.05)
    max_w, max_h = int(W * 0.70), int(H * 0.20)
    for sz in range(base, 10, -2):
        font = ImageFont.truetype(font_path, sz)
        lines, cur = [], ""
        for w in txt.split():
            test = (cur + " " + w).strip()
            if font.getbbox(test)[2] <= max_w:
                cur = test
            else:
                lines.append(cur); cur = w
        lines.append(cur)
        if (
            max(font.getbbox(l)[2] for l in lines) + int(sz * .8) <= max_w
            and sum(font.getbbox(l)[3] for l in lines) + int(sz * .6) <= max_h
        ):
            break
    pad_x, pad_y = int(sz * .4), int(sz * .25)
    box_w = max(font.getbbox(l)[2] for l in lines) + 2 * pad_x
    box_h = sum(font.getbbox(l)[3] for l in lines) + 2 * pad_y
    img = Image.new("RGBA", (box_w, box_h), (0,0,0,0))
    ImageDraw.Draw(img).rounded_rectangle([0,0,box_w,box_h], radius=int(sz * .35), fill="white")
    with Pilmoji(img) as dr:
        y = pad_y
        for ln in lines:
            lw = font.getbbox(ln)[2]
            dr.text(((box_w - lw)//2, y), ln, font=font, fill="black")
            y += font.getbbox(ln)[3]
    return ImageClip(np.array(img))

# ─── YOUTUBE MUSIC → ITUNES PREVIEW POOL ───────────────────────────────────────

def ytmusic_trending_tracks(country: str, chart_playlists: int, per_pl_limit: int) -> list[dict]:
    yt = YTMusic()
    charts = yt.get_charts(country=country)
    pids: list[str] = []
    v = charts.get("videos")
    if isinstance(v, dict) and v.get("playlist"):
        pids.append(v["playlist"])
    t = charts.get("trending")
    if isinstance(t, dict) and t.get("playlist"):
        pids.append(t["playlist"])
    for g in charts.get("genres", []):
        pid = g.get("playlistId")
        if pid and pid not in pids:
            pids.append(pid)
    pids = pids[:max(1, chart_playlists)]
    tracks: list[dict] = []
    for pid in pids:
        wp = yt.get_watch_playlist(playlistId=pid, limit=per_pl_limit)
        for it in wp.get("tracks", []):
            title = it.get("title") or ""
            artists = [a.get("name") for a in (it.get("artists") or []) if a.get("name")]
            if not artists:
                by = (it.get("byline") or "").split("•")[0].strip()
                if by: artists = [by]
            if title:
                tracks.append({"title": title, "artists": artists})
    # de-dup by (title, first artist)
    seen=set(); uniq=[]
    for t in tracks:
        key=(t['title'].lower(), (t['artists'][0].lower() if t['artists'] else ''))
        if key in seen: continue
        seen.add(key); uniq.append(t)
    return uniq


def itunes_preview_for(title: str, artists: list[str], country: str) -> tuple[str, dict] | None:
    term = f"{title} {artists[0]}" if artists else title
    params = {"term": term, "media": "music", "entity": "musicTrack", "limit": 10, "country": country}
    r = requests.get(ITUNES_SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("results", [])
    if not items: return None
    tnorm = title.lower()
    def score(x: dict) -> int:
        s=0; tn=x.get("trackName","" ).lower(); an=x.get("artistName","" ).lower()
        if tnorm in tn: s+=3
        if artists and artists[0].lower() in an: s+=2
        s += int(x.get("trackTimeMillis",0)//10000)
        return s
    items.sort(key=score, reverse=True)
    for it in items:
        url = it.get("previewUrl")
        if url: return url, it
    return None


def _guess_source_ext(url: str, headers: dict) -> str:
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    path = urlparse(url).path.lower()
    if ".mp3" in path or "audio/mpeg" in ctype: return ".mp3"
    if ".m4a" in path or "audio/mp4" in ctype or "audio/x-m4a" in ctype: return ".m4a"
    return ".m4a"


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def download_preview_audio(url: str, meta: dict, idx: int) -> str:
    """Download preview and ensure **audio-only** output. Returns path to audio file."""
    # HEAD to detect container
    try:
        h = requests.head(url, allow_redirects=True, timeout=20); h.raise_for_status()
        source_ext = _guess_source_ext(url, h.headers)
    except Exception:
        source_ext = ".m4a"
    base = f"__dl_{idx:02d}"
    tmp = f"{base}{source_ext}"
    dl(url, tmp)
    out = f"__tmp_aud_{idx:02d}.mp3"
    if _has_ffmpeg():
        subprocess.run(["ffmpeg","-y","-i", tmp, "-vn","-acodec","libmp3lame","-q:a","2", out],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.remove(tmp)
        return out
    else:
        # fallback: just return the original (moviepy can read m4a)
        return tmp


def build_previews_pool(pool_size: int, country: str, chart_playlists: int) -> list[tuple[str,dict]]:
    tracks = ytmusic_trending_tracks(country, chart_playlists, per_pl_limit=pool_size*2)
    previews: list[tuple[str,dict]] = []
    for t in tracks:
        res = itunes_preview_for(t['title'], t['artists'], country)
        if res:
            previews.append(res)
        if len(previews) >= pool_size:
            break
    if not previews:
        raise RuntimeError("No previews found via iTunes Search.")
    return previews

# ─── RENDER ONE CLIP ───────────────────────────────────────────────────────────

def render_clip(src_url: str, idx: int, previews: list[tuple[str,dict]]):
    dl(src_url, TMP_VIDEO)
    clip = VideoFileClip(TMP_VIDEO).without_audio()
    if clip.duration > MAX_DURATION:
        clip = clip.subclip(0, MAX_DURATION)

    label = make_caption(CAPTION_TEXT, clip.w, clip.h).set_duration(clip.duration)
    label = label.set_pos(("center", int(clip.h * 0.05)))

    # pick random preview from pool and make sure it's audio-only
    for _ in range(8):
        url, meta = random.choice(previews)
        try:
            audio_path = download_preview_audio(url, meta, idx)
            audio = AudioFileClip(audio_path)
            if audio.duration < clip.duration:
                audio = audio_loop(audio, duration=clip.duration)
            else:
                audio = audio.subclip(0, clip.duration)
            final = clip.set_audio(audio)
            CompositeVideoClip([final, label]).write_videofile(
                OUT_TMPL.format(idx), codec="libx264", preset="slow", audio_codec="aac",
                fps=clip.fps, threads=os.cpu_count() or 4, temp_audiofile="__temp_aac__.m4a",
                remove_temp=True, ffmpeg_params=["-crf","18"],
            )
            break
        except Exception as e:
            print(f"[{idx}] preview failed, retrying: {e}")
            continue
    else:
        raise RuntimeError("Failed to attach any preview audio.")

    # cleanup
    try:
        os.remove(TMP_VIDEO)
        # remove per-idx tmp audio if present
        for p in (f"__tmp_aud_{idx:02d}.mp3", f"__dl_{idx:02d}.m4a", f"__dl_{idx:02d}.mp3"):
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass

# ─── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    previews = build_previews_pool(POOL_SIZE, YT_COUNTRY, CHART_PLAYLISTS)
    urls = pexels_hd_portrait_mp4s(SEARCH_QUERY, VIDEOS_COUNT)
    for idx, url in enumerate(urls, 1):
        print(f"⇣  ({idx}/{len(urls)})", url.split("/")[-1])
        try:
            render_clip(url, idx, previews)
        except Exception as exc:
            print("⚠️  Skipped:", exc)
    print("✓ All done!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Batch build hooks or serve files with self-destruct.")
    parser.add_argument("--serve", action="store_true",
                        help="Start HTTP server that deletes files after successful download.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"),
                        help="Bind host for --serve (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")),
                        help="Bind port for --serve (default 8000)")
    parser.add_argument("--once", action="store_true",
                        help="Stop the server after the first successful download.")
    args = parser.parse_args()

    if args.serve:
        serve_and_delete(args.host, args.port, args.once)
    else:
        main()
