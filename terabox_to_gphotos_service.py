"""
TeraBox to Google Photos Transfer Service
=========================================
Orchestrates sequential downloading of TeraBox account videos using the
existing bot resolver and uploading them to Google Photos with a mandatory
10s delay between transfers and a comprehensive final failure report.
"""

import os
import time
import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple, Callable, Awaitable

try:
    from config import TRANSFER_DELAY_SEC
except ImportError:
    TRANSFER_DELAY_SEC = int(os.getenv("TRANSFER_DELAY_SEC", "10"))
from downloader import format_bytes, remove_file_safely
from terabox_account_manager import resolve_and_download_account_video, fetch_account_videos
from google_photos_manager import (
    is_photos_authenticated,
    upload_video_to_google_photos,
    record_transferred_video,
    get_known_google_photos_files,
    get_video_match_keys,
)

logger = logging.getLogger(__name__)

_TRANSFER_LOCK = asyncio.Lock()
_CURRENT_TRANSFER_TASK: Optional[asyncio.Task] = None
_CANCEL_REQUESTED = False
_SKIP_CURRENT_ITEM = False
_CURRENT_ITEM_TASK: Optional[asyncio.Task] = None


def is_transfer_in_progress() -> bool:
    """Checks if a transfer operation is currently active."""
    return _CURRENT_TRANSFER_TASK is not None and not _CURRENT_TRANSFER_TASK.done()


def cancel_current_transfer():
    """Signals cancellation to the ongoing transfer."""
    global _CANCEL_REQUESTED, _CURRENT_ITEM_TASK
    _CANCEL_REQUESTED = True
    if _CURRENT_ITEM_TASK and not _CURRENT_ITEM_TASK.done():
        _CURRENT_ITEM_TASK.cancel()
    if _CURRENT_TRANSFER_TASK and not _CURRENT_TRANSFER_TASK.done():
        _CURRENT_TRANSFER_TASK.cancel()


def skip_current_transfer() -> bool:
    """
    Skips the currently transferring file, aborting its download/upload and advancing to the next file.
    Residual files are immediately purged from disk and memory.
    """
    global _SKIP_CURRENT_ITEM, _CURRENT_ITEM_TASK
    _SKIP_CURRENT_ITEM = True
    if _CURRENT_ITEM_TASK and not _CURRENT_ITEM_TASK.done():
        _CURRENT_ITEM_TASK.cancel()
        return True
    return False


async def execute_transfer_job(
    videos: List[Dict[str, Any]],
    status_updater: Optional[Callable[[str], Awaitable[None]]] = None,
    delay_sec: int = TRANSFER_DELAY_SEC,
) -> Dict[str, Any]:
    """
    Executes sequential transfer of video items:
      1. Resolves & downloads video via existing bot resolver
      2. Uploads video to Google Photos
      3. Safely cleans up local downloaded file
      4. Waits `delay_sec` (10s) before processing the next video
      5. Emits final report with all failed file names at the end
    """
    global _CANCEL_REQUESTED, _CURRENT_TRANSFER_TASK, _SKIP_CURRENT_ITEM, _CURRENT_ITEM_TASK
    _CANCEL_REQUESTED = False
    _SKIP_CURRENT_ITEM = False
    _CURRENT_TRANSFER_TASK = asyncio.current_task()

    total_videos = len(videos)
    successful = []
    failed = []
    total_transferred_bytes = 0

    async def _safe_update(msg: str, is_finished: bool = False):
        if status_updater:
            try:
                try:
                    await status_updater(msg, is_finished=is_finished)
                except TypeError:
                    await status_updater(msg)
            except Exception as e:
                logger.debug(f"[Transfer Service] Status updater notice: {e}")

    try:
        # Check authentication
        auth_ok, auth_msg = is_photos_authenticated()
        if not auth_ok:
            return {
                "success": False,
                "error": "Google Photos is not authenticated. Please run `/gphotos_auth` first.",
                "successful": [],
                "failed": [],
            }

        await _safe_update(
            f"🚀 **Starting Transfer to Google Photos**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Queue:** `{total_videos}` videos\n"
            f"⏱️ **Cooldown Between Transfers:** `{delay_sec}s`\n\n"
            "Initializing pipeline..."
        )

        for index, item in enumerate(videos, 1):
            if _CANCEL_REQUESTED:
                logger.info("[Transfer Service] Transfer job cancelled by user request.")
                break

            fname = item.get("filename") or f"video_{item.get('fs_id', index)}.mp4"
            fsize = int(item.get("size") or 0)
            fsize_str = format_bytes(fsize)

            prefix = f"**[{index}/{total_videos}]** `{fname}` ({fsize_str})\n"
            downloaded_file = None
            _SKIP_CURRENT_ITEM = False

            async def _transfer_single_file():
                nonlocal downloaded_file, total_transferred_bytes
                await _safe_update(
                    f"{prefix}"
                    "⏳ **Step 1/2:** Initializing TeraBox multi-stage resolver pipeline..."
                )

                # Step 1: Download video via TeraBox resolver
                async def _dl_progress(p_txt: str):
                    await _safe_update(f"{prefix}{p_txt}")

                dl_ok, dl_file, dl_info, dl_err = await resolve_and_download_account_video(
                    item,
                    progress_updater=_dl_progress,
                )
                downloaded_file = dl_file

                if not dl_ok or not downloaded_file or not os.path.exists(downloaded_file):
                    err_msg = dl_err or "Resolver failed to download media."
                    logger.error(f"[Transfer Service] Download failed for {fname}: {err_msg}")
                    failed.append({
                        "filename": fname,
                        "fs_id": item.get("fs_id"),
                        "size": fsize,
                        "reason": f"TeraBox download error: {err_msg}",
                    })
                    await _safe_update(
                        f"{prefix}❌ **Download Failed:** {err_msg}\n"
                        f"⚠️ Moving to next file after cooldown..."
                    )
                    return False

                # Verify downloaded file size
                actual_size = os.path.getsize(downloaded_file)

                # Step 2: Upload to Google Photos
                await _safe_update(
                    f"{prefix}"
                    f"☁️ **Step 2/2:** Uploading to Google Photos ({format_bytes(actual_size)})..."
                )

                async def _up_progress(u_txt: str):
                    await _safe_update(f"{prefix}{u_txt}")

                up_ok, up_res, up_err = await upload_video_to_google_photos(
                    file_path=downloaded_file,
                    custom_filename=fname,
                    progress_updater=lambda txt: asyncio.create_task(_up_progress(txt)),
                )

                if not up_ok or not up_res:
                    err_msg = up_err or "Upload to Google Photos failed."
                    logger.error(f"[Transfer Service] Google Photos upload failed for {fname}: {err_msg}")
                    failed.append({
                        "filename": fname,
                        "fs_id": item.get("fs_id"),
                        "size": fsize,
                        "reason": f"Google Photos upload error: {err_msg}",
                    })
                    await _safe_update(
                        f"{prefix}❌ **Google Photos Upload Failed:** {err_msg}\n"
                        f"⚠️ Moving to next file after cooldown..."
                    )
                    return False

                total_transferred_bytes += actual_size
                item_fs_id = item.get("fs_id")
                photo_id = up_res.get("id")
                prod_url = up_res.get("product_url")

                successful.append({
                    "filename": fname,
                    "size": actual_size,
                    "id": photo_id,
                    "product_url": prod_url,
                })

                # Record to persistent registry
                try:
                    record_transferred_video(
                        fs_id=item_fs_id,
                        filename=fname,
                        size=actual_size,
                        google_id=photo_id,
                        product_url=prod_url,
                    )
                except Exception as reg_err:
                    logger.debug(f"[Transfer Service] Registry save error: {reg_err}")

                # Step 3: Immediately delete local file from server right after successful transfer
                if downloaded_file:
                    remove_file_safely(downloaded_file)
                    logger.info(f"[Transfer Service] Local file deleted from server: {downloaded_file}")
                    downloaded_file = None

                await _safe_update(
                    f"{prefix}✅ **Successfully transferred to Google Photos!** (Local file removed)"
                )
                return True

            item_task = asyncio.create_task(_transfer_single_file())
            _CURRENT_ITEM_TASK = item_task

            was_skipped = False
            try:
                await item_task
            except asyncio.CancelledError:
                if _SKIP_CURRENT_ITEM:
                    was_skipped = True
                    logger.info(f"[Transfer Service] Item {fname} was skipped by user request.")
                    failed.append({
                        "filename": fname,
                        "fs_id": item.get("fs_id"),
                        "size": fsize,
                        "reason": "Skipped by user",
                    })
                    await _safe_update(
                        f"{prefix}⏭️ **Skipped by User!**\n"
                        f"Residual files purged from memory and disk. Advancing to next video immediately..."
                    )
                else:
                    logger.info(f"[Transfer Service] Transfer job cancelled during {fname}.")
                    failed.append({
                        "filename": fname,
                        "fs_id": item.get("fs_id"),
                        "size": fsize,
                        "reason": "Transfer cancelled by user.",
                    })
                    break
            except Exception as item_err:
                logger.error(f"[Transfer Service] Unexpected error on {fname}: {item_err}", exc_info=True)
                failed.append({"filename": fname, "size": fsize, "reason": str(item_err)[:120]})
            finally:
                _CURRENT_ITEM_TASK = None
                # Purge any residual files from disk and force garbage collection
                if downloaded_file:
                    remove_file_safely(downloaded_file)
                    downloaded_file = None
                try:
                    from config import DOWNLOAD_DIR
                    fallback_file = DOWNLOAD_DIR / fname
                    if fallback_file.exists():
                        remove_file_safely(str(fallback_file))
                except Exception:
                    pass
                import gc
                gc.collect()

            # Step 4: 10-second timeout between transfers (skipped if user explicitly skipped the item)
            if index < total_videos and not _CANCEL_REQUESTED and not was_skipped:
                for remaining in range(delay_sec, 0, -2):
                    await _safe_update(
                        f"⏱️ **Cooldown:** Waiting `{remaining}s` before transferring next video...\n"
                        f"*(Completed: {len(successful)} | Failed: {len(failed)} | Total: {total_videos})*"
                    )
                    await asyncio.sleep(2)

        # Step 5: Final Summary Report (Failed files prominently shown at last)
        final_report = build_transfer_summary_text(
            total_queued=total_videos,
            successful=successful,
            failed=failed,
            total_transferred_bytes=total_transferred_bytes,
            was_cancelled=_CANCEL_REQUESTED,
        )

        await _safe_update(final_report, is_finished=True)

        return {
            "success": True,
            "total_queued": total_videos,
            "successful_count": len(successful),
            "failed_count": len(failed),
            "successful": successful,
            "failed": failed,
            "total_bytes": total_transferred_bytes,
            "summary_text": final_report,
        }
    finally:
        _CURRENT_TRANSFER_TASK = None
        _CURRENT_ITEM_TASK = None


def build_transfer_summary_text(
    total_queued: int,
    successful: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    total_transferred_bytes: int,
    was_cancelled: bool = False,
) -> str:
    """
    Constructs the final Telegram report. All failed files are shown at the end.
    """
    title = "🛑 **Transfer Cancelled by User**" if was_cancelled else "🏁 **TeraBox to Google Photos Transfer Finished!**"
    
    succ_lines = []
    for idx, s in enumerate(successful[:15], 1):
        succ_lines.append(f"  {idx}. ✅ `{s['filename']}` ({format_bytes(s['size'])})")
    if len(successful) > 15:
        succ_lines.append(f"  ... and {len(successful) - 15} more successfully uploaded.")

    fail_lines = []
    for idx, f in enumerate(failed, 1):
        reason = f.get("reason", "Unknown error")
        fail_lines.append(f"  {idx}. ❌ `{f['filename']}` ({format_bytes(f.get('size', 0))})\n     ↳ *Reason:* {reason}")

    report = (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **Transfer Results:**\n"
        f"• **Total Queued:** `{total_queued}` video files\n"
        f"• **Successfully Transferred:** `{len(successful)}` files (`{format_bytes(total_transferred_bytes)}`)\n"
        f"• **Failed:** `{len(failed)}` files\n\n"
    )

    if successful:
        report += (
            "✅ **Successfully Uploaded to Google Photos:**\n"
            + "\n".join(succ_lines)
            + "\n\n"
        )
    else:
        report += "⚠️ **No files were successfully transferred.**\n\n"

    # Failed files explicitly listed at the end
    if failed:
        report += (
            "❌ **Failed Files List:**\n"
            + "\n".join(fail_lines)
            + "\n\n"
            "💡 *Tip: Check failure reasons above. You can retry with `/teratransfer`.*"
        )
    else:
        report += "🎉 **100% of video files transferred with zero failures!**"

    return report


async def check_terabox_vs_google_photos(
    videos: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Cross-references videos from TeraBox account against Google Photos (API + local registry).
    Returns:
      {
        "success": bool,
        "error": Optional[str],
        "total_terabox": int,
        "total_terabox_size": int,
        "total_terabox_size_formatted": str,
        "present_count": int,
        "present_size": int,
        "present_size_formatted": str,
        "missing_count": int,
        "missing_size": int,
        "missing_size_formatted": str,
        "missing_videos": List[Dict[str, Any]],
        "present_videos": List[Dict[str, Any]],
        "all_videos": List[Dict[str, Any]],
      }
    """
    if videos is None:
        fetch_res = await fetch_account_videos()
        if not fetch_res.get("success"):
            return {
                "success": False,
                "error": fetch_res.get("error") or "Failed to fetch videos from TeraBox.",
                "total_terabox": 0,
                "total_terabox_size": 0,
                "total_terabox_size_formatted": "0 B",
                "present_count": 0,
                "present_size": 0,
                "present_size_formatted": "0 B",
                "missing_count": 0,
                "missing_size": 0,
                "missing_size_formatted": "0 B",
                "missing_videos": [],
                "present_videos": [],
                "all_videos": [],
            }
        videos = fetch_res.get("videos", [])

    known_fs_ids, known_match_keys, scope_warning = await get_known_google_photos_files()

    present_videos = []
    missing_videos = []

    for v in videos:
        fs_id_str = str(v.get("fs_id", "")).strip()
        fname = (v.get("filename") or "").strip()
        path = (v.get("path") or "").strip()

        # Resilient match keys for TeraBox video
        v_keys = get_video_match_keys(fname)
        if path:
            v_keys |= get_video_match_keys(path)

        # Match either by TeraBox fs_id or by intersection with known match keys
        if (fs_id_str and fs_id_str in known_fs_ids) or (v_keys and bool(v_keys & known_match_keys)):
            present_videos.append(v)
        else:
            missing_videos.append(v)

    total_size = sum(int(v.get("size") or 0) for v in videos)
    present_size = sum(int(v.get("size") or 0) for v in present_videos)
    missing_size = sum(int(v.get("size") or 0) for v in missing_videos)

    return {
        "success": True,
        "error": None,
        "warning": scope_warning,
        "total_terabox": len(videos),
        "total_terabox_size": total_size,
        "total_terabox_size_formatted": format_bytes(total_size) if total_size > 0 else "0 B",
        "present_count": len(present_videos),
        "present_size": present_size,
        "present_size_formatted": format_bytes(present_size) if present_size > 0 else "0 B",
        "missing_count": len(missing_videos),
        "missing_size": missing_size,
        "missing_size_formatted": format_bytes(missing_size) if missing_size > 0 else "0 B",
        "missing_videos": missing_videos,
        "present_videos": present_videos,
        "all_videos": videos,
    }
