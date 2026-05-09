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
import secrets
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

DEFAULT_URL = ""
DEFAULT_START_TIME = ""
DEFAULT_END_TIME = ""

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "youtube_clips"))
DEFAULT_COOKIES_FILE = "/etc/secrets/youtube_cookies.txt"
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", DEFAULT_COOKIES_FILE)

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
YTDLP_JS_RUNTIME = os.environ.get("YTDLP_JS_RUNTIME", "deno")

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

    .time-input {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr);
      gap: 6px;
      align-items: center;
    }

    .time-input input {
      text-align: center;
    }

    .time-separator {
      color: #60727a;
      font-weight: 700;
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

    iframe,
    #youtubePlayer {
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
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
        <span class="time-input">
          <input id="startHours" autocomplete="off" inputmode="numeric" aria-label="Start hours">
          <span class="time-separator">:</span>
          <input id="startMinutes" autocomplete="off" inputmode="numeric" aria-label="Start minutes">
          <span class="time-separator">:</span>
          <input id="startSeconds" autocomplete="off" inputmode="decimal" aria-label="Start seconds">
        </span>
      </label>
      <label>
        End
        <span class="time-input">
          <input id="endHours" autocomplete="off" inputmode="numeric" aria-label="End hours">
          <span class="time-separator">:</span>
          <input id="endMinutes" autocomplete="off" inputmode="numeric" aria-label="End minutes">
          <span class="time-separator">:</span>
          <input id="endSeconds" autocomplete="off" inputmode="decimal" aria-label="End seconds">
        </span>
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
    const startFields = {
      hours: document.querySelector("#startHours"),
      minutes: document.querySelector("#startMinutes"),
      seconds: document.querySelector("#startSeconds"),
    };
    const endFields = {
      hours: document.querySelector("#endHours"),
      minutes: document.querySelector("#endMinutes"),
      seconds: document.querySelector("#endSeconds"),
    };
    const fileNameInput = document.querySelector("#fileName");
    const grabButton = document.querySelector("#grab");
    const chopButton = document.querySelector("#chop");
    const clipControls = document.querySelector("#clipControls");
    const previewBox = document.querySelector("#previewBox");
    const statusBox = document.querySelector("#status");
    const downloadArea = document.querySelector("#downloadArea");

    urlInput.value = defaults.url;
    setTimeFields(startFields, defaults.start);
    setTimeFields(endFields, defaults.end);
    fileNameInput.value = defaults.fileName;

    function setBusy(isBusy, text) {
      grabButton.disabled = isBusy;
      chopButton.disabled = isBusy;
      if (text) statusBox.textContent = text;
    }

    function setTimeFields(fields, value) {
      const text = String(value || "").trim();
      if (!text) return;
      const parts = text.split(":");
      fields.hours.value = parts.length === 3 ? parts[0] : "";
      fields.minutes.value = parts.length >= 2 ? parts[parts.length - 2] : "";
      fields.seconds.value = parts[parts.length - 1] || "";
    }

    function parseWholePart(value, label) {
      const text = String(value).trim();
      if (!/^\d+$/.test(text)) throw new Error(`${label} must be a whole number.`);
      return Number.parseInt(text, 10);
    }

    function parseSecondsPart(value, label) {
      const text = String(value).trim();
      if (!/^\d+(\.\d+)?$/.test(text)) throw new Error(`${label} must be a number.`);
      return Number(text);
    }

    function readTimeFields(fields, label, requireAny = false) {
      const rawHours = fields.hours.value.trim();
      const rawMinutes = fields.minutes.value.trim();
      const rawSeconds = fields.seconds.value.trim();
      const hasAny = Boolean(rawHours || rawMinutes || rawSeconds);

      if (!hasAny) {
        if (requireAny) throw new Error(`Enter ${label.toLowerCase()} time before chopping.`);
        return {seconds: null, payload: ""};
      }

      const hours = rawHours ? parseWholePart(rawHours, `${label} hours`) : 0;
      const minutes = rawMinutes ? parseWholePart(rawMinutes, `${label} minutes`) : 0;
      const seconds = rawSeconds ? parseSecondsPart(rawSeconds, `${label} seconds`) : 0;

      if (minutes > 59) throw new Error(`${label} minutes must be 0 through 59.`);
      if (seconds >= 60) throw new Error(`${label} seconds must be less than 60.`);

      return {
        seconds: hours * 3600 + minutes * 60 + seconds,
        payload: `${hours}:${minutes}:${seconds}`,
      };
    }

    function getClipWindow(requireBoth = false) {
      const startTime = readTimeFields(startFields, "Start", requireBoth);
      const endTime = readTimeFields(endFields, "End", requireBoth);
      const start = startTime.seconds;
      const end = endTime.seconds;
      if (requireBoth && (start === null || end === null)) {
        throw new Error("Enter both start and end times before chopping.");
      }
      if (start !== null && end !== null && end <= start) {
        throw new Error("End time must be later than start time.");
      }
      return {
        start,
        end,
        startPayload: startTime.payload,
        endPayload: endTime.payload,
      };
    }

    function enforcePreviewWindow(video) {
      const {start, end} = getClipWindow();
      if (start !== null && video.currentTime < start) {
        video.currentTime = start;
      }
      if (start !== null && end !== null && video.currentTime >= end) {
        video.currentTime = start;
      }
      return {start, end};
    }

    let youtubePlayer = null;
    let youtubeClampTimer = null;
    let youtubeApiPromise = null;

    function loadYouTubeApi() {
      if (window.YT && window.YT.Player) return Promise.resolve();
      if (youtubeApiPromise) return youtubeApiPromise;

      youtubeApiPromise = new Promise((resolve) => {
        window.onYouTubeIframeAPIReady = () => resolve();
        const script = document.createElement("script");
        script.src = "https://www.youtube.com/iframe_api";
        document.head.appendChild(script);
      });
      return youtubeApiPromise;
    }

    function clearYouTubeClamp() {
      if (youtubeClampTimer) {
        clearInterval(youtubeClampTimer);
        youtubeClampTimer = null;
      }
    }

    function setLocalPreview(previewUrl, startSeconds) {
      clearYouTubeClamp();
      youtubePlayer = null;
      previewBox.innerHTML = "";
      const video = document.createElement("video");
      video.src = previewUrl;
      video.controls = true;
      video.preload = "metadata";
      video.addEventListener("loadedmetadata", () => {
        if (startSeconds !== null) video.currentTime = startSeconds;
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
          if (start !== null && video.currentTime < start) video.currentTime = start;
          if (start !== null && end !== null && video.currentTime >= end) {
            video.currentTime = start;
          }
        } catch (error) {
          statusBox.textContent = error.message;
        }
      });
      video.addEventListener("timeupdate", () => {
        try {
          const {start, end} = getClipWindow();
          if (start !== null && video.currentTime < start) {
            video.currentTime = start;
          } else if (start !== null && end !== null && video.currentTime >= end) {
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

    async function setYouTubePreview(videoId, startSeconds) {
      clearYouTubeClamp();
      previewBox.innerHTML = '<div id="youtubePlayer"></div>';
      await loadYouTubeApi();

      const playerVars = {
        autoplay: 0,
        controls: 1,
        enablejsapi: 1,
        origin: window.location.origin,
        rel: 0,
      };
      if (startSeconds !== null) {
        playerVars.start = Math.floor(startSeconds);
      }

      youtubePlayer = new YT.Player("youtubePlayer", {
        width: "100%",
        height: "100%",
        videoId,
        playerVars,
        events: {
          onReady: (event) => {
            if (startSeconds !== null) event.target.seekTo(startSeconds, true);
            clampYouTubePlayer(event.target);
          },
          onStateChange: (event) => {
            if (event.data === YT.PlayerState.PLAYING) {
              clampYouTubePlayer(event.target);
            }
          },
          onError: () => {
            fallbackToLocalPreview("YouTube embed is unavailable. Downloading for local preview...");
          },
        },
      });
    }

    function clampYouTubePlayer(player) {
      clearYouTubeClamp();
      youtubeClampTimer = setInterval(() => {
        try {
          const {start, end} = getClipWindow();
          const current = player.getCurrentTime();
          if (start !== null && current < start) {
            player.seekTo(start, true);
          } else if (start !== null && end !== null && current >= end) {
            player.pauseVideo();
            player.seekTo(start, true);
          }
        } catch (error) {
          player.pauseVideo();
          statusBox.textContent = error.message;
        }
      }, 250);
    }

    async function fallbackToLocalPreview(message) {
      try {
        setBusy(true, message);
        const data = await postJson("/api/local-preview", {
          url: urlInput.value,
          start: readTimeFields(startFields, "Start").payload,
        });
        setLocalPreview(data.previewUrl, data.startSeconds);
        statusBox.textContent = "Local preview loaded. Adjust start/end times, then chop the clip.";
      } catch (error) {
        statusBox.textContent = error.message;
      } finally {
        setBusy(false);
      }
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
        start: readTimeFields(startFields, "Start").payload,
      });
      const video = previewBox.querySelector("video");
      if (video) {
        if (data.startSeconds !== null) {
          video.currentTime = data.startSeconds;
        } else {
          enforcePreviewWindow(video);
        }
      } else if (youtubePlayer) {
        if (data.startSeconds !== null) youtubePlayer.seekTo(data.startSeconds, true);
        clampYouTubePlayer(youtubePlayer);
      } else if (data.previewUrl) {
        setLocalPreview(data.previewUrl, data.startSeconds);
      }
    }

    grabButton.addEventListener("click", async () => {
      try {
        setBusy(true, "Loading YouTube preview...");
        downloadArea.innerHTML = "";
        const data = await postJson("/api/grab", {url: urlInput.value});
        await setYouTubePreview(data.videoId, data.startSeconds);
        clipControls.classList.add("visible");
        statusBox.textContent = "YouTube preview loaded. If the embed is blocked, local preview will download automatically.";
      } catch (error) {
        statusBox.textContent = error.message;
      } finally {
        setBusy(false);
      }
    });

    chopButton.addEventListener("click", async () => {
      try {
        const {startPayload, endPayload} = getClipWindow(true);
        setBusy(true, "Cutting clip...");
        const data = await postJson("/api/chop", {
          start: startPayload,
          end: endPayload,
          fileName: fileNameInput.value,
        });
        statusBox.textContent = `Clip created:\n${data.outputPath}\n\nTemporary downloaded source video deleted.`;
        downloadArea.innerHTML = "";
        const link = document.createElement("a");
        link.href = data.downloadUrl;
        link.download = data.fileName;
        link.textContent = "Download clip";
        downloadArea.appendChild(link);
        fileNameInput.value = data.nextFileName;
        clipControls.classList.remove("visible");
        previewBox.innerHTML = '<div class="empty-preview">Clip created. Grab another video to preview again.</div>';
      } catch (error) {
        statusBox.textContent = error.message;
      } finally {
        setBusy(false);
      }
    });

    Object.values(startFields).forEach((input) => input.addEventListener("change", async () => {
      try {
        await refreshPreview();
        statusBox.textContent = "Preview timing updated.";
      } catch (error) {
        statusBox.textContent = error.message;
      }
    }));

    Object.values(endFields).forEach((input) => input.addEventListener("change", async () => {
      try {
        const video = previewBox.querySelector("video");
        if (video) enforcePreviewWindow(video);
        if (youtubePlayer) clampYouTubePlayer(youtubePlayer);
        statusBox.textContent = "Preview timing updated.";
      } catch (error) {
        statusBox.textContent = error.message;
      }
    }));
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
            elif self.path == "/api/local-preview":
                self.handle_local_preview()
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
            "fileName": random_clip_file_name(),
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
        url = normalize_video_url(str(payload.get("url", "")).strip())
        if not url:
            raise ValueError("Paste a YouTube URL first.")
        video_id = extract_youtube_video_id(url)

        with STATE.lock:
            delete_source_video()
            STATE.current_url = url
            STATE.source_version += 1

        start_seconds = parse_optional_time_to_seconds(DEFAULT_START_TIME)
        self.send_json(
            {
                "ok": True,
                "mode": "youtube",
                "videoId": video_id,
                "startSeconds": start_seconds,
            }
        )

    def handle_local_preview(self) -> None:
        payload = self.read_json()
        url = normalize_video_url(str(payload.get("url") or STATE.current_url).strip())
        start_seconds = parse_optional_time_to_seconds(payload.get("start"))

        with STATE.lock:
            delete_source_video()
            STATE.current_url = url

        source_video = download_video(url)

        with STATE.lock:
            STATE.source_video = source_video
            STATE.source_version += 1
            source_version = STATE.source_version

        self.send_json(
            {
                "ok": True,
                "mode": "local",
                "previewUrl": f"/media/source?v={source_version}",
                "startSeconds": start_seconds,
            }
        )

    def handle_preview(self) -> None:
        payload = self.read_json()
        start_seconds = parse_optional_time_to_seconds(payload.get("start"))
        with STATE.lock:
            source_video = STATE.source_video
            source_version = STATE.source_version

        response = {"ok": True, "startSeconds": start_seconds}
        if source_video is not None and source_video.exists():
            response["previewUrl"] = f"/media/source?v={source_version}"
            response["mode"] = "local"
        else:
            response["mode"] = "youtube"
        self.send_json(response)

    def handle_chop(self) -> None:
        payload = self.read_json()
        start_seconds = parse_optional_time_to_seconds(payload.get("start"))
        end_seconds = parse_optional_time_to_seconds(payload.get("end"))
        if start_seconds is None or end_seconds is None:
            raise ValueError("Enter both start and end times before chopping.")

        output_path = make_output_path(str(payload.get("fileName") or ""))

        if end_seconds <= start_seconds:
            raise ValueError("End time must be later than start time.")

        with STATE.lock:
            source_video = STATE.source_video

        if source_video is None or not source_video.exists():
            with STATE.lock:
                url = STATE.current_url
            source_video = download_video(url)

        with STATE.lock:
            STATE.source_video = source_video
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
                "nextFileName": random_clip_file_name(),
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
    url = normalize_video_url(url)
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
        "--js-runtimes",
        YTDLP_JS_RUNTIME,
        "--remote-components",
        "ejs:npm",
        "-f",
        DOWNLOAD_FORMAT,
        "-o",
        str(OUTPUT_DIR / SOURCE_FILE_TEMPLATE),
        "--print-to-file",
        "after_move:%(filepath)s",
        str(path_log),
    ]

    cookies_file = Path(YTDLP_COOKIES_FILE)
    if cookies_file.exists():
        command.extend(["--cookies", str(cookies_file)])

    command.append(url)

    run_checked(command, "Downloading source video")

    source_path = read_downloaded_path(path_log)
    if not source_path.exists():
        raise RuntimeError(
            f"yt-dlp finished, but the video file was not found: {source_path}"
        )
    return source_path


def normalize_video_url(url: str) -> str:
    """Accept pasted YouTube URLs with or without https://."""
    clean_url = url.strip()
    if not clean_url:
        raise ValueError("Paste a YouTube URL first.")
    if "://" not in clean_url:
        clean_url = f"https://{clean_url}"
    return clean_url


def extract_youtube_video_id(url: str) -> str:
    """Extract a video id for iframe preview from common YouTube URL shapes."""
    split_url = urlsplit(normalize_video_url(url))
    host = split_url.netloc.lower().removeprefix("www.")
    path_parts = [part for part in split_url.path.split("/") if part]

    if host == "youtu.be" and path_parts:
        return path_parts[0]

    if host.endswith("youtube.com"):
        query = parse_qs(split_url.query)
        if query.get("v"):
            return query["v"][0]
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed"}:
            return path_parts[1]

    raise ValueError("That does not look like a supported YouTube video URL.")


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


def parse_optional_time_to_seconds(value: Any) -> float | None:
    """Return None for an empty time field; otherwise parse seconds normally."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return parse_time_to_seconds(value)


def random_clip_file_name() -> str:
    """Generate a short random mp4 name that does not already exist."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        file_name = f"{secrets.token_hex(6)}.mp4"
        if not (OUTPUT_DIR / file_name).exists():
            return file_name
    return f"{secrets.token_hex(12)}.mp4"


def make_output_path(file_name: str) -> Path:
    """Build a safe output path inside OUTPUT_DIR."""
    clean_name = file_name.strip()
    if not clean_name:
        clean_name = random_clip_file_name()
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
            raise RuntimeError(f"{step_name} failed:\n{friendly_command_error(details)}")
        raise RuntimeError(f"{step_name} failed with exit code {completed.returncode}.")


def friendly_command_error(details: str) -> str:
    """Add deployment-specific guidance for common yt-dlp failures."""
    if "Sign in to confirm" in details or "not a bot" in details or "HTTP Error 429" in details:
        cookies_file = YTDLP_COOKIES_FILE or DEFAULT_COOKIES_FILE
        return (
            f"{details}\n\n"
            "YouTube is blocking this server/IP. On Render, this often happens "
            "because datacenter IPs get bot-checked.\n\n"
            "Fix: export YouTube cookies from a browser account that can watch "
            "the video, add them to Render as a Secret File, and set "
            f"YTDLP_COOKIES_FILE to that file path. The default path this app "
            f"checks is: {cookies_file}"
        )
    if "No supported JavaScript runtime" in details:
        return (
            f"{details}\n\n"
            "yt-dlp needs a JavaScript runtime for current YouTube extraction. "
            "The Dockerfile installs Deno and the app passes --js-runtimes deno."
        )
    return details


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
