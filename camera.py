from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

from config import (
    CAMERA_CAPTURE_TIMEOUT_SECONDS,
    CAMERA_HEIGHT,
    CAMERA_ROTATION,
    CAMERA_SNAPSHOTS_DIR,
    CAMERA_WIDTH,
)


class CameraError(RuntimeError):
    pass


@dataclass
class CameraCaptureResult:
    captured_at: str
    latest_filename: str
    archived_filename: str
    width: int
    height: int


def _require_camera_binary() -> str:
    binary = shutil.which("rpicam-still")
    if not binary:
        raise CameraError("rpicam-still is not installed or not on PATH")
    return binary


def _snapshot_paths(timestamp_utc: datetime) -> tuple[Path, Path]:
    CAMERA_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_path = CAMERA_SNAPSHOTS_DIR / "latest.jpg"
    archived_name = f"snapshot-{timestamp_utc.strftime('%Y%m%dT%H%M%SZ')}.jpg"
    archived_path = CAMERA_SNAPSHOTS_DIR / archived_name
    return latest_path, archived_path


def capture_snapshot() -> CameraCaptureResult:
    binary = _require_camera_binary()
    timestamp_utc = datetime.now(timezone.utc)
    latest_path, archived_path = _snapshot_paths(timestamp_utc)

    command = [
        binary,
        "-n",
        "-t",
        "1",
        "--width",
        str(CAMERA_WIDTH),
        "--height",
        str(CAMERA_HEIGHT),
        "--rotation",
        str(CAMERA_ROTATION),
        "-o",
        str(latest_path),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CameraError(f"camera capture timed out after {CAMERA_CAPTURE_TIMEOUT_SECONDS}s") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise CameraError(stderr or "camera capture failed")

    if not latest_path.exists():
        raise CameraError("camera capture completed but no image was written")

    archived_path.write_bytes(latest_path.read_bytes())

    return CameraCaptureResult(
        captured_at=timestamp_utc.isoformat(),
        latest_filename=latest_path.name,
        archived_filename=archived_path.name,
        width=CAMERA_WIDTH,
        height=CAMERA_HEIGHT,
    )


def latest_snapshot_info() -> dict:
    latest_path = CAMERA_SNAPSHOTS_DIR / "latest.jpg"
    if not latest_path.exists():
        return {
            "available": False,
            "captured_at": None,
            "latest_url": None,
            "archived_filename": None,
        }

    captured_at = datetime.fromtimestamp(latest_path.stat().st_mtime, timezone.utc).isoformat()
    return {
        "available": True,
        "captured_at": captured_at,
        "latest_url": f"/camera/latest.jpg?ts={int(latest_path.stat().st_mtime)}",
        "archived_filename": latest_path.name,
    }
