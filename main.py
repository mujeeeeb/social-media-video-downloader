from __future__ import annotations

import os
import re
import uuid

import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Cookies setup (for bypassing YouTube bot detection) ---
COOKIES_PATH = "/tmp/cookies.txt"

cookies_content = os.getenv("YTDLP_COOKIES_CONTENT")
if cookies_content:
    with open(COOKIES_PATH, "w") as f:
        f.write(cookies_content)
# -------------------------------------------------------------


def sanitize_filename(title: str) -> str:
    """Remove emojis, non-ASCII chars, and illegal filename characters."""
    title = title.encode("ascii", "ignore").decode("ascii")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title)
    title = title.strip("-").strip() or "video"
    return title


def get_ydl_base_opts():
    """Base yt-dlp options shared across all endpoints."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        # "android"/"ios" previously bypassed YouTube's bot-check reliably
        # without cookies. YouTube has since tightened detection further,
        # so "tv" is added as an additional fallback client — it mimics a
        # smart TV app and has historically been more resistant to the
        # sign-in wall. We deliberately do NOT include "web" — without
        # valid cookies, "web" gets blocked and breaks /info and /formats
        # entirely (not just quality options).
    "extractor_args": {
    "youtube": {
        "player_client": ["android", "ios", "tv"],
    }
},
    }
    if os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    return opts


@app.get("/")
async def root():
    return {
        "message": (
            "Social Media Video Downloader API. "
            "Endpoints: /info, /formats, /download"
        )
    }


@app.get("/info")
async def video_info(url: str = Query(...)):
    """Return title and thumbnail URL for a given video URL (no download)."""
    try:
        opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        return {
            "title": info.get("title", "Video"),
            "thumbnail": info.get("thumbnail", ""),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching info: {str(e)}")


@app.get("/formats")
async def video_formats(url: str = Query(...)):
    """
    Return all available video and audio formats for a given URL.
    The Flutter app uses this to show the user only qualities that truly exist.
    """
    try:
        opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_formats = info.get("formats", [])

        # Duration (seconds) is used to *estimate* a real filesize for
        # formats where yt-dlp doesn't provide "filesize" or
        # "filesize_approx" directly (very common for adaptive/DASH
        # streams on YouTube). Without this, every such format falls
        # back to the same guessed size on the client, which is why
        # different qualities were showing identical MB values.
        duration = info.get("duration")  # seconds, may be None

        def estimate_filesize(fmt: dict) -> int | None:
            """Best-effort filesize in bytes for a single format dict."""
            filesize = fmt.get("filesize") or fmt.get("filesize_approx")
            if filesize:
                return int(filesize)

            # tbr = average total bitrate in Kbit/s (video formats) —
            # this is present on almost every yt-dlp format even when
            # filesize isn't, so we derive size from it directly.
            bitrate = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
            if bitrate and duration:
                return int(bitrate * 1000 / 8 * duration)

            return None

        # ------------------------------------------------------------
        # VIDEO FORMATS
        # ------------------------------------------------------------
        # Group by height and keep the *best* candidate per resolution
        # (highest bitrate), instead of just the first one yt-dlp
        # happened to list — otherwise a low-bitrate duplicate could
        # silently get chosen for a given resolution.
        best_by_height: dict[int, dict] = {}

        for f in raw_formats:
            height = f.get("height")
            vcodec = f.get("vcodec", "none")

            # Skip audio-only and formats with no height info
            if vcodec == "none" or height is None:
                continue

            tbr = f.get("tbr") or 0
            current_best = best_by_height.get(height)
            if current_best is None or tbr > (current_best.get("tbr") or 0):
                best_by_height[height] = f

        video_formats = []
        for height, f in best_by_height.items():
            video_formats.append({
                "format_id": f.get("format_id"),
                "height": height,
                "ext": f.get("ext", "mp4"),
                "filesize": estimate_filesize(f),
                "label": f"{height}p",
            })

        # Sort from highest to lowest resolution
        video_formats.sort(key=lambda x: x["height"], reverse=True)

        # ------------------------------------------------------------
        # AUDIO FORMATS — pulled from real yt-dlp audio-only streams
        # instead of a single hardcoded "Audio Only" placeholder, so the
        # app can show real bitrate options (e.g. "160kbps (m4a)")
        # with real, distinct sizes.
        # ------------------------------------------------------------
        best_by_abr: dict[int, dict] = {}

        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")

            # Only keep audio-only streams (no video track)
            if vcodec != "none" or acodec == "none":
                continue

            abr = f.get("abr") or f.get("tbr")
            if not abr:
                continue

            abr_rounded = round(abr)
            current_best = best_by_abr.get(abr_rounded)
            if current_best is None:
                best_by_abr[abr_rounded] = f

        audio_formats = []
        for abr_rounded, f in best_by_abr.items():
            ext = f.get("ext", "m4a")
            audio_formats.append({
                "format_id": f.get("format_id"),
                "height": 0,
                "ext": ext,
                "abr": abr_rounded,
                "filesize": estimate_filesize(f),
                "label": f"{abr_rounded}kbps ({ext})",
            })

        # Sort from highest to lowest bitrate
        audio_formats.sort(key=lambda x: x["abr"], reverse=True)

        # Fallback: if yt-dlp exposed no separate audio-only streams for
        # this platform (some sites only offer combined video+audio),
        # keep a generic option so audio download still works.
        if not audio_formats:
            audio_formats = [{
                "format_id": "bestaudio/best",
                "height": 0,
                "ext": "mp3",
                "filesize": None,
                "label": "Audio Only",
            }]

        return {
            "title": info.get("title", "Video"),
            "thumbnail": info.get("thumbnail", ""),
            "formats": video_formats + audio_formats,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching formats: {str(e)}")


@app.get("/download")
async def download_video(
    url: str = Query(...),
    format: str = Query("best"),
):
    """Download a video/audio by format selector and stream it back to the client."""
    try:
        # Step 1: fetch metadata only (fast)
        info_opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_title = info.get("title", "video")
        safe_title = sanitize_filename(raw_title)
        is_audio = "audio" in format.lower() or format == "bestaudio/best"
        extension = "mp3" if is_audio else "mp4"
        filename = f"{safe_title}.{extension}"

        # Step 2: download to /tmp
        uid = uuid.uuid4().hex[:8]
        output_template = f"/tmp/{uid}.%(ext)s"

        # Try the requested format first, then fall back to safer
        # selectors if that exact format isn't available for this video
        # (this commonly happens on Shorts / certain videos with a
        # limited format set).
        #
        # For video formats: most high-quality YouTube formats (1080p,
        # 720p, etc.) are video-only, so we merge them with the best
        # available audio track. "+bestaudio/best" means: "use this
        # format plus best audio; if that combo isn't available, just
        # use the format alone (it may already include audio)."
        if is_audio:
            format_chain = [format, "bestaudio/best", "best"]
        else:
            format_chain = [
                f"{format}+bestaudio/best",
                format,
                "best",
                "worst",
            ]

        # Remove duplicates while preserving order
        seen = set()
        format_chain = [f for f in format_chain if not (f in seen or seen.add(f))]

        last_error = None
        for attempt_format in format_chain:
            dl_opts = {
                **get_ydl_base_opts(),
                "format": attempt_format,
                "outtmpl": output_template,
                "merge_output_format": "mp4",
            }
            if is_audio:
                # Always actually extract/convert to real audio-only
                # mp3 using ffmpeg, regardless of what format yt-dlp
                # picked. Without this, platforms that have no true
                # audio-only stream would silently fall back to
                # downloading the full video while still labeling it
                # as .mp3 — producing a file that looks like audio
                # but is actually a video underneath.
                dl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]
            try:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([url])
                last_error = None
                break
            except Exception as e:
                last_error = e
                continue

        if last_error is not None:
            raise last_error

        # Step 3: find the downloaded file
        actual_file_path = None
        for f in os.listdir("/tmp"):
            if f.startswith(uid):
                actual_file_path = os.path.join("/tmp", f)
                break

        if not actual_file_path or not os.path.exists(actual_file_path):
            raise HTTPException(
                status_code=500,
                detail="Download failed: output file not found.",
            )

        file_size = os.path.getsize(actual_file_path)

        # Step 4: stream the file back and clean up after
        def iterfile():
            with open(actual_file_path, "rb") as f:
                yield from f
            os.unlink(actual_file_path)

        content_type = "audio/mpeg" if is_audio else "video/mp4"

        return StreamingResponse(
            iterfile(),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error during download: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
