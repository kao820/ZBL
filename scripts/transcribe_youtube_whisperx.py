#!/usr/bin/env python3
"""
WhisperX-based long-video transcription helper (chunked + resumable).
Designed as an optional per-video worker for playlist-video-transcripts/<videoId>.json.
"""

import os
import sys
import json
import shutil
import logging
import warnings
import argparse
import subprocess
from pathlib import Path

os.environ["PYTORCH_NO_PYNVML"] = "1"
os.environ["PYTORCH_NO_TF32_WARNING"] = "1"
warnings.filterwarnings("ignore", message=".*torchcodec.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
warnings.filterwarnings("ignore", category=FutureWarning)

import torch
import yt_dlp
import whisperx
from whisperx.diarize import DiarizationPipeline

from scripts.progress_tracker import save_progress, load_progress

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout, force=True)
logger = logging.getLogger(__name__)


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def download_audio(url: str, temp_dir: Path, cookie_file: str | None = None, proxy: str | None = None) -> Path:
    out = temp_dir / "source_audio.mp3"
    if out.exists() and out.stat().st_size > 1_000_000:
        return out
    opts = {
        "format": "18/best[acodec!=none]/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "outtmpl": str(temp_dir / "source_audio.%(ext)s"),
        "extractor_args": {"youtube": {"player_client": ["android"], "player_js_version": ["actual"]}},
        "remote_components": {"ejs": "github"},
        "retries": 10,
        "socket_timeout": 30,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "quiet": True,
        "no_warnings": True,
    }
    if proxy:
        opts["proxy"] = proxy
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if not out.exists():
        raise RuntimeError("source_audio.mp3 was not created")
    return out


def split_into_chunks(audio_path: Path, temp_dir: Path, chunk_min: int) -> list[Path]:
    existing = sorted(temp_dir.glob("chunk_*.wav"))
    if existing:
        return existing
    cmd = ["ffmpeg", "-i", str(audio_path), "-f", "segment", "-segment_time", str(chunk_min * 60), "-c:a", "pcm_s16le", "-ac", "1", "-ar", "16000", "-reset_timestamps", "1", str(temp_dir / "chunk_%03d.wav"), "-y"]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-1000:])
    return sorted(temp_dir.glob("chunk_*.wav"))


def process_chunk(chunk_path: Path, idx: int, total: int, offset: float, model, align_model, align_meta, diar_model, device: str, batch_size: int) -> list[dict]:
    tag = f"[{idx+1}/{total}]"
    audio_file = str(chunk_path)
    logger.info(f"{tag} transcribe")
    result = model.transcribe(audio_file, batch_size=batch_size, language="ru", print_progress=False)
    segments = result.get("segments", [])
    if not segments:
        return []
    logger.info(f"{tag} align")
    try:
        aligned = whisperx.align(segments, align_model, align_meta, audio_file, device, return_char_alignments=False)
    except TypeError:
        aligned = whisperx.align(segments, align_model, align_meta, audio_file, device)
    logger.info(f"{tag} diarize")
    diar = diar_model(audio_file, min_speakers=2, max_speakers=10)
    final = whisperx.assign_word_speakers(diar, {"segments": aligned.get("segments", segments)})
    out = []
    for s in final.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        out.append({"start": s.get("start", 0) + offset, "end": s.get("end", 0) + offset, "speaker": s.get("speaker", "SPEAKER_??"), "text": text})
    return out


def write_per_video_json(segments: list[dict], video_id: str, title: str, url: str, output_dir: Path):
    payload = {
        "video": {"videoId": video_id, "title": title, "url": url, "transcriptSource": "whisperx"},
        "chunks": [{"id": f"{video_id}-chunk-{i+1:03d}", "videoId": video_id, "title": title, "url": url, "start": int(seg.get("start", 0)), "text": seg.get("text", "")} for i, seg in enumerate(sorted(segments, key=lambda x: x["start"]))],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{video_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    ap.add_argument("--chunk-min", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=24)
    ap.add_argument("--temp-dir", default="whisperx_temp")
    ap.add_argument("--cookie-file", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--output-dir", default="playlist-video-transcripts")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")
    if not args.hf_token:
        raise RuntimeError("HF_TOKEN is required")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    temp_dir = Path(args.temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    audio = download_audio(args.source, temp_dir, args.cookie_file, args.proxy)
    chunks = split_into_chunks(audio, temp_dir, args.chunk_min)

    model = whisperx.load_model("large-v3", device, compute_type=compute_type, language="ru")
    align_model, align_meta = whisperx.load_align_model(language_code="ru", device=device)
    diar_model = DiarizationPipeline(token=args.hf_token, device=torch.device(device))

    processed = load_progress(temp_dir)
    all_segments = []
    for i, c in enumerate(chunks):
        if i in processed:
            continue
        segs = process_chunk(c, i, len(chunks), i * args.chunk_min * 60, model, align_model, align_meta, diar_model, device, args.batch_size)
        all_segments.extend(segs)
        processed.append(i)
        save_progress(temp_dir, processed)
        logger.info(f"saved progress {len(processed)}/{len(chunks)}")

    if not all_segments:
        logger.warning("no segments")
        return
    write_per_video_json(all_segments, args.video_id, args.title or args.video_id, args.source, Path(args.output_dir))
    logger.info(f"done {args.video_id}, segments={len(all_segments)}, last={fmt_time(max(s['end'] for s in all_segments))}")


if __name__ == "__main__":
    main()
