#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
import time
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
SCRIPT_REV = "2026-05-12-full-coverage-retry-v2"
MAX_RETRIES = int(os.getenv("TRANSCRIPT_FETCH_RETRIES", "4"))
REQUIRE_FULL_COVERAGE = os.getenv("REQUIRE_FULL_COVERAGE", "0") == "1"
MAX_PASSES = int(os.getenv("TRANSCRIPT_MAX_PASSES", "70"))
RETRY_SLEEP_SEC = float(os.getenv("TRANSCRIPT_RETRY_SLEEP_SEC", "15"))
MAX_RUNTIME_SEC = int(float(os.getenv("TRANSCRIPT_MAX_HOURS", "23")) * 3600)
NOISE_RE = re.compile(r"^\s*(\[[^\]]+\]|\([^\)]+\)|\{[^\}]+\})\s*$", re.I)
TIMECODE_RE = re.compile(r"(?:^|\s)(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?(?:\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)?(?:$|\s)")


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
    return [{"videoId": e.get("id"), "title": e.get("title") or e.get("id"), "url": f"https://www.youtube.com/watch?v={e.get('id')}", "duration": e.get("duration"), "language": None} for e in (data.get("entries") or []) if e.get("id")]


def clean_caption_line(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = TIMECODE_RE.sub(" ", s)
    s = re.sub(r"\b(captions?|subtitles?|transcript|автосубтитры|субтитры)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -–—\t")
    if not s or NOISE_RE.match(s):
        return ""
    if re.fullmatch(r"(смех|музыка|аплодисменты|laugh(ing)?|music|applause|foreign)", s, re.I):
        return ""
    if re.fullmatch(r"[\W_]+", s):
        return ""
    return s


def parse_vtt_rows(vtt: str) -> list[tuple[int | None, str]]:
    rows, seen, cur = [], set(), None
    for line in vtt.splitlines():
        s = line.strip()
        if "-->" in s:
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", s)
            cur = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) if m else None
            continue
        if not s or s == "WEBVTT" or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        clean = clean_caption_line(s)
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
        text = "".join(seg.get("utf8", "") for seg in (e.get("segs") or []))
        clean = clean_caption_line(text)
        if clean:
            rows.append((int((e.get("tStartMs") or 0) / 1000), clean))
    return rows


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def fetch_rows_from_metadata(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    p = run(ytdlp_base_args() + ["-J", video["url"]])
    if p.returncode != 0:
        return None, []
    try:
        meta = json.loads(p.stdout)
    except Exception:
        return None, []
    caps = meta.get("subtitles") or meta.get("automatic_captions") or {}
    langs = ["ru"] + [x for x in sorted(caps.keys()) if x != "ru"]
    for lang in langs:
        for t in (caps.get(lang) or []):
            u = t.get("url")
            if not u:
                continue
            try:
                body = fetch_text(u)
            except Exception:
                continue
            rows = rows_from_json3(body) if ((t.get("ext") or "").lower() == "json3" or body.lstrip().startswith("{")) else parse_vtt_rows(body)
            if rows:
                return lang, rows
    return None, []


def fetch_rows_from_yta(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    if YouTubeTranscriptApi is None:
        return None, []

    def _rows(items: list[dict]) -> list[tuple[int | None, str]]:
        out = []
        for item in items:
            clean = clean_caption_line((item.get("text") or "").strip())
            if clean:
                out.append((int(item.get("start", 0)), clean))
        return out

    try:
        transcript = YouTubeTranscriptApi.get_transcript(video["videoId"], languages=["ru"])
        rows = _rows(transcript)
        if rows:
            return (transcript[0].get("language_code") if transcript else None), rows
    except Exception:
        pass

    try:
        listing = YouTubeTranscriptApi.list_transcripts(video["videoId"])
    except Exception:
        return None, []

    candidates = []
    for tr in listing:
        candidates.append(tr)
        try:
            candidates.append(tr.translate("en"))
        except Exception:
            pass

    seen = set()
    for tr in candidates:
        key = (getattr(tr, "language_code", None), getattr(tr, "is_generated", None))
        if key in seen:
            continue
        seen.add(key)
        try:
            fetched = tr.fetch()
        except Exception:
            continue
        rows = _rows(fetched)
        if rows:
            return getattr(tr, "language_code", None), rows

    return None, []



def fetch_rows_from_ytdlp_subs(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "%(id)s"
        p = run(ytdlp_base_args() + [
            "--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
            "--sub-langs", "ru,ru.*,all,-live_chat", "--sub-format", "vtt/json3/srv3",
            "-o", str(base), video["url"],
        ])
        if p.returncode != 0:
            return None, []

        candidates = sorted(Path(td).glob(f"{video['videoId']}*"))
        for fp in candidates:
            ext = fp.suffix.lower().lstrip('.')
            try:
                body = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rows = rows_from_json3(body) if ext == "json3" or body.lstrip().startswith("{") else parse_vtt_rows(body)
            if rows:
                # filename example: <id>.ru.vtt
                parts = fp.name.split('.')
                lang = parts[-2] if len(parts) >= 3 else None
                return lang, rows
    return None, []


def fetch_rows_from_ytdlp_subs(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "%(id)s"
        p = run(ytdlp_base_args() + [
            "--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs",
            "--sub-langs", "ru,en,ru.*,en.*", "--sub-format", "vtt/json3",
            "-o", str(base), video["url"],
        ])
        if p.returncode != 0:
            return None, []

        candidates = sorted(Path(td).glob(f"{video['videoId']}*"))
        for fp in candidates:
            ext = fp.suffix.lower().lstrip('.')
            try:
                body = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rows = rows_from_json3(body) if ext == "json3" or body.lstrip().startswith("{") else parse_vtt_rows(body)
            if rows:
                # filename example: <id>.ru.vtt
                parts = fp.name.split('.')
                lang = parts[-2] if len(parts) >= 3 else None
                return lang, rows
    return None, []

def fetch_rows_from_openai(video: dict) -> tuple[str | None, list[tuple[int | None, str]]]:
    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return None, []
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / f"{video['videoId']}.m4a"
        p = run(ytdlp_base_args() + ["-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "-o", str(audio), video["url"]])
        if p.returncode != 0 or not audio.exists():
            return None, []
        client = OpenAI()
        with audio.open("rb") as fh:
            tr = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=fh, response_format="verbose_json")
        rows = []
        for seg in (getattr(tr, "segments", None) or []):
            clean = clean_caption_line((getattr(seg, "text", "") or "").strip())
            if clean:
                rows.append((int(getattr(seg, "start", 0) or 0), clean))
        return (getattr(tr, "language", None) or "openai"), rows


def chunk_rows(video: dict, rows: list[tuple[int | None, str]]) -> list[dict]:
    out, seen, buf, start, idx = [], set(), [], None, 1
    for s, line in rows:
        if start is None:
            start = s
        buf.extend(line.split())
        if len(buf) >= MAX_WORDS:
            txt = " ".join(buf).strip()
            if txt and txt not in seen:
                seen.add(txt)
                out.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
                idx += 1
            buf, start = [], None
    if buf:
        txt = " ".join(buf).strip()
        if txt and txt not in seen:
            out.append({"id": f"{video['videoId']}-chunk-{idx:03d}", "videoId": video["videoId"], "title": video["title"], "url": video["url"], "start": start, "text": txt})
    return out



def fetch_video_rows(video: dict) -> tuple[str | None, list[tuple[int | None, str]], str | None]:
    attempts = max(1, MAX_RETRIES)
    last_source = None
    for _ in range(attempts):
        lang, rows = fetch_rows_from_metadata(video)
        last_source = "metadata"
        if rows:
            return lang, rows, last_source
        lang, rows = fetch_rows_from_ytdlp_subs(video)
        last_source = "ytdlp_subs"
        if rows:
            return lang, rows, last_source
        lang, rows = fetch_rows_from_yta(video)
        last_source = "yta"
        if rows:
            return lang, rows, last_source
        lang, rows = fetch_rows_from_openai(video)
        last_source = "openai"
        if rows:
            return lang, rows, last_source
    return None, [], last_source


def try_index_video(video: dict, existing_by_video: dict, out_videos_map: dict, out_chunks_map: dict) -> bool:
    vid = video["videoId"]
    old_v = existing_by_video.get(vid)
    lang, rows, source = fetch_video_rows(video)
    if rows:
        video = dict(video)
        video["language"] = lang
        video["transcriptSource"] = source
        ch = chunk_rows(video, rows)
        if ch:
            out_videos_map[vid] = video
            out_chunks_map[vid] = ch
            return True
    if old_v:
        out_videos_map.setdefault(vid, old_v)
        return True
    return False

def main() -> int:
    existing = load_existing()
    existing_by_video = {v.get("videoId"): v for v in (existing.get("videos") or []) if v.get("videoId")}
    chunks_by_video: dict[str, list[dict]] = {}
    for ch in (existing.get("chunks") or []):
        vid = ch.get("videoId")
        if vid:
            chunks_by_video.setdefault(vid, []).append(ch)

    videos = list_playlist_videos()
    if not videos:
        raise RuntimeError("Playlist returned zero videos; refusing to overwrite index with empty payload")
    new_count = 0

    # Start from existing index and update incrementally. This avoids losing old data on partial failures.
    out_videos_map = dict(existing_by_video)
    out_chunks_map = {vid: list(chunks) for vid, chunks in chunks_by_video.items()}

    start_ts = time.time()
    missing_map = {v["videoId"]: dict(v) for v in videos}

    for pass_idx in range(max(1, MAX_PASSES)):
        if not missing_map:
            break
        if time.time() - start_ts > MAX_RUNTIME_SEC:
            break

        unresolved = {}
        for vid, video in list(missing_map.items()):
            ok = try_index_video(video, existing_by_video, out_videos_map, out_chunks_map)
            if ok:
                if vid not in existing_by_video:
                    new_count += 1
            else:
                unresolved[vid] = video

        missing_map = unresolved
        print(f"Pass {pass_idx+1}: indexed={len(out_videos_map)}, unresolved={len(missing_map)}")
        if not missing_map:
            break
        if pass_idx + 1 < max(1, MAX_PASSES):
            time.sleep(max(0.0, RETRY_SLEEP_SEC))

    missing_videos = [
        {"videoId": v["videoId"], "title": v.get("title"), "url": v.get("url"), "reason": "transcript_unavailable"}
        for v in missing_map.values()
    ]

    # Keep playlist order first, but never drop already indexed videos/chunks that may be absent temporarily.
    ordered_ids = [v["videoId"] for v in videos]
    extra_ids = [vid for vid in out_videos_map.keys() if vid not in ordered_ids]
    final_ids = ordered_ids + sorted(extra_ids)
    out_videos = [out_videos_map[vid] for vid in final_ids if vid in out_videos_map]
    out_chunks = [ch for vid in final_ids for ch in out_chunks_map.get(vid, [])]

    Path("missing-videos.json").write_text(json.dumps({
        "updatedAt": now_iso(),
        "playlistVideos": len(videos),
        "missingCount": len(missing_videos),
        "videos": missing_videos,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if REQUIRE_FULL_COVERAGE and missing_videos:
        raise RuntimeError(f"Missing transcripts for {len(missing_videos)} playlist videos; see missing-videos.json")

    if not out_videos:
        if existing:
            raise RuntimeError("Indexing produced zero videos; preserving previously saved index")
        raise RuntimeError("Indexing produced zero videos on initial build")

    payload = {
        "updatedAt": now_iso(),
        "scriptRevision": SCRIPT_REV,
        "playlistUrl": PLAYLIST_URL,
        "videos": out_videos,
        "chunks": out_chunks,
        "error": None,
        "stats": {"playlistVideos": len(videos), "indexedVideos": len(out_videos), "chunks": len(out_chunks), "newOrRefreshedVideos": new_count},
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Indexed videos: {len(out_videos)}/{len(videos)}, chunks: {len(out_chunks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
