#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT_PATH = Path("playlist-transcripts.json")
MAX_WORDS = 160


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def base_payload() -> dict:
    return {
        "playlistUrl": PLAYLIST_URL,
        "updatedAt": now_iso(),
        "error": None,
        "videos": [],
        "chunks": [],
        "stats": {"total": 0, "withCaptions": 0, "withoutCaptions": 0, "failed": 0},
    }


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_playlist_video_ids(html: str) -> list[str]:
    return sorted(set(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)))


def parse_title(html: str, vid: str) -> str:
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, flags=re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    if m2:
        t = re.sub(r"\s+", " ", m2.group(1)).strip()
        t = re.sub(r"\s*-\s*YouTube$", "", t).strip()
        if t and t != "- YouTube":
            return t
    return vid


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    chunks, buf, seen = [], [], set()
    start, idx = None, 1

    def flush():
        nonlocal buf, start, idx
        if not buf:
            return
        txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if txt and txt not in seen:
            seen.add(txt)
            chunks.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
            idx += 1
        buf, start = [], None

    for s, line in rows:
        if start is None:
            start = s
        buf.extend(line.split())
        if len(buf) >= MAX_WORDS:
            flush()
    flush()
    return chunks


def parse_srv_xml(xml: str) -> list[tuple[int | None, str]]:
    rows = []
    for start, text in re.findall(r'<text[^>]*start="([0-9.]+)"[^>]*>(.*?)</text>', xml, flags=re.S):
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            rows.append((int(float(start)), clean))
    return rows


def tracks_from_player_html(html: str) -> list[dict]:
    for pat in [r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;", r"var\s+ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;"]:
        m = re.search(pat, html, flags=re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            return data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        except Exception:
            continue
    return []


def choose_track(tracks: list[dict]) -> dict | None:
    if not tracks:
        return None
    def key(t: dict):
        lang = (t.get("languageCode") or "").lower()
        if lang == "ru": rank = 0
        elif lang == "en": rank = 1
        elif lang.startswith("ru"): rank = 2
        elif lang.startswith("en"): rank = 3
        else: rank = 4
        return (rank, 0 if t.get("kind") == "asr" else 1)
    return sorted(tracks, key=key)[0]


def try_http_captions(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    html = fetch_text(video["url"])
    tracks = tracks_from_player_html(html)
    track = choose_track(tracks)
    if not track or not track.get("baseUrl"):
        return None, []
    base = track["baseUrl"]
    sep = "&" if "?" in base else "?"
    xml = fetch_text(f"{base}{sep}fmt=srv3")
    return track.get("languageCode"), parse_srv_xml(xml)


def try_ytdlp_captions(vid: str, url: str) -> tuple[str | None, list[tuple[int | None, str]]]:
    if not shutil.which("yt-dlp"):
        return None, []
    with tempfile.TemporaryDirectory() as td:
        cmd = ["yt-dlp", "--skip-download", "--write-auto-subs", "--sub-langs", "ru,en,ru.*,en.*,.*", "--sub-format", "vtt", "-o", str(Path(td)/"%(id)s.%(ext)s"), url]
        proc = subprocess.run(cmd, text=True, capture_output=True)
        if proc.returncode != 0:
            return None, []
        files = sorted(Path(td).glob(f"{vid}*.vtt"))
        if not files:
            return None, []
        sf = files[0]
        lang = sf.suffixes[-2].lstrip('.') if len(sf.suffixes) >= 2 else None
        txt = sf.read_text(encoding="utf-8", errors="ignore")
        rows = []
        cur = None
        for line in txt.splitlines():
            s = line.strip()
            if "-->" in s:
                m = re.search(r"(\d{2}):(\d{2}):(\d{2})", s)
                cur = int(m.group(1))*3600+int(m.group(2))*60+int(m.group(3)) if m else None
                continue
            if not s or s == "WEBVTT" or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
                continue
            clean = re.sub(r"<[^>]+>", "", s)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                rows.append((cur, clean))
        return lang, rows


def build() -> dict:
    data = base_payload()
    playlist_html = fetch_text(PLAYLIST_URL)
    ids = parse_playlist_video_ids(playlist_html)
    data["stats"]["total"] = len(ids)
    for vid in ids:
        url = f"https://www.youtube.com/watch?v={vid}"
        try:
            html = fetch_text(url)
            video = {"videoId": vid, "title": parse_title(html, vid), "url": url, "duration": None, "language": None}
            dm = re.search(r'"lengthSeconds":"(\d+)"', html)
            if dm: video["duration"] = int(dm.group(1))

            lang, rows = try_ytdlp_captions(vid, url)
            if not rows:
                lang, rows = try_http_captions(video)

            video["language"] = lang
            data["videos"].append(video)
            if rows:
                data["stats"]["withCaptions"] += 1
                data["chunks"].extend(chunk_rows(video, rows))
            else:
                data["stats"]["withoutCaptions"] += 1
        except Exception:
            data["stats"]["failed"] += 1

    if not data["chunks"]:
        data["error"] = "index_unavailable"
    return data


def main() -> int:
    try:
        result = build()
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception:
        fail = base_payload()
        fail["error"] = "index_unavailable"
        OUT_PATH.write_text(json.dumps(fail, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
