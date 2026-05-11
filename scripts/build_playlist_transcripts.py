#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:
    YouTubeTranscriptApi = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT_PATH = Path("playlist-transcripts.json")
MAX_WORDS = 120
SCRIPT_REV = "2026-05-11-final-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def ytdlp_base_args() -> list[str]:
    args = ["yt-dlp"]
    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies_file:
        args += ["--cookies", cookies_file]
    return args


def load_existing() -> dict:
    if not OUT_PATH.exists():
        return {}
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_playlist_videos() -> list[dict]:
    p = run(ytdlp_base_args() + ["--flat-playlist", "--dump-single-json", PLAYLIST_URL])
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "yt-dlp playlist fetch failed")
    data = json.loads(p.stdout)
    entries = data.get("entries") or []
    return [
        {
            "videoId": e.get("id"),
            "title": e.get("title") or e.get("id"),
            "url": f"https://www.youtube.com/watch?v={e.get('id')}",
            "duration": e.get("duration"),
            "language": None,
        }
        for e in entries
        if e.get("id")
    ]


def parse_vtt_rows(vtt: str) -> list[tuple[int | None, str]]:
    rows, seen = [], set()
    cur = None
    for line in vtt.splitlines():
        s = line.strip()
        if "-->" in s:
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", s)
            cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None
            continue
        if not s or s == "WEBVTT" or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        clean = re.sub(r"<[^>]+>", "", s)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean and clean not in seen:
            seen.add(clean)
            rows.append((cur, clean))
    return rows


def rows_from_json3(payload: str) -> list[tuple[int | None, str]]:
    try:
        data = json.loads(payload)
    except Exception:
        return []
    rows = []
    for e in data.get("events", []):
        segs = e.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if not text:
            continue
        start = int((e.get("tStartMs") or 0) / 1000)
        clean = re.sub(r"\s+", " ", text).strip()
        if clean:
            rows.append((start, clean))
    return rows


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    out, seen = [], set()
    buf, start, idx = [], None, 1

    for s, line in rows:
        if start is None:
            start = s
        buf.extend(line.split())
        if len(buf) >= MAX_WORDS:
            txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
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
            buf, start = [], None

    if buf:
        txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if txt and txt not in seen:
            out.append({
                "id": f"{video['videoId']}-chunk-{idx:03d}",
                "videoId": video["videoId"],
                "title": video["title"],
                "url": video["url"],
                "start": start,
                "text": txt,
            })

    return out


def fallback_rows_from_metadata(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    p = run(ytdlp_base_args() + ["-J", video["url"]])
    if p.returncode != 0:
        return None, []
    try:
        meta = json.loads(p.stdout)
    except Exception:
        return None, []

    caps = meta.get("automatic_captions") or meta.get("subtitles") or {}
    langs = ["ru", "en"] + sorted(caps.keys())
    seen = set()

    for lang in langs:
        if lang in seen:
            continue
        seen.add(lang)
        for t in caps.get(lang) or []:
            u = t.get("url")
            ext = (t.get("ext") or "").lower()
            if not u:
                continue
            try:
                body = fetch_text(u)
            except Exception:
                continue

            if ext == "json3" or body.strip().startswith("{"):
                rows = rows_from_json3(body)
            else:
                rows = parse_vtt_rows(body)

            if rows:
                return lang, rows

    return None, []


def fallback_rows_from_yta(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    if YouTubeTranscriptApi is None:
        return None, []
    vid = video.get("videoId")
    if not vid:
        return None, []

    try:
        transcript = YouTubeTranscriptApi.get_transcript(vid, languages=["ru", "en"])
    except Exception:
        return None, []

    rows = []
    for item in transcript:
        txt = re.sub(r"\s+", " ", (item.get("text") or "").strip())
        if txt:
            rows.append((int(item.get("start", 0)), txt))

    lang = transcript[0].get("language_code") if transcript else None
    return lang, rows


def fallback_rows_from_openai(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return None, []

    vid = video.get("videoId")
    if not vid:
        return None, []

    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / f"{vid}.m4a"
        cmd = ytdlp_base_args() + [
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--no-playlist",
            "-o", str(audio),
            video["url"],
        ]
        p = run(cmd)
        if p.returncode != 0 or not audio.exists():
            return None, []

        client = OpenAI()
        with audio.open("rb") as fh:
            tr = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=fh,
                response_format="verbose_json",
            )

        segs = getattr(tr, "segments", None) or []
        rows = []
        if segs:
            for seg in segs:
                txt = re.sub(r"\s+", " ", (seg.text or "").strip())
                if txt:
                    rows.append((int(seg.start or 0), txt))
        else:
            txt = re.sub(r"\s+", " ", (getattr(tr, "text", "") or "").strip())
            if txt:
                rows.append((0, txt))

        return "auto-openai", rows


def extract_video_chunks(video: dict) -> list[dict]:
    vid = video["videoId"]
    with tempfile.TemporaryDirectory() as td:
        out_tpl = str(Path(td) / "%(id)s.%(ext)s")
        cmd = ytdlp_base_args() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "ru,en,ru.*,en.*,.*",
            "--sub-format", "vtt",
            "-o", out_tpl,
            video["url"],
        ]
        p = run(cmd)

        if p.returncode == 0:
            files = sorted(Path(td).glob(f"{vid}*.vtt"))
            if files:
                chosen = files[0]
                if len(chosen.suffixes) >= 2:
                    video["language"] = chosen.suffixes[-2].lstrip(".")
                rows = parse_vtt_rows(chosen.read_text(encoding="utf-8", errors="ignore"))
                if rows:
                    return chunk_rows(video, rows)

        # fallback 1: metadata tracks
        lang, rows = fallback_rows_from_metadata(video)
        if lang:
            video["language"] = lang
        if rows:
            return chunk_rows(video, rows)

        # fallback 2: youtube_transcript_api
        ylang, yrows = fallback_rows_from_yta(video)
        if ylang:
            video["language"] = ylang
        if yrows:
            return chunk_rows(video, yrows)

        # fallback 3: OpenAI transcription
        olang, orows = fallback_rows_from_openai(video)
        if olang:
            video["language"] = olang
        if orows:
            return chunk_rows(video, orows)

        return []


def dedupe_chunks(chunks: list[dict]) -> list[dict]:
    by_id = {}
    seen_text = set()

    for c in chunks:
        cid = c.get("id")
        txt = re.sub(r"\s+", " ", (c.get("text") or "").strip())
        key = (c.get("videoId"), txt)
        if not txt:
            continue
        if key in seen_text:
            continue
        seen_text.add(key)
        if cid not in by_id:
            by_id[cid] = c

    out = list(by_id.values())
    out.sort(key=lambda x: (x.get("videoId") or "", x.get("start") or 0, x.get("id") or ""))
    return out


def extract_with_retries(video: dict, attempts: int = 3) -> list[dict]:
    for i in range(attempts):
        chunks = extract_video_chunks(video)
        if chunks:
            return chunks
        if i < attempts - 1:
            time.sleep(1 + i)
    return []


def build() -> dict:
    existing = load_existing()
    existing_chunks = existing.get("chunks") if isinstance(existing.get("chunks"), list) else []
    by_video = {}
    for c in existing_chunks:
        by_video.setdefault(c.get("videoId"), []).append(c)

    videos = list_playlist_videos()
    chunks = list(existing_chunks)
    stats = {
        "total": len(videos),
        "withCaptions": 0,
        "withoutCaptions": 0,
        "failed": 0,
        "reused": 0,
        "downloaded": 0,
    }
    missing_videos = []

    for video in videos:
        vid = video["videoId"]
        if by_video.get(vid):
            stats["withCaptions"] += 1
            stats["reused"] += 1
            continue

        try:
            new_chunks = extract_with_retries(video, attempts=3)
            if new_chunks:
                chunks.extend(new_chunks)
                stats["withCaptions"] += 1
                stats["downloaded"] += 1
            else:
                stats["withoutCaptions"] += 1
                missing_videos.append(vid)
        except Exception:
            stats["failed"] += 1
            missing_videos.append(vid)

    chunks = dedupe_chunks(chunks)

    if not chunks and existing_chunks:
        chunks = dedupe_chunks(existing_chunks)
        stats["reused"] = max(stats["reused"], len({c.get("videoId") for c in chunks if c.get("videoId")}))

    return {
        "scriptRev": SCRIPT_REV,
        "playlistUrl": PLAYLIST_URL,
        "updatedAt": now_iso(),
        "error": None if chunks else "index_unavailable",
        "videos": videos,
        "chunks": chunks,
        "stats": stats,
        "missingVideos": sorted(set(missing_videos)),
    }


def main() -> int:
    try:
        result = build()
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception:
        existing = load_existing()
        if isinstance(existing.get("chunks"), list) and existing.get("chunks"):
            existing["updatedAt"] = now_iso()
            existing["error"] = None
            OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0

        fallback = {
            "scriptRev": SCRIPT_REV,
            "playlistUrl": PLAYLIST_URL,
            "updatedAt": now_iso(),
            "error": "index_unavailable",
            "videos": [],
            "chunks": [],
            "stats": {
                "total": 0,
                "withCaptions": 0,
                "withoutCaptions": 0,
                "failed": 0,
                "reused": 0,
                "downloaded": 0,
            },
            "missingVideos": [],
        }
        OUT_PATH.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
