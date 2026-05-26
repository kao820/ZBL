# progress_tracker.py
import json
from pathlib import Path

def save_progress(temp_dir: Path, processed_chunks: list[int]):
    (temp_dir / "progress.json").write_text(json.dumps({"processed": processed_chunks}), encoding="utf-8")

def load_progress(temp_dir: Path) -> list[int]:
    try:
        data = json.loads((temp_dir / "progress.json").read_text(encoding="utf-8"))
        return data.get("processed", [])
    except Exception:
        return []

