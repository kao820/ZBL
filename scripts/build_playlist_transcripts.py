#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
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


def payload(error=None):
    return {"playlistUrl": PLAYLIST_URL, "updatedAt": now_iso(), "error": error, "videos": [], "chunks": []}


def load_existing():
    if not OUT_PATH.exists():
        return None
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_video_ids_from_playlist(html: str) -> list[str]:
    ids = sorted(set(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)))
    return ids


def parse_title(html: str, video_id: str) -> str:
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, flags=re.I)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    if m2:
        t = re.sub(r"\s+", " ", m2.group(1)).strip()
        t = re.sub(r"\s*-\s*YouTube$", "", t).strip()
        if t and t != "- YouTube":
            return t
    return video_id


def extract_player_response_json(html: str) -> dict | None:
    pats = [
        r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;",
        r"var\s+ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;",
    ]
    for pat in pats:
        m = re.search(pat, html, flags=re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def extract_caption_tracks(player_response: dict) -> list[dict]:
    return (
        player_response.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )


def pick_track(tracks: list[dict]) -> dict | None:
    if not tracks:
        return None
    pref = ["ru", "en"]

    def rank(t: dict):
        lang = (t.get("languageCode") or "").lower()
        kind = t.get("kind", "")
        if lang == "ru":
            base = 0
        elif lang == "en":
            base = 1
        elif lang.startswith("ru"):
            base = 2
        elif lang.startswith("en"):
            base = 3
        else:
            base = 4
        auto_penalty = 0 if kind == "asr" else 1
        return (base, auto_penalty, lang)

    return sorted(tracks, key=rank)[0]


def transcript_rows_from_track(track: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    base_url = track.get("baseUrl")
    if not base_url:
        return None, []
    lang = track.get("languageCode")
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}fmt=srv3"
    xml = fetch_text(url)

    rows = []
    for start, text in re.findall(r'<text[^>]*start="([0-9.]+)"[^>]*>(.*?)</text>', xml, flags=re.S):
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            rows.append((int(float(start)), clean))
    return lang, rows


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    out, bucket = [], []
    start = None
    idx = 1
    seen = set()

    def flush():
        nonlocal bucket, start, idx
        if not bucket:
            return
        txt = re.sub(r"\s+", " ", " ".join(bucket)).strip()
        if txt and txt not in seen:
            seen.add(txt)
            out.append({
                "id": f"{video['videoId']}-chunk-{idx:03d}",
                "videoId": video["videoId"],
                "title": video["title"],
                "url": video["url"],
                "start": start,
                "text": txt,
            })
            idx += 1
        bucket = []
        start = None

    for s, line in rows:
        words = line.split()
        if start is None:
            start = s
        bucket.extend(words)
        if len(bucket) >= MAX_WORDS:
            flush()
    flush()
    return out


def fetch_video_bundle(video_id: str) -> tuple[dict, list[tuple[int | None, str]]]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    html = fetch_text(url)
    title = parse_title(html, video_id)
    dur_match = re.search(r'"lengthSeconds":"(\d+)"', html)
    duration = int(dur_match.group(1)) if dur_match else None

    video = {"videoId": video_id, "title": title, "url": url, "duration": duration, "language": None}
    player = extract_player_response_json(html)
    if not player:
        return video, []

    tracks = extract_caption_tracks(player)
    track = pick_track(tracks)
    if not track:
        return video, []

    lang, rows = transcript_rows_from_track(track)
    video["language"] = lang
    return video, rows


def main() -> int:
    data = payload()
    try:
        playlist_html = fetch_text(PLAYLIST_URL)
        video_ids = parse_video_ids_from_playlist(playlist_html)
        if not video_ids:
            data["error"] = "index_unavailable"
            OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

        for vid in video_ids:
            try:
                video, rows = fetch_video_bundle(vid)
                data["videos"].append(video)
                if rows:
                    data["chunks"].extend(chunk_rows(video, rows))
            except Exception:
                data["videos"].append({"videoId": vid, "title": vid, "url": f"https://www.youtube.com/watch?v={vid}", "duration": None, "language": None})

        if not data["chunks"]:
            existing = load_existing()
            if existing and existing.get("chunks"):
                existing["updatedAt"] = now_iso()
                OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                return 0
            data["error"] = "index_unavailable"

        OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception:
        existing = load_existing()
        if existing and existing.get("chunks"):
            existing["updatedAt"] = now_iso()
            OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        OUT_PATH.write_text(json.dumps(payload("index_unavailable"), ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
