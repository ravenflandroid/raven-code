from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def extract_frames(
    video_path: Path,
    output_dir: Path,
    *,
    sample_count: int = 12,
    ffmpeg_path: str = "ffmpeg",
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which(ffmpeg_path):
        pattern = output_dir / "frame_%03d.jpg"
        vf = f"select='not(mod(n\\,{max(1, sample_count)}))',scale=720:-1"
        subprocess.run(
            [ffmpeg_path, "-y", "-i", str(video_path), "-vf", vf, "-frames:v", str(sample_count), str(pattern)],
            check=True,
            capture_output=True,
            text=True,
        )
        return sorted(output_dir.glob("frame_*.jpg"))

    try:
        import cv2  # type: ignore
    except Exception as exc:
        raise RuntimeError("Install ffmpeg or opencv-python to extract video frames") from exc

    cap = cv2.VideoCapture(str(video_path))
    total = max(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 1)
    step = max(total // sample_count, 1)
    frames: list[Path] = []
    index = 0
    emitted = 0
    while emitted < sample_count:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok:
            break
        out = output_dir / f"frame_{emitted:03d}.jpg"
        cv2.imwrite(str(out), frame)
        frames.append(out)
        emitted += 1
        index += step
    cap.release()
    return frames

