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


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def list_playlist_videos() -> list[dict]:
    p = run(["yt-dlp", "--flat-playlist", "--dump-single-json", PLAYLIST_URL])
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "yt-dlp playlist fetch failed")
    data = json.loads(p.stdout)
    entries = data.get("entries") or []
    vids = []
    for e in entries:
        vid = e.get("id")
        if not vid:
            continue
        vids.append({
            "videoId": vid,
            "title": e.get("title") or vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": e.get("duration"),
            "language": None,
        })
    return vids


def parse_vtt_rows(vtt: str) -> list[tuple[int | None, str]]:
    rows = []
    cur = None
    seen_line = set()
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
        if clean and clean not in seen_line:
            seen_line.add(clean)
            rows.append((cur, clean))
    return rows


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    out, buf, seen = [], [], set()
    start, idx = None, 1

    def flush():
        nonlocal buf, start, idx
        if not buf:
            return
        txt = re.sub(r"\s+", " ", " ".join(buf)).strip()
        if txt and txt not in seen:
            seen.add(txt)
            out.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
            idx += 1
        buf, start = [], None

    for s, line in rows:
        if start is None:
            start = s
        buf.extend(line.split())
        if len(buf) >= MAX_WORDS:
            flush()
    flush()
    return out


def extract_video_captions(video: dict) -> list[dict]:
    vid = video["videoId"]
    with tempfile.TemporaryDirectory() as td:
        out_tpl = str(Path(td) / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "ru,en,ru.*,en.*,.*",
            "--sub-format",
            "vtt",
            "-o",
            out_tpl,
            video["url"],
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
        vtt = chosen.read_text(encoding="utf-8", errors="ignore")
        rows = parse_vtt_rows(vtt)
        return chunk_rows(video, rows)


def build() -> dict:
    data = base_payload()
    videos = list_playlist_videos()
    data["stats"]["total"] = len(videos)

    for video in videos:
        data["videos"].append(video)
        try:
            chunks = extract_video_captions(video)
            if chunks:
                data["chunks"].extend(chunks)
                data["stats"]["withCaptions"] += 1
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
