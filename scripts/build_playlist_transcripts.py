#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT_PATH = Path("playlist-transcripts.json")
MAX_WORDS = 160
SCRIPT_REV = "2026-05-11-fix-nonlocal-v2"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def load_existing() -> dict:
    if not OUT_PATH.exists():
        return {}
    try:
        data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_playlist_videos() -> list[dict]:
    p = run(["yt-dlp", "--flat-playlist", "--dump-single-json", PLAYLIST_URL])
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
            cur = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) if m else None
            continue
        if not s or s == "WEBVTT" or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        clean = re.sub(r"<[^>]+>", "", s)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean and clean not in seen:
            seen.add(clean)
            rows.append((cur, clean))
    return rows


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    out, seen = [], set()
    buf = []
    start = None
    idx = 1

    for s, line in rows:
        if start is None:
            start = s
        buf.extend(line.split())
        if len(buf) >= MAX_WORDS:
            txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
            if txt and txt not in seen:
                seen.add(txt)
                out.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
                idx += 1
            buf = []
            start = None

    if buf:
        txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if txt and txt not in seen:
            out.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
    return out



def extract_video_chunks(video: dict) -> list[dict]:
    vid = video["videoId"]
    with tempfile.TemporaryDirectory() as td:
        out_tpl = str(Path(td) / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "ru,en,ru.*,en.*,.*", "--sub-format", "vtt", "-o", out_tpl, video["url"],
        ]
        p = run(cmd)
        if p.returncode != 0:
            return []
        files = sorted(Path(td).glob(f"{vid}*.vtt"))
        if not files:
            return []
        chosen = files[0]
        if len(chosen.suffixes) >= 2:
            video["language"] = chosen.suffixes[-2].lstrip(".")
        return chunk_rows(video, parse_vtt_rows(chosen.read_text(encoding="utf-8", errors="ignore")))


def build() -> dict:
    existing = load_existing()
    existing_chunks = existing.get("chunks") if isinstance(existing.get("chunks"), list) else []
    by_video = {}
    for c in existing_chunks:
        by_video.setdefault(c.get("videoId"), []).append(c)

    videos = list_playlist_videos()
    chunks = []
    stats = {"total": len(videos), "withCaptions": 0, "withoutCaptions": 0, "failed": 0, "reused": 0, "downloaded": 0}

    for video in videos:
        vid = video["videoId"]
        reused = by_video.get(vid)
        if reused:
            chunks.extend(reused)
            stats["withCaptions"] += 1
            stats["reused"] += 1
            continue
        try:
            new_chunks = extract_video_chunks(video)
            if new_chunks:
                chunks.extend(new_chunks)
                stats["withCaptions"] += 1
                stats["downloaded"] += 1
            else:
                stats["withoutCaptions"] += 1
        except Exception:
            stats["failed"] += 1

    return {
        "scriptRev": SCRIPT_REV,
        "playlistUrl": PLAYLIST_URL,
        "updatedAt": now_iso(),
        "error": None if chunks else "index_unavailable",
        "videos": videos,
        "chunks": chunks,
        "stats": stats,
    }


def main() -> int:
    try:
        result = build()
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception:
        OUT_PATH.write_text(json.dumps({"playlistUrl": PLAYLIST_URL, "updatedAt": now_iso(), "error": "index_unavailable", "videos": [], "chunks": [], "stats": {"total": 0, "withCaptions": 0, "withoutCaptions": 0, "failed": 0, "reused": 0, "downloaded": 0}}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
