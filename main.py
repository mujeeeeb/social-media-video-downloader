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
        # Force yt-dlp to use the Android/iOS player clients, which
        # currently serve formats without requiring a PO token — this
        # avoids YouTube filtering out all formats for the web client.
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios"],
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
    Return all available video formats for a given URL.
    The Flutter app uses this to show the user only qualities that truly exist.
    """
    try:
        opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_formats = info.get("formats", [])

        # Build a clean list of unique video formats
        seen_heights = set()
        video_formats = []

        for f in raw_formats:
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            ext = f.get("ext", "mp4")

            # Skip audio-only and formats with no height info
            if vcodec == "none" or height is None:
                continue

            # Only keep one format per resolution (pick the best one)
            if height in seen_heights:
                continue

            seen_heights.add(height)
            video_formats.append({
                "format_id": f.get("format_id"),
                "height": height,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "label": f"{height}p",
            })

        # Sort from highest to lowest resolution
        video_formats.sort(key=lambda x: x["height"], reverse=True)

        # Always add an audio-only option
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
        if is_audio:
            format_chain = [format, "bestaudio/best", "best"]
        else:
            format_chain = [format, "best", "worst"]

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

        return StreamingResponse(
            iterfile(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size),  # Enables real progress bar in app
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
