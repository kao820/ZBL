#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
STATUS_PATH = Path("playlist-video-status.json")
VIDEO_DIR = Path("playlist-video-transcripts")
MAX_WORDS = 120
SCRIPT_REV = "2026-05-26-per-video-v1"
YTDLP_TIMEOUT_SEC = int(os.getenv("YTDLP_TIMEOUT_SEC", "90"))
MAX_VIDEO_ATTEMPTS = int(os.getenv("MAX_VIDEO_ATTEMPTS", "5"))
PROCESS_MODE = os.getenv("PROCESS_MODE", "normal").strip().lower()

NOISE_RE = re.compile(r"^\s*(\[[^\]]+\]|\([^\)]+\)|\{[^\}]+\})\s*$", re.I)
TIMECODE_RE = re.compile(r"(?:^|\s)(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?(?:\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)?(?:$|\s)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=YTDLP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, stdout=e.stdout or "", stderr=(e.stderr or "") + "\nTIMEOUT")


def ytdlp_base_args() -> list[str]:
    args = ["yt-dlp"]
    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies_file:
        args += ["--cookies", cookies_file]
    return args


def list_playlist_videos() -> list[dict]:
    p = run(ytdlp_base_args() + ["--flat-playlist", "--dump-single-json", PLAYLIST_URL])
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "yt-dlp playlist fetch failed")
    data = json.loads(p.stdout)
    return [{"videoId": e.get("id"), "title": e.get("title") or e.get("id"), "url": f"https://www.youtube.com/watch?v={e.get('id')}"} for e in (data.get("entries") or []) if e.get("id")]


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


def fetch_rows(video: dict) -> tuple[str | None, list[tuple[int | None, str]], str, str | None]:
    p = run(ytdlp_base_args() + ["-J", video["url"]])
    if p.returncode == 0:
        try:
            meta = json.loads(p.stdout)
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
                        return lang, rows, "metadata", None
        except Exception:
            pass

    # fallback: yt-dlp writes subtitle files
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "%(id)s"
        p2 = run(ytdlp_base_args() + ["--skip-download", "--no-playlist", "--write-subs", "--write-auto-subs", "--sub-langs", "ru,ru.*,all,-live_chat", "--sub-format", "vtt/json3/srv3", "-o", str(base), video["url"]])
        if p2.returncode == 0:
            for fp in sorted(Path(td).glob(f"{video['videoId']}*")):
                ext = fp.suffix.lower().lstrip('.')
                body = fp.read_text(encoding="utf-8", errors="ignore")
                rows = rows_from_json3(body) if ext == "json3" or body.lstrip().startswith("{") else parse_vtt_rows(body)
                if rows:
                    return "ru", rows, "ytdlp_subs", None

    if YouTubeTranscriptApi is not None:
        try:
            tr = YouTubeTranscriptApi.get_transcript(video["videoId"], languages=["ru"])
            rows = []
            for item in tr:
                t = clean_caption_line((item.get("text") or "").strip())
                if t:
                    rows.append((int(item.get("start", 0)), t))
            if rows:
                return "ru", rows, "yta", None
        except Exception:
            pass

    lang, rows, src, err = fetch_rows_openai(video)
    if rows:
        return lang, rows, src, None

    return None, [], "none", err or "no_captions_or_api_access"


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



def cleanup_legacy_files() -> None:
    # Legacy artifacts from old monolithic flow can create false signals in CI/debugging.
    for fp in (Path("missing-videos.json"), Path("quartz/static/playlist-transcripts.json")):
        if fp.exists():
            fp.unlink()


def fetch_rows_openai(video: dict) -> tuple[str | None, list[tuple[int | None, str]], str, str | None]:
    if OpenAI is None or not os.getenv("OPENAI_API_KEY"):
        return None, [], "openai", "openai_not_configured"
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / f"{video['videoId']}.m4a"
        p = run(ytdlp_base_args() + ["-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "-o", str(audio), video["url"]])
        if p.returncode != 0 or not audio.exists():
            return None, [], "openai", "audio_download_failed"
        client = OpenAI()
        with audio.open("rb") as fh:
            tr = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=fh, response_format="verbose_json")
        rows = []
        for seg in (getattr(tr, "segments", None) or []):
            t = clean_caption_line((getattr(seg, "text", "") or "").strip())
            if t:
                rows.append((int(getattr(seg, "start", 0) or 0), t))
        if rows:
            return (getattr(tr, "language", None) or "ru"), rows, "openai", None
    return None, [], "openai", "openai_empty"

def load_status() -> dict:
    if not STATUS_PATH.exists():
        return {"updatedAt": None, "playlistUrl": PLAYLIST_URL, "videos": []}
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def save_status(status: dict) -> None:
    status["updatedAt"] = now_iso()
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def rebuild_aggregate(status: dict) -> None:
    videos, chunks = [], []
    for v in status.get("videos", []):
        if v.get("status") != "indexed":
            continue
        fp = VIDEO_DIR / f"{v['videoId']}.json"
        if not fp.exists():
            continue
        payload = json.loads(fp.read_text(encoding="utf-8"))
        videos.append(payload["video"])
        chunks.extend(payload["chunks"])
    out = {
        "updatedAt": now_iso(),
        "scriptRevision": SCRIPT_REV,
        "playlistUrl": PLAYLIST_URL,
        "videos": videos,
        "chunks": chunks,
        "error": None,
        "stats": {"playlistVideos": len(status.get("videos", [])), "indexedVideos": len(videos), "chunks": len(chunks)},
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_legacy_files()
    playlist = list_playlist_videos()
    status = load_status()
    existing = {v["videoId"]: v for v in status.get("videos", []) if v.get("videoId")}

    changed = False
    for item in playlist:
        vid = item["videoId"]
        if vid not in existing:
            status.setdefault("videos", []).append({"videoId": vid, "title": item["title"], "url": item["url"], "status": "pending", "updatedAt": now_iso()})
            changed = True

    pending = [v for v in status.get("videos", []) if v.get("status") in {"pending", "retry"}]

    if PROCESS_MODE == "queue_only":
        rebuild_aggregate(status)
        save_status(status)
        print("Queue refreshed: total={}, active={}".format(len(status.get("videos", [])), len(pending)))
        return 0

    if PROCESS_MODE == "rebuild_only":
        rebuild_aggregate(status)
        save_status(status)
        indexed_n = len([v for v in status.get("videos", []) if v.get("status") == "indexed"])
        print(f"Aggregate rebuilt: indexed={indexed_n}")
        return 0

    if not pending and not changed:
        print("No playlist updates and no pending videos. Nothing to do.")
        rebuild_aggregate(status)
        save_status(status)
        return 0

    target = pending[0] if pending else None
    if target:
        video = {"videoId": target["videoId"], "title": target["title"], "url": target["url"]}
        target["attempts"] = int(target.get("attempts", 0)) + 1
        lang, rows, source, err = fetch_rows(video)
        if rows:
            ch = chunk_rows(video, rows)
            if ch:
                per_video = {"updatedAt": now_iso(), "video": {**video, "language": lang, "transcriptSource": source}, "chunks": ch}
                (VIDEO_DIR / f"{video['videoId']}.json").write_text(json.dumps(per_video, ensure_ascii=False, indent=2), encoding="utf-8")
                target["status"] = "indexed"
                target["lastError"] = None
                target["updatedAt"] = now_iso()
            else:
                target["status"] = "retry" if target["attempts"] < MAX_VIDEO_ATTEMPTS else "missing"
                target["lastError"] = "empty_chunks"
                target["updatedAt"] = now_iso()
        else:
            target["status"] = "retry" if target["attempts"] < MAX_VIDEO_ATTEMPTS else "missing"
            target["lastError"] = err or source
            target["updatedAt"] = now_iso()

    rebuild_aggregate(status)
    save_status(status)
    indexed = len([v for v in status.get("videos", []) if v.get("status")=="indexed"]); retry = len([v for v in status.get("videos", []) if v.get("status")=="retry"]); missing = len([v for v in status.get("videos", []) if v.get("status")=="missing"]); pending_n = len([v for v in status.get("videos", []) if v.get("status")=="pending"]);
    print(f"Status: total={len(status.get('videos', []))}, indexed={indexed}, pending={pending_n}, retry={retry}, missing={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
