#!/usr/bin/env python3
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT = Path("playlist-transcripts.json")
TMP = Path(".tmp_subs")


def base_payload():
    return {"playlistUrl": PLAYLIST_URL, "updatedAt": None, "error": None, "videos": [], "chunks": []}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def clean_vtt(text):
    lines, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return " ".join(lines).strip()


def parse_vtt_chunks(vtt_text, max_words=150):
    entries = []
    current_start = None
    current_text = []
    for line in vtt_text.splitlines():
        s = line.strip()
        if "-->" in s:
            if current_text:
                entries.append((current_start, " ".join(current_text)))
                current_text = []
            current_start = _start_seconds(s)
            continue
        if s and s not in {"WEBVTT"} and not s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            clean = re.sub(r"<[^>]+>", "", s).strip()
            if clean:
                current_text.append(clean)
    if current_text:
        entries.append((current_start, " ".join(current_text)))

    chunks, bucket, starts = [], [], []
    idx = 1
    for st, txt in entries:
        words = txt.split()
        if st is not None and not starts:
            starts.append(st)
        bucket.extend(words)
        if len(bucket) >= max_words:
            chunks.append((idx, starts[0] if starts else None, " ".join(bucket)))
            idx += 1
            bucket, starts = [], []
    if bucket:
        chunks.append((idx, starts[0] if starts else None, " ".join(bucket)))
    return chunks


def _start_seconds(timerange):
    m = re.search(r"(\d{2}:\d{2}:\d{2}\.\d{3})", timerange)
    if not m:
        return None
    h, mi, sec = m.group(1).split(":")
    return int(h) * 3600 + int(mi) * 60 + int(float(sec))


def main():
    payload = base_payload()
    TMP.mkdir(exist_ok=True)
    test = run(["yt-dlp", "--version"])
    if test.returncode != 0:
        payload["error"] = "yt-dlp не установлен"
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    cmd = ["yt-dlp", "--flat-playlist", "--dump-json", PLAYLIST_URL]
    proc = run(cmd)
    if proc.returncode != 0:
        payload["error"] = proc.stderr.strip() or "Не удалось получить плейлист"
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        meta = json.loads(line)
        vid = meta.get("id")
        title = meta.get("title") or vid
        url = f"https://www.youtube.com/watch?v={vid}"
        video = {"videoId": vid, "title": title, "url": url, "duration": meta.get("duration"), "language": None}
        payload["videos"].append(video)

        sproc = run(["yt-dlp", "--skip-download", "--write-auto-subs", "--sub-langs", "ru,en,ru.*,en.*,.*", "--sub-format", "vtt", "-o", str(TMP / "%(id)s.%(ext)s"), url])
        if sproc.returncode != 0:
            continue

        files = sorted(TMP.glob(f"{vid}*.vtt"))
        if not files:
            continue
        subfile = files[0]
        lang = subfile.stem.split(".")[-1] if "." in subfile.stem else "unknown"
        video["language"] = lang
        raw = subfile.read_text(encoding="utf-8", errors="ignore")
        cleaned = clean_vtt(raw)
        if not cleaned:
            continue
        for cidx, start, text in parse_vtt_chunks(raw):
            payload["chunks"].append({"id": f"{vid}-chunk-{cidx:03d}", "videoId": vid, "title": title, "url": url, "start": start, "text": text})

    payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
    if not payload["chunks"] and payload["error"] is None:
        payload["error"] = "Не удалось получить субтитры: проверьте ограничения YouTube или языки субтитров"
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        payload = base_payload()
        payload["error"] = str(exc)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
