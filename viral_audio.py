#!/usr/bin/env python3
"""
Fetch trending *YouTube Music* tracks (unofficial, ytmusicapi) and save **audio previews only**
for the first N items into ./Audio using iTunes Search API.

- Никаких видео и JSON — только аудио-превью (≈30 c) для теста пайплайна.
- Для публикаций офф-платформенно эти превью нельзя использовать (copyright). Это именно тест.
"""

from __future__ import annotations
import argparse, sys, re, shutil, subprocess
from typing import Any, Dict, List
from pathlib import Path
from urllib.parse import urlparse

import requests
from ytmusicapi import YTMusic

AUDIO_DIR = Path("Audio"); AUDIO_DIR.mkdir(parents=True, exist_ok=True)
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

# ---- Extra helpers ------------------------------------------------------------

def _guess_source_ext(url: str, headers: dict) -> str:
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    path = urlparse(url).path.lower()
    if ".mp3" in path or "audio/mpeg" in ctype:
        return ".mp3"
    if ".m4a" in path or "audio/mp4" in ctype or "audio/x-m4a" in ctype:
        return ".m4a"
    return ".m4a"


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

# ---- Helpers -----------------------------------------------------------------

def _safe_name(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\w\-\. ]+", "", s)
    return s[:100]


def chart_playlist_ids(charts: Dict[str, Any], need: int = 2) -> List[str]:
    ids: List[str] = []
    v = charts.get("videos")
    if isinstance(v, dict) and v.get("playlist"):
        ids.append(v["playlist"])  # trending videos as watch-playlist
    t = charts.get("trending")
    if isinstance(t, dict) and t.get("playlist"):
        ids.append(t["playlist"])  # sometimes present
    for g in charts.get("genres", []):
        pid = g.get("playlistId")
        if pid and pid not in ids:
            ids.append(pid)
    return ids[:need]


def tracks_from_watch_playlist(yt: YTMusic, pid: str, limit: int) -> List[Dict[str, Any]]:
    wp = yt.get_watch_playlist(playlistId=pid, limit=limit)
    out: List[Dict[str, Any]] = []
    for it in wp.get("tracks", []):
        artists = [a.get("name") for a in (it.get("artists") or []) if a.get("name")]
        if not artists:
            by = (it.get("byline") or "").split("•")[0].strip()
            if by:
                artists = [by]
        out.append({
            "title": it.get("title"),
            "artists": artists,
        })
    return out

# ---- iTunes Search: preview URL for a title/artist ----------------------------

def itunes_preview_for(title: str, artists: List[str], country: str = "US") -> tuple[str, dict] | None:
    term = f"{title} {artists[0]}" if artists else title
    params = {
        "term": term,
        "media": "music",
        "entity": "musicTrack",
        "limit": 10,
        "country": country,
    }
    r = requests.get(ITUNES_SEARCH_URL, params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("results", [])
    if not items:
        return None

    tnorm = title.lower()
    def score(x: dict) -> int:
        s = 0
        tn = x.get("trackName", "").lower()
        an = x.get("artistName", "").lower()
        if tnorm in tn:
            s += 3
        if artists and artists[0].lower() in an:
            s += 2
        s += int(x.get("trackTimeMillis", 0) // 10000)
        return s

    items.sort(key=score, reverse=True)
    for it in items:
        url = it.get("previewUrl")
        if url:
            return url, it
    return None


def save_preview(url: str, meta: dict) -> Path:
    """Download preview and ensure **audio-only** output (no video track).
    If ffmpeg is available, we always strip video with -vn.
    """
    # 1) HEAD: detect container
    try:
        h = requests.head(url, allow_redirects=True, timeout=20)
        h.raise_for_status()
        source_ext = _guess_source_ext(url, h.headers)
    except Exception:
        source_ext = ".m4a"

    # 2) base filename (strip accidental extensions from title)
    name = _safe_name(f"{meta.get('artistName','Unknown')} - {meta.get('trackName','Track')}")
    lower = name.lower()
    for ext in ('.mp3','.m4a','.wav','.flac','.aac','.ogg','.mp4','.m4v'):
        if lower.endswith(ext):
            name = name[: -len(ext)]
            break

    # 3) download to temp as-is
    tmp = AUDIO_DIR / f".__dl_{name}{source_ext}"
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)

    # 4) normalize: output pure mp3, dropping any video stream
    out = AUDIO_DIR / f"yt_trending_preview_{name}.mp3"
    i = 2
    while out.exists():
        out = AUDIO_DIR / f"yt_trending_preview_{name}_{i}.mp3"
        i += 1

    if _has_ffmpeg():
        subprocess.run([
            "ffmpeg", "-y", "-i", str(tmp), "-vn",
            "-acodec", "libmp3lame", "-q:a", "2", str(out)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tmp.unlink(missing_ok=True)
    else:
        # no ffmpeg → just rename into proper container if it's audio-only already
        out = AUDIO_DIR / (f"yt_trending_preview_{name}{source_ext}")
        j = 2
        while out.exists():
            out = AUDIO_DIR / f"yt_trending_preview_{name}_{j}{source_ext}"
            j += 1
        tmp.replace(out)
        return out

    return out

# ---- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Save ONLY audio previews for trending YouTube Music tracks into ./Audio")
    ap.add_argument("--country", default="US", help="ISO-3166 country code (default: US; ZZ = global)")
    ap.add_argument("--chart-playlists", type=int, default=1, help="how many chart playlists to combine (default: 1)")
    ap.add_argument("--limit", type=int, default=30, help="tracks per playlist to fetch (default: 30)")
    ap.add_argument("-n", "--num", type=int, default=1, help="how many previews to save (default: 1)")
    args = ap.parse_args()

    yt = YTMusic()  # public charts
    charts = yt.get_charts(country=args.country)

    pids = chart_playlist_ids(charts, args.chart_playlists)
    if not pids:
        sys.exit("No chart playlists available for this country.")

    tracks: List[Dict[str, Any]] = []
    for pid in pids:
        tracks.extend(tracks_from_watch_playlist(yt, pid, args.limit))
    if not tracks:
        sys.exit("No tracks fetched.")

    saved = 0
    for t in tracks:
        if saved >= args.num:
            break
        pr = itunes_preview_for(t.get("title") or "", t.get("artists") or [], country=args.country)
        if not pr:
            continue
        url, meta = pr
        out = save_preview(url, meta)
        print(f"Saved preview → {out}")
        saved += 1

    if saved == 0:
        sys.exit("No previews saved — try another country or increase --limit.")


if __name__ == "__main__":
    main()
