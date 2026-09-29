"""
Video Streaming Helper for Telegram
===================================
Optimizes video files for progressive in-app streaming in Telegram clients.

Why Telegram requires downloading the whole file before playing:
1. In MP4 containers, the `moov` atom (index, frame offsets, metadata) is by
   default written at the END of the file by encoders. Telegram players cannot
   start playing until the `moov` atom is read.
2. Incompatible containers like `.mkv` or `.webm` cannot be streamed natively
   in Telegram's in-app player.
3. Missing video dimensions (`width`, `height`), duration, and thumbnail cause
   Telegram to treat the file as a generic document rather than a streamable video.

This module provides:
- FastStart remuxing (`-c copy -movflags +faststart` in 1-2s without re-encoding)
- Automatic container normalization to `.mp4`
- Video thumbnail extraction
- Video dimension & duration probing
"""

import os
import re
import json
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from downloader import remove_file_safely

logger = logging.getLogger(__name__)


def _find_binary(name: str) -> Optional[str]:
    """Finds binary executable in PATH or static_ffmpeg."""
    path = shutil.which(name)
    if path:
        return path
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        path = shutil.which(name)
        if path:
            return path
    except Exception:
        pass
    return None


def is_ffmpeg_available() -> bool:
    """Checks if ffmpeg is available in the current environment."""
    return _find_binary("ffmpeg") is not None


def get_video_metadata(file_path: str) -> Dict[str, Any]:
    """
    Extracts video duration (seconds), width, height, and codec information.
    Uses ffprobe if available, with regex fallback on ffmpeg stderr.
    """
    metadata: Dict[str, Any] = {
        "duration": None,
        "width": None,
        "height": None,
        "video_codec": None,
        "audio_codec": None,
    }
    if not os.path.exists(file_path):
        return metadata

    ffprobe_bin = _find_binary("ffprobe")
    if ffprobe_bin:
        try:
            cmd = [
                ffprobe_bin,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                file_path,
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                # Parse format duration
                format_info = data.get("format", {})
                if "duration" in format_info:
                    try:
                        metadata["duration"] = int(float(format_info["duration"]))
                    except (ValueError, TypeError):
                        pass

                # Parse streams
                for stream in data.get("streams", []):
                    codec_type = stream.get("codec_type")
                    if codec_type == "video" and not metadata["width"]:
                        metadata["width"] = stream.get("width")
                        metadata["height"] = stream.get("height")
                        metadata["video_codec"] = stream.get("codec_name")
                        if not metadata["duration"] and "duration" in stream:
                            try:
                                metadata["duration"] = int(float(stream["duration"]))
                            except (ValueError, TypeError):
                                pass
                    elif codec_type == "audio" and not metadata["audio_codec"]:
                        metadata["audio_codec"] = stream.get("codec_name")

                return metadata
        except Exception as e:
            logger.debug(f"[Video Prober] ffprobe failed: {e}")

    # Fallback to ffmpeg -i
    ffmpeg_bin = _find_binary("ffmpeg")
    if ffmpeg_bin:
        try:
            cmd = [ffmpeg_bin, "-i", file_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            stderr = res.stderr or ""

            # Duration: 00:01:23.45
            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", stderr)
            if dur_match:
                hours, mins, secs = dur_match.groups()
                metadata["duration"] = int(int(hours) * 3600 + int(mins) * 60 + float(secs))

            # Video: ..., 1920x1080 [SAR ...], ...
            res_match = re.search(r"Stream.*Video:.*,\s*(\d{2,5})x(\d{2,5})", stderr)
            if res_match:
                metadata["width"] = int(res_match.group(1))
                metadata["height"] = int(res_match.group(2))
        except Exception as e:
            logger.debug(f"[Video Prober] ffmpeg fallback failed: {e}")

    return metadata


def generate_video_thumbnail(file_path: str, output_path: Optional[str] = None) -> Optional[str]:
    """
    Extracts a frame at ~1.0s and scales it to max 320px width for Telegram thumbnail.
    """
    ffmpeg_bin = _find_binary("ffmpeg")
    if not ffmpeg_bin or not os.path.exists(file_path):
        return None

    if not output_path:
        base = Path(file_path).stem
        parent = Path(file_path).parent
        output_path = str(parent / f"thumb_{base}.jpg")

    try:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss", "00:00:01",
            "-i", file_path,
            "-vframes", "1",
            "-vf", "scale='min(320,iw)':-1",
            "-q:v", "2",
            output_path,
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as e:
        logger.debug(f"[Video Prober] Thumbnail generation failed: {e}")

    if output_path and os.path.exists(output_path):
        remove_file_safely(output_path)
    return None


def ensure_faststart_and_streamable(file_path: str) -> Tuple[str, bool]:
    """
    Remuxes video container to MP4 with `+faststart` (moving the `moov` atom to the head).
    Uses `-c copy` (zero re-encoding) which takes ~1-2 seconds even for 1-2GB files.

    Returns:
        (streamable_file_path, is_remuxed)
    """
    ffmpeg_bin = _find_binary("ffmpeg")
    if not ffmpeg_bin or not os.path.exists(file_path):
        return file_path, False

    p = Path(file_path)
    # Target output path with .mp4 extension and _faststart marker
    out_path = str(p.parent / f"{p.stem}_faststart.mp4")

    try:
        # Remux with stream copy and faststart
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", file_path,
            "-c", "copy",
            "-movflags", "+faststart",
            out_path,
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            logger.info(f"⚡ [Video FastStart] Successfully remuxed {p.name} -> {Path(out_path).name} for Telegram streaming!")
            return out_path, True
        else:
            logger.warning(f"[Video FastStart] Remux with -c copy returned code {res.returncode}. Using original file.")
            if os.path.exists(out_path):
                remove_file_safely(out_path)
    except Exception as e:
        logger.warning(f"[Video FastStart] Failed to remux {file_path}: {e}")
        if os.path.exists(out_path):
            remove_file_safely(out_path)

    return file_path, False


def prepare_video_for_telegram(file_path: str) -> Dict[str, Any]:
    """
    Complete preparation pipeline to make any video instantly streamable in Telegram:
      1. Probes duration, width, height.
      2. Remuxes to MP4 with +faststart (`moov` atom at start) in 1-2 seconds.
      3. Generates high-quality thumbnail image.

    Returns a dict with:
      - file_path: str (path to streamable file)
      - thumbnail_path: Optional[str]
      - duration: Optional[int]
      - width: Optional[int]
      - height: Optional[int]
      - is_remuxed: bool
    """
    result: Dict[str, Any] = {
        "file_path": file_path,
        "thumbnail_path": None,
        "duration": None,
        "width": None,
        "height": None,
        "is_remuxed": False,
    }

    if not file_path or not os.path.exists(file_path):
        return result

    # 1. Probe original metadata
    meta = get_video_metadata(file_path)
    result["duration"] = meta.get("duration")
    result["width"] = meta.get("width")
    result["height"] = meta.get("height")

    # 2. FastStart Remuxing (repositions moov atom to front)
    streamable_path, is_remuxed = ensure_faststart_and_streamable(file_path)
    result["file_path"] = streamable_path
    result["is_remuxed"] = is_remuxed

    # 3. Generate thumbnail from the streamable file
    thumb = generate_video_thumbnail(streamable_path)
    result["thumbnail_path"] = thumb

    return result
