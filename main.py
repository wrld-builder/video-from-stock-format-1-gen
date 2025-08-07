#!/usr/bin/env python3
"""
Batch‑builder: fetches the first **N** *HD* portrait clips from Pexels and turns each into a
≤ 10 s hook‑video with a unique trending CC‑0 soundtrack and the caption
«KITCHEN‑1988 -> DREAM KITCHEN FOR $3,500» (details — see comments 👇).

Guard‑rails
-----------
* **HD‑only input** – width ≥ 720 px, height ≥ 1280 px, quality tag == "hd" (avoids blurry footage).
* **No down‑scaling** – original resolution/FPS preserved.
* **x264 CRF 18 + preset slow** – visually loss‑less for socials.
* Each output trimmed to **≤ 10 s** (so it fits TikTok / YT‑Shorts).

Env vars (mandatory): `PEXELS_API_KEY`, `FREESOUND_API_KEY`.
"""

from __future__ import annotations
import os, math, random, shutil, pathlib, datetime as _dt, typing as _t, requests, numpy as np
from dotenv import load_dotenv
from moviepy.editor import (
    VideoFileClip,
    ImageClip,
    CompositeVideoClip,
    AudioFileClip,
)
from moviepy.audio.fx.audio_loop import audio_loop
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji

# ─── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()
PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY")
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")
if not PEXELS_API_KEY:
    raise SystemExit("PEXELS_API_KEY missing → https://www.pexels.com/api/")
if not FREESOUND_API_KEY:
    raise SystemExit("FREESOUND_API_KEY missing → https://freesound.org/docs/api/")

SEARCH_QUERY  = "home remodeling"
CAPTION_TEXT  = (
    "KITCHEN‑1988 -> DREAM KITCHEN FOR $3,500\n"
    "(details — see comments 👇)"
)
VIDEOS_COUNT  = 10           # how many clips per batch
TREND_DAYS    = 14           # freshness window for viral sounds
MAX_DURATION  = 10           # s — hard cap for Shorts/Reels
MIN_WIDTH     = 720          # px — guards against muddy sources
MIN_HEIGHT    = 1280

FONT_DIR   = "fonts"
OUT_TMPL   = "hooked_{:02d}.mp4"
TMP_VIDEO  = "__tmp_vid__.mp4"
TMP_AUDIO  = "__tmp_aud__.mp3"

PEXELS_HEADERS = {"Authorization": PEXELS_API_KEY}
ARROW_FALLBACK = "->"  # guaranteed ASCII
SYSTEM_FONT    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ─── GLYPH‑SANITISER ───────────────────────────────────────────────────────────
#  Converts “fancy” Unicode characters to plain‑ASCII so *any* font works.
_CHAR_MAP = {
    "–": "-", "—": "-", "―": "-", "‑": "-",          # dashes & nb‑hyphen
    "‘": "'", "’": "'", "‚": "'", "‛": "'",            # single quotes
    "“": '"', "”": '"', "„": '"',                         # double quotes
    "…": "...", "•": "*",                                  # ellipsis & bullet
    " ": " ", " ": " ",                                   # nbsp & narrow nbsp
    "→": "->",                                               # arrow to ASCII
}

def sanitise(txt: str) -> str:
    """Replace problem glyphs with safe ASCII equivalents."""
    return "".join(_CHAR_MAP.get(c, c) for c in txt)

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def dl(url: str, path: str):
    """Stream *url* → *path*."""
    with requests.get(url, stream=True, timeout=60) as r, open(path, "wb") as f:
        r.raise_for_status(); shutil.copyfileobj(r.raw, f)


def pexels_hd_portrait_mp4s(query: str, n: int) -> list[str]:
    """Return up to *n* portrait MP4 URLs that are HD and tall enough."""
    collected: list[str] = []
    page = 1
    while len(collected) < n:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers=PEXELS_HEADERS,
            params={
                "query": query,
                "orientation": "portrait",
                "per_page": 80,
                "page": page,
            },
            timeout=30,
        )
        r.raise_for_status()
        vids = r.json().get("videos", [])
        if not vids:
            break  # no more pages
        for v in vids:
            # pick the *best* matching file inside the video bundle
            best: _t.Optional[dict] = None
            for f in v.get("video_files", []):
                if (
                    f.get("file_type") == "video/mp4"
                    and f.get("quality") == "hd"
                    and f.get("width", 0) >= MIN_WIDTH
                    and f.get("height", 0) >= MIN_HEIGHT
                ):
                    if best is None or f["width"] < best["width"]:  # pick the smaller HD to save bandwidth
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
    """Choose a font that supports **all** glyphs in *text*. Fallback to DejaVuSans."""
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
    base = int(H * 0.05)            # start at ≈5 % height
    max_w, max_h = int(W * 0.70), int(H * 0.20)

    for sz in range(base, 10, -2):
        font = ImageFont.truetype(font_path, sz)
        # manual word‑wrap
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

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [0, 0, box_w, box_h], radius=int(sz * .35), fill="white"
    )

    with Pilmoji(img) as dr:
        y = pad_y
        for ln in lines:
            lw = font.getbbox(ln)[2]
            dr.text(((box_w - lw) // 2, y), ln, font=font, fill="black")
            y += font.getbbox(ln)[3]
    return ImageClip(np.array(img))

# ─── VIRAL CC‑0 MUSIC ───────────────────────────────────────────────────────────

def viral_music_url(min_dur: float, rank: int) -> str:
    """Return URL of the *rank*‑th most‑downloaded CC‑0 sound ≥ *min_dur* (fresh <= TREND_DAYS)."""
    lower = math.ceil(min_dur) + 1
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=TREND_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    flt = (
        f'license:"Creative Commons 0" '
        f'duration:[{lower} TO 10000] '
        f'created:[{since} TO NOW] '
        f'tag:music'
    )
    params = {
        "token": FREESOUND_API_KEY,
        "filter": flt,
        "sort": "downloads_desc",
        "page_size": 1,
        "page": rank,
        "fields": "id,previews,duration,num_downloads",
    }
    r = requests.get("https://freesound.org/apiv2/search/text/", params=params, timeout=30)
    r.raise_for_status(); res = r.json().get("results", [])
    if not res:  # fallback: ignore *created* filter to widen pool
        params["filter"] = flt.replace(f'created:[{since} TO NOW] ', '')
        r = requests.get("https://freesound.org/apiv2/search/text/", params=params, timeout=30)
        r.raise_for_status(); res = r.json().get("results", [])
    if not res:
        raise RuntimeError("No CC‑0 track of required length :(")
    item = res[0]
    return item["previews"].get("preview-hq-mp3") or item["previews"]["preview-lq-mp3"]

# ─── RENDER ONE CLIP ───────────────────────────────────────────────────────────

def render_clip(src_url: str, idx: int):
    dl(src_url, TMP_VIDEO)
    clip = VideoFileClip(TMP_VIDEO).without_audio()
    if clip.duration > MAX_DURATION:
        clip = clip.subclip(0, MAX_DURATION)

    label = make_caption(CAPTION_TEXT, clip.w, clip.h).set_duration(clip.duration)
    label = label.set_pos(("center", int(clip.h * 0.05)))

    # soundtrack
    aurl = viral_music_url(clip.duration, idx)
    print(f"[{idx}] ♫", aurl.split("/")[-1])
    dl(aurl, TMP_AUDIO)
    audio = AudioFileClip(TMP_AUDIO)
    if audio.duration < clip.duration:
        audio = audio_loop(audio, duration=clip.duration)
    else:
        audio = audio.subclip(0, clip.duration)

    final = clip.set_audio(audio)
    CompositeVideoClip([final, label]).write_videofile(
        OUT_TMPL.format(idx),
        codec="libx264",
        preset="slow",
        audio_codec="aac",
        fps=clip.fps,
        threads=os.cpu_count() or 4,
        temp_audiofile="__temp_aac__.m4a",
        remove_temp=True,
        ffmpeg_params=["-crf", "18"],
    )
    os.remove(TMP_VIDEO); os.remove(TMP_AUDIO)

# ─── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    urls = pexels_hd_portrait_mp4s(SEARCH_QUERY, VIDEOS_COUNT)
    for idx, url in enumerate(urls, 1):
        print(f"⇣  ({idx}/{len(urls)})", url.split("/")[-1])
        try:
            render_clip(url, idx)
        except Exception as exc:
            print("⚠️  Skipped:", exc)
    print("✓ All done!")


if __name__ == "__main__":
    main()
