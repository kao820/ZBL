#!/usr/bin/env python3
"""
YouTube Audio Transcriber with WhisperX
Оптимизировано для файлов 4+ часа с чанками по 30 минут.

Порядок обработки каждого чанка:
  1. transcribe
  2. align
  3. diarize
  4. assign_word_speakers
"""

import os
import sys
import warnings

os.environ["PYTORCH_NO_PYNVML"] = "1"
os.environ["PYTORCH_NO_TF32_WARNING"] = "1"
warnings.filterwarnings("ignore", message=".*torchcodec.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
warnings.filterwarnings("ignore", category=FutureWarning)

import argparse
import logging
import subprocess
import shutil
import json
from pathlib import Path

import torch
import yt_dlp
import whisperx
from whisperx.diarize import DiarizationPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout, force=True)
logger = logging.getLogger(__name__)


def save_progress(temp_dir: Path, processed: list[int], segments: list[dict]):
    data = {"processed": processed, "segments": segments}
    path = temp_dir / "progress.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_progress(temp_dir: Path) -> tuple[list[int], list[dict]]:
    try:
        data = json.loads((temp_dir / "progress.json").read_text(encoding="utf-8"))
        return data.get("processed", []), data.get("segments", [])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return [], []


def fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def convert_cookies_json_to_netscape(json_path: str) -> str:
    with open(json_path, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    netscape_path = json_path + ".netscape.txt"
    with open(netscape_path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = str(int(c.get("expirationDate", 0)))
            f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{c.get('name','')}\t{c.get('value','')}\n")
    return netscape_path


def download_audio(url: str, temp_dir: Path, cookie_file: str | None = None, proxy: str | None = None) -> Path:
    audio_path = temp_dir / "source_audio.mp3"
    if audio_path.exists() and audio_path.stat().st_size > 1_000_000:
        return audio_path

    ydl_opts: dict = {
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
        ydl_opts["proxy"] = proxy
    if cookie_file:
        cookie_path = convert_cookies_json_to_netscape(cookie_file) if cookie_file.endswith(".json") else cookie_file
        ydl_opts["cookiefile"] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if not audio_path.exists():
        raise RuntimeError("source_audio.mp3 not created")
    return audio_path


def split_into_chunks(audio_path: Path, temp_dir: Path, chunk_min: int) -> list[Path]:
    existing = sorted(temp_dir.glob("chunk_*.wav"))
    if existing:
        return existing
    result = subprocess.run([
        "ffmpeg", "-i", str(audio_path), "-f", "segment", "-segment_time", str(chunk_min * 60),
        "-c:a", "pcm_s16le", "-ac", "1", "-ar", "16000", "-reset_timestamps", "1",
        str(temp_dir / "chunk_%03d.wav"), "-y"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:])
    return sorted(temp_dir.glob("chunk_*.wav"))


def process_chunk(chunk_path: Path, chunk_index: int, total_chunks: int, offset_sec: float,
                  model, align_model, align_metadata, diarize_model, device: str, batch_size: int,
                  min_speakers: int, max_speakers: int) -> list[dict]:
    tag = f"[{chunk_index + 1}/{total_chunks}]"
    audio_file = str(chunk_path)
    result = model.transcribe(audio_file, batch_size=batch_size, language="ru", print_progress=False)
    segments = result.get("segments", [])
    if not segments:
        return []
    try:
        aligned = whisperx.align(segments, align_model, align_metadata, audio_file, device, return_char_alignments=False)
    except TypeError:
        aligned = whisperx.align(segments, align_model, align_metadata, audio_file, device)
    diarize_result = diarize_model(audio_file, min_speakers=min_speakers, max_speakers=max_speakers)
    final = whisperx.assign_word_speakers(diarize_result, {"segments": aligned.get("segments", segments)})

    out = []
    for seg in final.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": seg.get("start", 0) + offset_sec,
            "end": seg.get("end", 0) + offset_sec,
            "speaker": seg.get("speaker", "SPEAKER_??"),
            "text": text,
        })
    logger.info(f"{tag} done: {len(out)}")
    return out


def write_per_video_json(segments: list[dict], video_id: str, title: str, url: str, output_dir: Path):
    segments.sort(key=lambda x: x["start"])
    payload = {
        "updatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "video": {"videoId": video_id, "title": title, "url": url, "transcriptSource": "whisperx"},
        "chunks": [{
            "id": f"{video_id}-chunk-{i+1:03d}",
            "videoId": video_id,
            "title": title,
            "url": url,
            "start": int(seg.get("start", 0)),
            "text": seg.get("text", ""),
        } for i, seg in enumerate(segments)]
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{video_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("source")
    p.add_argument("--video-id", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--hf-token", default=os.getenv("HF_TOKEN"))
    p.add_argument("--chunk-min", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--proxy", default=None)
    p.add_argument("--cookie-file", default=None)
    p.add_argument("--temp-dir", default="whisperx_temp")
    p.add_argument("--min-speakers", type=int, default=2)
    p.add_argument("--max-speakers", type=int, default=10)
    p.add_argument("--output-dir", default="playlist-video-transcripts")
    return p.parse_args()


def main():
    args = parse_args()
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")
    if not args.hf_token:
        raise RuntimeError("HF_TOKEN is required")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    temp_dir = Path(args.temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    audio_path = download_audio(args.source, temp_dir, args.cookie_file, args.proxy) if args.source.lower() != "local" else (temp_dir / "source_audio.mp3")
    if not audio_path.exists():
        raise RuntimeError(f"audio not found: {audio_path}")

    chunks = split_into_chunks(audio_path, temp_dir, args.chunk_min)
    model = whisperx.load_model("large-v3", device, compute_type=compute_type, language="ru", vad_options={"vad_onset": 0.45, "vad_offset": 0.35})
    align_model, align_meta = whisperx.load_align_model(language_code="ru", device=device)
    diar_model = DiarizationPipeline(token=args.hf_token, device=torch.device(device))

    processed, all_segments = load_progress(temp_dir)
    for i, chunk_path in enumerate(chunks):
        if i in processed:
            continue
        segs = process_chunk(chunk_path, i, len(chunks), i * args.chunk_min * 60, model, align_model, align_meta, diar_model, device, args.batch_size, args.min_speakers, args.max_speakers)
        all_segments.extend(segs)
        processed.append(i)
        save_progress(temp_dir, processed, all_segments)

    if not all_segments:
        logger.warning("No segments")
        return

    write_per_video_json(all_segments, args.video_id, args.title or args.video_id, args.source, Path(args.output_dir))
    logger.info(f"Done {args.video_id}: {len(all_segments)} segments, end={fmt_time(max(s['end'] for s in all_segments))}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)
