#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLQ0wmPbdvhzJl6lFMAVzbneqAPtBvCrsg"
OUT = Path("quartz/static/playlist-transcripts.json")


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "command failed")
    return result.stdout


def clean(text):
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def chunk_words(text, size=130):
    words = text.split()
    for idx in range(0, len(words), size):
        yield " ".join(words[idx : idx + size])


def main():
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-subs",
        "--sub-langs",
        "ru.*,en.*",
        "--convert-subs",
        "vtt",
        "--print",
        "%(id)s\t%(title)s\t%(webpage_url)s\t%(requested_subtitles)s",
        PLAYLIST_URL,
    ]
    raw = run(cmd)

    chunks = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        video_id, title, url, subtitles = parts
        if subtitles in ("NA", "{}"):
            continue

        sub = run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-auto-subs",
                "--sub-langs",
                "ru.*,en.*",
                "--convert-subs",
                "vtt",
                "--sub-format",
                "vtt",
                "--print",
                "%(automatic_captions)s",
                url,
            ]
        )
        text = clean(sub)
        if not text:
            continue

        for idx, piece in enumerate(chunk_words(text), start=1):
            chunks.append(
                {
                    "videoId": video_id,
                    "title": title,
                    "url": url,
                    "start": f"chunk-{idx}",
                    "text": piece,
                }
            )

    OUT.write_text(
        json.dumps({"playlist": PLAYLIST_URL, "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"saved {len(chunks)} chunks -> {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
