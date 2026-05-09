"""
YouTube clipper with an embedded preview UI

Run:
    python clipper.py

Requirements:
    1. yt-dlp  -> install with: python -m pip install -U yt-dlp
    2. ffmpeg  -> install from https://ffmpeg.org/ and make sure ffmpeg is on PATH

This script starts a tiny local web app and opens it in your browser. The UI has:
    - a URL bar
    - a Grab Video button
    - an embedded local video preview player
    - start/end clip controls
    - a Chop Clip button

The downloaded source video is temporary. It is deleted when a new video is
grabbed, after a successful clip, and when the server shuts down.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit

# ---------------------------------------------------------------------------
# Useful defaults. Change these if you want different startup behavior.
# ---------------------------------------------------------------------------

DEFAULT_URL = "https://www.youtube.com/"
DEFAULT_START_TIME = "00:00:30"
DEFAULT_END_TIME = "00:00:50"
DEFAULT_CLIP_FILE_NAME = "clip.mp4"

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "youtube_clips"))

# Render requires web services to bind to 0.0.0.0 and the PORT environment
# variable. Locally, no PORT is usually set, so the app picks a free port and
# opens it in your browser.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "0"))
AUTO_OPEN_BROWSER = (
    os.environ.get(
        "AUTO_OPEN_BROWSER",
        "0" if "PORT" in os.environ else "1",
    )
    == "1"
)
OVERWRITE_OUTPUT = True

# True re-encodes for precise frame cuts. False is faster but can start/end on
# nearby keyframes instead of exactly where you asked.
ACCURATE_CLIP = True


# ---------------------------------------------------------------------------
# Tool settings. You usually do not need to touch these.
# ---------------------------------------------------------------------------

DOWNLOAD_FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/" "b[ext=mp4]/best[ext=mp4]/best"
)
SOURCE_FILE_TEMPLATE = "downloaded_source.%(ext)s"
YTDLP_COMMAND = "yt-dlp"
FFMPEG_COMMAND = "ffmpeg"

VIDEO_CODEC = "libx264"
VIDEO_PRESET = "medium"
VIDEO_CRF = "18"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Clipper</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f5f7f8;
      color: #172126;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
    }

    main {
      width: min(1080px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0;
    }

    h1 {
      margin: 0 0 18px;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .toolbar,
    .clip-controls,
    .status {
      background: #ffffff;
      border: 1px solid #d9e0e3;
      border-radius: 8px;
      padding: 14px;
    }

    .toolbar {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
    }

    input {
      width: 100%;
      min-height: 40px;
      border: 1px solid #b8c4ca;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      background: #ffffff;
      color: #172126;
    }

    button {
      min-height: 40px;
      border: 0;
      border-radius: 6px;
      padding: 8px 14px;
      font: inherit;
      font-weight: 700;
      background: #116a7b;
      color: #ffffff;
      cursor: pointer;
      white-space: nowrap;
    }

    button.secondary {
      background: #41545d;
    }

    button:disabled {
      cursor: wait;
      opacity: 0.65;
    }

    .preview {
      margin: 16px 0;
      background: #101820;
      border-radius: 8px;
      overflow: hidden;
      aspect-ratio: 16 / 9;
      border: 1px solid #18242c;
    }

    video {
      width: 100%;
      height: 100%;
      display: block;
      background: #101820;
    }

    .empty-preview {
      height: 100%;
      display: grid;
      place-items: center;
      color: #d5dde1;
      padding: 24px;
      text-align: center;
    }

    .clip-controls {
      display: none;
      grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
      gap: 10px;
      align-items: end;
    }

    .clip-controls.visible {
      display: grid;
    }

    label {
      display: grid;
      gap: 5px;
      font-size: 13px;
      font-weight: 700;
      color: #314148;
    }

    .status {
      margin-top: 16px;
      min-height: 52px;
      white-space: pre-wrap;
      line-height: 1.45;
      color: #263940;
    }

    .download-area {
      margin-top: 10px;
    }

    .download-area a {
      display: inline-block;
      border-radius: 6px;
      padding: 10px 14px;
      background: #116a7b;
      color: #ffffff;
      font-weight: 700;
      text-decoration: none;
    }

    @media (max-width: 760px) {
      .toolbar,
      .clip-controls {
        grid-template-columns: 1fr;
      }

      button {
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>YouTube Clipper</h1>

    <section class="toolbar">
      <input id="url" autocomplete="off" spellcheck="false">
      <button id="grab">Grab Video</button>
    </section>

    <section class="preview" id="previewBox">
      <div class="empty-preview" id="emptyPreview">Paste a YouTube URL, then grab the video to load the embedded preview.</div>
    </section>

    <section class="clip-controls" id="clipControls">
      <label>
        Start
        <input id="start" autocomplete="off">
      </label>
      <label>
        End
        <input id="end" autocomplete="off">
      </label>
      <label>
        Output file
        <input id="fileName" autocomplete="off">
      </label>
      <button id="chop">Chop Clip</button>
    </section>

    <section class="status" id="status">Ready.</section>
    <section class="download-area" id="downloadArea"></section>
  </main>

  <script>
    const defaults = __DEFAULTS__;
    const urlInput = document.querySelector("#url");
    const startInput = document.querySelector("#start");
    const endInput = document.querySelector("#end");
    const fileNameInput = document.querySelector("#fileName");
    const grabButton = document.querySelector("#grab");
    const chopButton = document.querySelector("#chop");
    const clipControls = document.querySelector("#clipControls");
    const previewBox = document.querySelector("#previewBox");
    const statusBox = document.querySelector("#status");
    const downloadArea = document.querySelector("#downloadArea");

    urlInput.value = defaults.url;
    startInput.value = defaults.start;
    endInput.value = defaults.end;
    fileNameInput.value = defaults.fileName;

    function setBusy(isBusy, text) {
      grabButton.disabled = isBusy;
      chopButton.disabled = isBusy;
      if (text) statusBox.textContent = text;
    }

    function parseTime(value) {
      const text = String(value).trim();
      if (!text) throw new Error("Time values cannot be empty.");
      const parts = text.split(":");
      if (parts.length > 3) throw new Error(`Invalid time value: ${value}`);

      let seconds;
      if (parts.length === 1) {
        seconds = Number(parts[0]);
      } else if (parts.length === 2) {
        seconds = Number.parseInt(parts[0], 10) * 60 + Number(parts[1]);
      } else {
        seconds = Number.parseInt(parts[0], 10) * 3600 +
          Number.parseInt(parts[1], 10) * 60 +
          Number(parts[2]);
      }

      if (!Number.isFinite(seconds) || seconds < 0) {
        throw new Error(`Invalid time value: ${value}`);
      }
      return seconds;
    }

    function getClipWindow() {
      const start = parseTime(startInput.value);
      const end = parseTime(endInput.value);
      if (end <= start) throw new Error("End time must be later than start time.");
      return {start, end};
    }

    function enforcePreviewWindow(video) {
      const {start, end} = getClipWindow();
      if (video.currentTime < start || video.currentTime >= end) {
        video.currentTime = start;
      }
      return {start, end};
    }

    function setPreview(previewUrl, startSeconds) {
      previewBox.innerHTML = "";
      const video = document.createElement("video");
      video.src = previewUrl;
      video.controls = true;
      video.preload = "metadata";
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = startSeconds;
      }, {once: true});
      video.addEventListener("play", () => {
        try {
          enforcePreviewWindow(video);
        } catch (error) {
          video.pause();
          statusBox.textContent = error.message;
        }
      });
      video.addEventListener("seeking", () => {
        try {
          const {start, end} = getClipWindow();
          if (video.currentTime < start) video.currentTime = start;
          if (video.currentTime >= end) video.currentTime = start;
        } catch (error) {
          statusBox.textContent = error.message;
        }
      });
      video.addEventListener("timeupdate", () => {
        try {
          const {start, end} = getClipWindow();
          if (video.currentTime < start) {
            video.currentTime = start;
          } else if (video.currentTime >= end) {
            video.pause();
            video.currentTime = start;
          }
        } catch (error) {
          video.pause();
          statusBox.textContent = error.message;
        }
      });
      previewBox.appendChild(video);
    }

    async function postJson(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || "Request failed.");
      }
      return data;
    }

    async function refreshPreview() {
      const data = await postJson("/api/preview", {
        start: startInput.value,
      });
      const video = previewBox.querySelector("video");
      if (video) {
        video.currentTime = data.startSeconds;
      } else {
        setPreview(data.previewUrl, data.startSeconds);
      }
    }

    grabButton.addEventListener("click", async () => {
      try {
        setBusy(true, "Downloading source video...");
        downloadArea.innerHTML = "";
        const data = await postJson("/api/grab", {url: urlInput.value});
        setPreview(data.previewUrl, data.startSeconds);
        clipControls.classList.add("visible");
        statusBox.textContent = "Video grabbed. Adjust start/end times, then chop the clip.";
      } catch (error) {
        statusBox.textContent = error.message;
      } finally {
        setBusy(false);
      }
    });

    chopButton.addEventListener("click", async () => {
      try {
        setBusy(true, "Cutting clip...");
        const data = await postJson("/api/chop", {
          start: startInput.value,
          end: endInput.value,
          fileName: fileNameInput.value,
        });
        statusBox.textContent = `Clip created:\n${data.outputPath}\n\nTemporary downloaded source video deleted.`;
        downloadArea.innerHTML = "";
        const link = document.createElement("a");
        link.href = data.downloadUrl;
        link.download = data.fileName;
        link.textContent = "Download clip";
        downloadArea.appendChild(link);
        clipControls.classList.remove("visible");
        previewBox.innerHTML = '<div class="empty-preview">Clip created. Grab another video to preview again.</div>';
      } catch (error) {
        statusBox.textContent = error.message;
      } finally {
        setBusy(false);
      }
    });

    startInput.addEventListener("change", async () => {
      try {
        await refreshPreview();
        statusBox.textContent = "Preview clamped to the selected start and end times.";
      } catch (error) {
        statusBox.textContent = error.message;
      }
    });

    endInput.addEventListener("change", async () => {
      try {
        const video = previewBox.querySelector("video");
        if (video) enforcePreviewWindow(video);
        statusBox.textContent = "Preview clamped to the selected start and end times.";
      } catch (error) {
        statusBox.textContent = error.message;
      }
    });
  </script>
</body>
</html>
"""


class AppState:
    """Shared server state for the one active temporary download."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.source_video: Path | None = None
        self.current_url = DEFAULT_URL
        self.source_version = 0


STATE = AppState()


class ClipperRequestHandler(BaseHTTPRequestHandler):
    """HTTP endpoints used by the local clipping UI."""

    server_version = "YouTubeClipper/1.0"

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route in {"/", "/index.html"}:
            self.send_html()
            return
        if route == "/healthz":
            self.send_json({"ok": True})
            return
        if route == "/media/source":
            self.send_source_video()
            return
        if route == "/download":
            self.send_finished_clip()
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        try:
            if self.path == "/api/grab":
                self.handle_grab()
            elif self.path == "/api/chop":
                self.handle_chop()
            elif self.path == "/api/preview":
                self.handle_preview()
            else:
                self.send_json({"ok": False, "error": "Unknown endpoint."}, status=404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default per-request logging so the terminal stays readable."""

    def send_html(self) -> None:
        defaults = {
            "url": DEFAULT_URL,
            "start": DEFAULT_START_TIME,
            "end": DEFAULT_END_TIME,
            "fileName": DEFAULT_CLIP_FILE_NAME,
        }
        html = APP_HTML.replace("__DEFAULTS__", json.dumps(defaults))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_grab(self) -> None:
        payload = self.read_json()
        url = str(payload.get("url", "")).strip()
        if not url:
            raise ValueError("Paste a YouTube URL first.")

        with STATE.lock:
            delete_source_video()
            STATE.source_video = download_video(url)
            STATE.current_url = url
            STATE.source_version += 1
            source_version = STATE.source_version

        start_seconds = parse_time_to_seconds(DEFAULT_START_TIME)
        self.send_json(
            {
                "ok": True,
                "previewUrl": f"/media/source?v={source_version}",
                "startSeconds": start_seconds,
            }
        )

    def handle_preview(self) -> None:
        payload = self.read_json()
        start_seconds = parse_time_to_seconds(payload.get("start", DEFAULT_START_TIME))
        with STATE.lock:
            if STATE.source_video is None or not STATE.source_video.exists():
                raise ValueError("Grab a video before previewing.")
            source_version = STATE.source_version
        self.send_json(
            {
                "ok": True,
                "previewUrl": f"/media/source?v={source_version}",
                "startSeconds": start_seconds,
            }
        )

    def handle_chop(self) -> None:
        payload = self.read_json()
        start_seconds = parse_time_to_seconds(payload.get("start", DEFAULT_START_TIME))
        end_seconds = parse_time_to_seconds(payload.get("end", DEFAULT_END_TIME))
        output_path = make_output_path(
            str(payload.get("fileName", DEFAULT_CLIP_FILE_NAME))
        )

        if end_seconds <= start_seconds:
            raise ValueError("End time must be later than start time.")

        with STATE.lock:
            if STATE.source_video is None or not STATE.source_video.exists():
                raise ValueError("Grab a video before chopping a clip.")

            source_video = STATE.source_video
            clip_video(source_video, output_path, start_seconds, end_seconds)
            source_video.unlink(missing_ok=True)
            STATE.source_video = None

        file_name = str(output_path.relative_to(OUTPUT_DIR))
        self.send_json(
            {
                "ok": True,
                "outputPath": str(output_path.resolve()),
                "fileName": output_path.name,
                "downloadUrl": f"/download?{urlencode({'file': file_name})}",
            }
        )

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_source_video(self) -> None:
        """Serve the temporary MP4 with byte ranges so browser seeking works."""
        with STATE.lock:
            source_video = STATE.source_video

        if source_video is None or not source_video.exists():
            self.send_error(404, "No grabbed video is available.")
            return

        self.send_file(source_video, "video/mp4")

    def send_finished_clip(self) -> None:
        """Serve a finished clip as an attachment for the download link."""
        query = parse_qs(urlsplit(self.path).query)
        file_values = query.get("file")
        if not file_values:
            self.send_error(400, "Missing file.")
            return

        try:
            clip_path = make_output_path(file_values[0])
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        if not clip_path.exists():
            self.send_error(404, "Clip was not found.")
            return

        self.send_file(clip_path, "video/mp4", attachment_name=clip_path.name)

    def send_file(
        self,
        file_path: Path,
        content_type: str,
        attachment_name: str | None = None,
    ) -> None:
        """Serve a file with byte ranges so video playback and downloads work."""
        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = 200

        if range_header:
            try:
                unit, range_value = range_header.split("=", 1)
                if unit.strip() != "bytes":
                    raise ValueError
                start_text, end_text = range_value.split("-", 1)
                if start_text:
                    start = int(start_text)
                if end_text:
                    end = int(end_text)
                status = 206
            except ValueError:
                self.send_error(416, "Invalid range.")
                return

        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        content_length = end - start + 1

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if attachment_name is not None:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{attachment_name}"',
            )
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with file_path.open("rb") as opened_file:
            opened_file.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = opened_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def download_video(url: str) -> Path:
    """Download a YouTube URL and return the temporary local video path."""
    ytdlp = find_yt_dlp_command()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path_log = OUTPUT_DIR / "_downloaded_path.txt"
    path_log.unlink(missing_ok=True)

    command = [
        *ytdlp,
        "--no-playlist",
        "--force-overwrites",
        "--no-continue",
        "--merge-output-format",
        "mp4",
        "-f",
        DOWNLOAD_FORMAT,
        "-o",
        str(OUTPUT_DIR / SOURCE_FILE_TEMPLATE),
        "--print-to-file",
        "after_move:%(filepath)s",
        str(path_log),
        url,
    ]

    run_checked(command, "Downloading source video")

    source_path = read_downloaded_path(path_log)
    if not source_path.exists():
        raise RuntimeError(
            f"yt-dlp finished, but the video file was not found: {source_path}"
        )
    return source_path


def clip_video(
    source_video: Path, output_path: Path, start_seconds: float, end_seconds: float
) -> None:
    """Cut a source video into the requested mp4 output file."""
    ffmpeg = require_executable(
        FFMPEG_COMMAND,
        "ffmpeg was not found. Install ffmpeg and make sure it is available on PATH.",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end_seconds - start_seconds

    if output_path.resolve() == source_video.resolve():
        raise ValueError(
            "The clip output cannot be the same file as the downloaded source."
        )

    command = [ffmpeg, "-hide_banner"]
    command.append("-y" if OVERWRITE_OUTPUT else "-n")

    if ACCURATE_CLIP:
        # Accurate mode seeks after opening the file, so ffmpeg decodes up to
        # START_TIME and cuts precisely. This is slower on long videos.
        command.extend(["-i", str(source_video), "-ss", ffmpeg_time(start_seconds)])
    else:
        # Fast mode seeks before opening the file. It is quick but can snap to
        # the nearest keyframe instead of the exact frame requested.
        command.extend(["-ss", ffmpeg_time(start_seconds), "-i", str(source_video)])

    command.extend(["-t", ffmpeg_time(duration), "-map", "0:v:0", "-map", "0:a?"])

    if ACCURATE_CLIP:
        command.extend(
            [
                "-c:v",
                VIDEO_CODEC,
                "-preset",
                VIDEO_PRESET,
                "-crf",
                VIDEO_CRF,
                "-c:a",
                AUDIO_CODEC,
                "-b:a",
                AUDIO_BITRATE,
            ]
        )
    else:
        command.extend(["-c", "copy"])

    command.extend(["-movflags", "+faststart", str(output_path)])
    run_checked(command, "Cutting clip")


def parse_time_to_seconds(value: Any) -> float:
    """
    Convert a time value into seconds.

    Accepted examples:
        75
        "75"
        "1:15"
        "00:01:15"
        "00:01:15.250"
    """
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ValueError("Times cannot be negative.")
        return seconds

    text = str(value).strip()
    if not text:
        raise ValueError("Time values cannot be empty.")

    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time value: {value!r}")

    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            minutes = int(parts[0])
            seconds = (minutes * 60) + float(parts[1])
        else:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = (hours * 3600) + (minutes * 60) + float(parts[2])
    except ValueError as exc:
        raise ValueError(f"Invalid time value: {value!r}") from exc

    if seconds < 0:
        raise ValueError("Times cannot be negative.")
    return seconds


def make_output_path(file_name: str) -> Path:
    """Build a safe output path inside OUTPUT_DIR."""
    clean_name = file_name.strip()
    if not clean_name:
        raise ValueError("Output file name cannot be empty.")
    if not clean_name.lower().endswith(".mp4"):
        raise ValueError("Output file name must end with .mp4.")

    path = Path(clean_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Output file name must stay inside the output folder.")
    return OUTPUT_DIR / path


def delete_source_video() -> None:
    """Delete temporary downloaded source videos without touching finished clips."""
    if STATE.source_video is not None:
        STATE.source_video.unlink(missing_ok=True)
        STATE.source_video = None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUTPUT_DIR.glob("downloaded_source.*"):
        path.unlink(missing_ok=True)
    (OUTPUT_DIR / "_downloaded_path.txt").unlink(missing_ok=True)


def find_yt_dlp_command() -> list[str]:
    """Return the best available way to run yt-dlp."""
    executable = shutil.which(YTDLP_COMMAND)
    if executable:
        return [executable]

    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]

    raise RuntimeError(
        "yt-dlp was not found. Install it with: python -m pip install -U yt-dlp"
    )


def require_executable(name: str, missing_message: str) -> str:
    """Return an executable path or stop with a helpful message."""
    executable = shutil.which(name)
    if executable:
        return executable
    raise RuntimeError(missing_message)


def read_downloaded_path(path_log: Path) -> Path:
    """Read the source video path written by yt-dlp."""
    if not path_log.exists():
        raise RuntimeError("yt-dlp did not report the downloaded file path.")

    lines = [line.strip() for line in path_log.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise RuntimeError("yt-dlp reported an empty downloaded file path.")

    downloaded = Path(lines[-1])
    if not downloaded.is_absolute():
        downloaded = Path.cwd() / downloaded
    return downloaded


def ffmpeg_time(seconds: float) -> str:
    """Format seconds in a way ffmpeg accepts without losing millisecond detail."""
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def run_checked(command: Iterable[str], step_name: str) -> None:
    """Run a command and turn failures into readable script errors."""
    command = [str(part) for part in command]
    completed = subprocess.run(command, capture_output=True, text=True)

    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        if details:
            raise RuntimeError(f"{step_name} failed:\n{details}")
        raise RuntimeError(f"{step_name} failed with exit code {completed.returncode}.")


def bind_server() -> ThreadingHTTPServer:
    """Bind the configured host/port for local use or Render deployment."""
    try:
        return ThreadingHTTPServer((HOST, PORT), ClipperRequestHandler)
    except OSError:
        if "PORT" in os.environ:
            raise
        return ThreadingHTTPServer((HOST, 0), ClipperRequestHandler)


def main() -> None:
    server = bind_server()
    host, port = server.server_address
    url = f"http://{host}:{port}/"

    print(f"YouTube Clipper running at {url}")
    print("Press Ctrl+C in this terminal to stop the server.")
    if AUTO_OPEN_BROWSER:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        delete_source_video()
        server.server_close()


if __name__ == "__main__":
    main()
