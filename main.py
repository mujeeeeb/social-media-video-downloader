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
    # Handle literal escaped '\n' strings from env vars and ensure clean output
    cleaned_cookies = cookies_content.replace("\\n", "\n").strip()
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(cleaned_cookies + "\n")
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
    }

    if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
        opts["cookiefile"] = COOKIES_PATH
        # Desktop browser cookies must match the web/mweb client requests
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["web", "mweb"]
            }
        }
    else:
        # Fallback clients when no cookies are provided
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "tv"]
            }
        }

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
    The client uses this to show the user only qualities that truly exist.
    """
    try:
        opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_formats = info.get("formats", [])
        duration = info.get("duration")  # seconds, may be None

        def estimate_filesize(fmt: dict) -> int | None:
            """Best-effort filesize in bytes for a single format dict."""
            filesize = fmt.get("filesize") or fmt.get("filesize_approx")
            if filesize:
                return int(filesize)

            bitrate = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
            if bitrate and duration:
                return int(bitrate * 1000 / 8 * duration)

            return None

        # ------------------------------------------------------------
        # VIDEO FORMATS
        # ------------------------------------------------------------
        best_by_height: dict[int, dict] = {}

        for f in raw_formats:
            height = f.get("height")
            vcodec = f.get("vcodec", "none")

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

        video_formats.sort(key=lambda x: x["height"], reverse=True)

        # ------------------------------------------------------------
        # AUDIO FORMATS
        # ------------------------------------------------------------
        best_by_abr: dict[int, dict] = {}

        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")

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

        audio_formats.sort(key=lambda x: x["abr"], reverse=True)

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
        info_opts = {**get_ydl_base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_title = info.get("title", "video")
        safe_title = sanitize_filename(raw_title)
        is_audio = "audio" in format.lower() or format == "bestaudio/best"
        extension = "mp3" if is_audio else "mp4"
        filename = f"{safe_title}.{extension}"

        uid = uuid.uuid4().hex[:8]
        output_template = f"/tmp/{uid}.%(ext)s"

        if is_audio:
            format_chain = [format, "bestaudio/best", "best"]
        else:
            format_chain = [
                f"{format}+bestaudio/best",
                format,
                "best",
                "worst",
            ]

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
