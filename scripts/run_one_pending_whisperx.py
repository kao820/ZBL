#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone

STATUS = Path("playlist-video-status.json")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def main() -> int:
    if not STATUS.exists():
        print("status file not found")
        return 0
    st = json.loads(STATUS.read_text(encoding='utf-8'))
    videos = st.get('videos') or []
    target = next((v for v in videos if v.get('status') in {'pending','retry'}), None)
    if not target:
        print('no pending/retry videos')
        return 0

    vid, url, title = target.get('videoId'), target.get('url'), target.get('title') or target.get('videoId')
    cmd = [sys.executable, 'scripts/transcribe_youtube_whisperx.py', url, '--video-id', vid, '--title', title, '--output-dir', 'playlist-video-transcripts']
    hf = os.getenv('HF_TOKEN','').strip()
    if hf:
        cmd += ['--hf-token', hf]

    p = subprocess.run(cmd)
    target['attempts'] = int(target.get('attempts',0)) + 1
    if p.returncode == 0 and Path(f'playlist-video-transcripts/{vid}.json').exists():
        target['status'] = 'indexed'
        target['lastError'] = None
    else:
        max_attempts = int(os.getenv('MAX_VIDEO_ATTEMPTS','5'))
        target['status'] = 'retry' if target['attempts'] < max_attempts else 'missing'
        target['lastError'] = f'whisperx_rc_{p.returncode}'
    target['updatedAt'] = now_iso()
    STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
