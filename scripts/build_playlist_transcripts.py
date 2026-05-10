#!/usr/bin/env python3
"""Build a static transcript index for playlist QA (GitHub Pages friendly)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT_PATH = Path("playlist-transcripts.json")
TMP_DIR = Path(".tmp_subs")
LANG_PREF = ["ru", "en"]
MAX_WORDS = 160


@dataclass
class VideoMeta:
    video_id: str
    title: str
    url: str
    duration: int | None
    language: str | None = None


def empty_payload(error: str | None = None) -> dict:
    return {
        "playlistUrl": PLAYLIST_URL,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "videos": [],
        "chunks": [],
    }


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True)


def write_payload(payload: dict) -> None:
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def choose_sub_file(video_id: str, files: Iterable[Path]) -> Path | None:
    files = list(files)
    if not files:
        return None

    def rank(path: Path) -> tuple[int, str]:
        suffixes = path.suffixes
        lang = suffixes[-2].lstrip(".") if len(suffixes) >= 2 else "zz"
        if lang == "ru":
            return (0, lang)
        if lang == "en":
            return (1, lang)
        if lang.startswith("ru"):
            return (2, lang)
        if lang.startswith("en"):
            return (3, lang)
        return (4, lang)

    return sorted(files, key=rank)[0]


def parse_lang_from_filename(path: Path) -> str | None:
    # Example: AbCdEf.ru.vtt or AbCdEf.en-orig.vtt
    suffixes = path.suffixes
    if len(suffixes) < 2:
        return None
    return suffixes[-2].lstrip(".")


def vtt_blocks(vtt_text: str) -> list[tuple[int | None, str]]:
    blocks: list[tuple[int | None, str]] = []
    cur_start: int | None = None
    cur_lines: list[str] = []

    for raw in vtt_text.splitlines():
        line = raw.strip()
        if not line:
            if cur_lines:
                blocks.append((cur_start, " ".join(cur_lines)))
                cur_lines = []
            continue

        if "-->" in line:
            if cur_lines:
                blocks.append((cur_start, " ".join(cur_lines)))
                cur_lines = []
            cur_start = parse_start_seconds(line)
            continue

        if line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue

        clean = re.sub(r"<[^>]+>", "", line)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            cur_lines.append(clean)

    if cur_lines:
        blocks.append((cur_start, " ".join(cur_lines)))

    return blocks


def parse_start_seconds(time_range: str) -> int | None:
    m = re.search(r"(\d{2}:\d{2}:\d{2}\.\d{3})", time_range)
    if not m:
        return None
    h, mnt, sec = m.group(1).split(":")
    return int(h) * 3600 + int(mnt) * 60 + int(float(sec))


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text


def chunk_blocks(video: VideoMeta, blocks: list[tuple[int | None, str]]) -> list[dict]:
    chunks: list[dict] = []
    bucket: list[str] = []
    start: int | None = None
    chunk_idx = 1
    seen = set()

    def flush() -> None:
        nonlocal bucket, start, chunk_idx
        if not bucket:
            return
        text = normalize_text(" ".join(bucket))
        if not text or text in seen:
            bucket = []
            start = None
            return
        seen.add(text)
        chunks.append(
            {
                "id": f"{video.video_id}-chunk-{chunk_idx:03d}",
                "videoId": video.video_id,
                "title": video.title,
                "url": video.url,
                "start": start,
                "text": text,
            }
        )
        chunk_idx += 1
        bucket = []
        start = None

    for block_start, block_text in blocks:
        words = block_text.split()
        if not words:
            continue
        if start is None:
            start = block_start
        bucket.extend(words)
        if len(bucket) >= MAX_WORDS:
            flush()

    flush()
    return chunks


def collect_playlist_entries() -> tuple[list[VideoMeta], str | None]:
    proc = run(["yt-dlp", "--flat-playlist", "--dump-json", PLAYLIST_URL])
    if proc.returncode != 0:
        return [], proc.stderr.strip() or "Не удалось получить плейлист"

    videos: list[VideoMeta] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        meta = json.loads(line)
        vid = meta.get("id")
        if not vid:
            continue
        videos.append(
            VideoMeta(
                video_id=vid,
                title=meta.get("title") or vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                duration=meta.get("duration"),
            )
        )
    return videos, None


def download_subs(video: VideoMeta) -> Path | None:
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-subs",
        "--sub-langs",
        "ru,en,ru.*,en.*,.*",
        "--sub-format",
        "vtt",
        "-o",
        str(TMP_DIR / "%(id)s.%(ext)s"),
        video.url,
    ]
    _ = run(cmd)
    return choose_sub_file(video.video_id, TMP_DIR.glob(f"{video.video_id}*.vtt"))


def main() -> None:
    if shutil.which("yt-dlp") is None:
        write_payload(empty_payload("yt-dlp не найден в окружении"))
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    payload = empty_payload(None)
    videos, err = collect_playlist_entries()
    if err:
        payload["error"] = err
        write_payload(payload)
        return

    all_chunks: list[dict] = []
    for video in videos:
        sub = download_subs(video)
        if not sub:
            payload["videos"].append(video.__dict__)
            continue

        video.language = parse_lang_from_filename(sub)
        raw = sub.read_text(encoding="utf-8", errors="ignore")
        blocks = vtt_blocks(raw)
        chunks = chunk_blocks(video, blocks)
        all_chunks.extend(chunks)
        payload["videos"].append(video.__dict__)

    payload["chunks"] = all_chunks
    if not payload["chunks"]:
        payload["error"] = "Субтитры не извлечены: проверьте доступ к YouTube/ограничения авто-субтитров"
    write_payload(payload)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload(error=None):
    return {"playlistUrl": PLAYLIST_URL, "updatedAt": now_iso(), "error": error, "videos": [], "chunks": []}


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_video_ids_from_playlist(html: str) -> list[str]:
    ids = set(re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html))
    return list(ids)


def parse_title(html: str) -> str | None:
    m = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    if not m:
        return None
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    return re.sub(r" - YouTube$", "", t)


def transcript_api_url(video_id: str, lang: str) -> str:
    qs = urllib.parse.urlencode({"lang": lang, "v": video_id, "fmt": "srv3"})
    return f"https://www.youtube.com/api/timedtext?{qs}"


def parse_transcript_xml(xml: str) -> list[tuple[int | None, str]]:
    rows = []
    for start, text in re.findall(r'<text[^>]*start="([0-9.]+)"[^>]*>(.*?)</text>', xml, flags=re.S):
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            rows.append((int(float(start)), clean))
    return rows


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


def fetch_video_meta(video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    html = fetch_text(url)
    title = parse_title(html) or video_id
    dur_match = re.search(r'"lengthSeconds":"(\d+)"', html)
    duration = int(dur_match.group(1)) if dur_match else None
    return {"videoId": video_id, "title": title, "url": url, "duration": duration, "language": None}


def fetch_best_transcript(video_id: str) -> tuple[str | None, list[tuple[int | None, str]]]:
    for lang in ["ru", "en"]:
        xml = fetch_text(transcript_api_url(video_id, lang))
        rows = parse_transcript_xml(xml)
        if rows:
            return lang, rows
    return None, []


def main() -> int:
    data = payload()
    try:
        main()
    except Exception as exc:  # keep JSON valid in all cases
        write_payload(empty_payload(f"Сбой сборки индекса: {exc}"))
