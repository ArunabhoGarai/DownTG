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

from config import TRANSFER_DELAY_SEC
from downloader import format_bytes, remove_file_safely
from terabox_account_manager import resolve_and_download_account_video
from google_photos_manager import (
    is_photos_authenticated,
    upload_video_to_google_photos,
)

logger = logging.getLogger(__name__)

# State tracking for running transfers
_TRANSFER_LOCK = asyncio.Lock()
_CURRENT_TRANSFER_TASK: Optional[asyncio.Task] = None
_CANCEL_REQUESTED = False


def is_transfer_in_progress() -> bool:
    """Checks if a transfer operation is currently active."""
    return _CURRENT_TRANSFER_TASK is not None and not _CURRENT_TRANSFER_TASK.done()


def cancel_current_transfer():
    """Signals cancellation to the ongoing transfer."""
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    if _CURRENT_TRANSFER_TASK and not _CURRENT_TRANSFER_TASK.done():
        _CURRENT_TRANSFER_TASK.cancel()


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
    global _CANCEL_REQUESTED, _CURRENT_TRANSFER_TASK
    _CANCEL_REQUESTED = False

    total_videos = len(videos)
    successful = []
    failed = []
    total_transferred_bytes = 0

    async def _safe_update(msg: str):
        if status_updater:
            try:
                await status_updater(msg)
            except Exception as e:
                logger.debug(f"[Transfer Service] Status updater notice: {e}")

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

        await _safe_update(
            f"{prefix}"
            "⏳ **Step 1/2:** Resolving & downloading from TeraBox using bot resolver..."
        )

        # Step 1: Download video via TeraBox resolver
        downloaded_file = None
        try:
            async def _dl_progress(p_txt: str):
                await _safe_update(f"{prefix}📥 {p_txt}")

            dl_ok, downloaded_file, dl_info, dl_err = await resolve_and_download_account_video(
                item,
                progress_updater=lambda txt: asyncio.create_task(_dl_progress(txt)),
            )

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
                if index < total_videos and not _CANCEL_REQUESTED:
                    await asyncio.sleep(delay_sec)
                continue

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
            else:
                total_transferred_bytes += actual_size
                successful.append({
                    "filename": fname,
                    "size": actual_size,
                    "id": up_res.get("id"),
                    "product_url": up_res.get("product_url"),
                })
                # Step 3: Immediately delete local file from server right after successful transfer
                if downloaded_file:
                    remove_file_safely(downloaded_file)
                    logger.info(f"[Transfer Service] Local file deleted from server: {downloaded_file}")
                    downloaded_file = None

                await _safe_update(
                    f"{prefix}✅ **Successfully transferred to Google Photos!** (Local file removed)"
                )

        except asyncio.CancelledError:
            logger.info(f"[Transfer Service] Task cancelled during processing of {fname}.")
            failed.append({"filename": fname, "size": fsize, "reason": "Transfer cancelled by user."})
            break
        except Exception as item_err:
            logger.error(f"[Transfer Service] Unexpected error on {fname}: {item_err}", exc_info=True)
            failed.append({"filename": fname, "size": fsize, "reason": str(item_err)[:120]})
        finally:
            # Step 3 (Failsafe): Always delete local file immediately to free disk space
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

        # Step 4: 10-second timeout between transfers (as specifically requested)
        if index < total_videos and not _CANCEL_REQUESTED:
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

    await _safe_update(final_report)

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
