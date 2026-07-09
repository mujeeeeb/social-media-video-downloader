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


def sanitize_filename(title: str) -> str:
    """Remove emojis, non-ASCII chars, and illegal filename characters."""
    # Strip non-ASCII (emojis, unicode symbols, etc.)
    title = title.encode("ascii", "ignore").decode("ascii")
    # Remove characters illegal in filenames
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title)
    title = title.strip("-").strip() or "video"
    return title


@app.get("/")
async def root():
    return {
        "message": (
            "Welcome to the Social Media Video Downloader API. "
            "Use /download?url=<video_url>&format=<video_format> to download videos. "
            "Use /info?url=<video_url> to fetch title and thumbnail."
        )
    }


@app.get("/info")
async def video_info(url: str = Query(...)):
    """Return title and thumbnail URL for a given video URL (no download)."""
    try:
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        return {
            "title": info.get("title", "Video"),
            "thumbnail": info.get("thumbnail", ""),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching info: {str(e)}")


@app.get("/download")
async def download_video(
    url: str = Query(...),
    format: str = Query("best"),
):
    """Download a video/audio and stream it back to the client."""
    try:
        # ── Step 1: fetch metadata only (fast) ──────────────────────────────
        ydl_opts_info = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_title = info.get("title", "video")
        safe_title = sanitize_filename(raw_title)
        extension = "mp4"
        filename = f"{safe_title}.{extension}"

        # ── Step 2: download to /tmp ─────────────────────────────────────────
        uid = uuid.uuid4().hex[:8]
        output_template = f"/tmp/{uid}.%(ext)s"

        ydl_opts_dl = {
            "format": format,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }

        with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
            ydl.download([url])

        # ── Step 3: find the downloaded file ────────────────────────────────
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

        # ── Step 4: stream the file and clean up ────────────────────────────
        def iterfile():
            with open(actual_file_path, "rb") as f:
                yield from f
            os.unlink(actual_file_path)

        return StreamingResponse(
            iterfile(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
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
