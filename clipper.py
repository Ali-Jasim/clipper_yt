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
SESSION_COOKIES_FILE = OUTPUT_DIR / "_session_cookies.txt"

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
      --bg:              #0d0f17;
      --surface:         #161923;
      --surface2:        #1c2030;
      --border:          #272c3d;
      --accent:          #22d3ee;
      --accent-dark:     #06b6d4;
      --accent-light:    #164e63;
      --text:            #e2e8f0;
      --text-muted:      #8892a4;
      --success:         #4ade80;
      --success-bg:      #052e16;
      --success-border:  #166534;
      --error:           #f87171;
      --error-bg:        #3b0a0a;
      --error-border:    #7f1d1d;
      --loading:         #93c5fd;
      --loading-bg:      #0f1e3d;
      --loading-border:  #1e3a5f;
      --radius:          10px;
      --radius-sm:       6px;
      --shadow:          0 1px 4px rgba(0,0,0,.4), 0 1px 2px rgba(0,0,0,.3);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      color-scheme: dark;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 24px 16px 48px;
    }

    main {
      width: min(1080px, 100%);
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    h1 {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -.5px;
      margin-bottom: 4px;
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px 16px;
      box-shadow: var(--shadow);
    }

    /* ── Toolbar ── */
    .toolbar {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
    }

    /* ── Inputs ── */
    input[type="text"] {
      width: 100%;
      height: 38px;
      padding: 0 10px;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      font: inherit;
      font-size: 14px;
      background: var(--surface2);
      color: var(--text);
      outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    input[type="text"]:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(34,211,238,.15);
    }

    /* ── Buttons ── */
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      height: 38px;
      padding: 0 16px;
      border: none;
      border-radius: var(--radius-sm);
      font: inherit;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      background: var(--accent);
      color: #fff;
      transition: background .15s, opacity .15s;
    }
    button:hover:not(:disabled) { background: var(--accent-dark); }
    button:disabled { opacity: .55; cursor: wait; }

    button.secondary {
      background: transparent;
      color: var(--accent);
      border: 1px solid var(--accent);
      font-weight: 500;
    }
    button.secondary:hover:not(:disabled) { background: var(--accent-light); }

    /* ── Video preview ── */
    .preview {
      background: #080b10;
      border-radius: var(--radius);
      overflow: hidden;
      aspect-ratio: 16/9;
      border: 1px solid var(--border);
    }
    video, #youtubePlayer {
      width: 100%;
      height: 100%;
      display: block;
      border: none;
      background: #080b10;
    }
    .empty-preview {
      height: 100%;
      display: grid;
      place-items: center;
      color: var(--text-muted);
      text-align: center;
      padding: 32px;
      font-size: 15px;
      line-height: 1.6;
    }

    /* ── Clip range bar ── */
    .clip-bar-wrapper {
      display: none;
      flex-direction: column;
      gap: 5px;
      padding: 0 2px;
    }
    .clip-bar-wrapper.visible { display: flex; }

    .clip-bar {
      position: relative;
      height: 6px;
      background: var(--border);
      border-radius: 3px;
      overflow: hidden;
      cursor: default;
    }
    .clip-bar-fill {
      position: absolute;
      top: 0; bottom: 0;
      background: var(--accent);
      opacity: .75;
      border-radius: 3px;
      transition: left .18s, width .18s;
    }
    .clip-bar-labels {
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }

    /* ── Clip controls ── */
    .clip-controls { display: none; }
    .clip-controls.visible {
      display: grid;
      grid-template-columns: 1fr 1fr;
      grid-template-areas:
        "start   end"
        "meta    meta"
        "file    chop";
      gap: 14px 20px;
      align-items: end;
    }

    .time-group {
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .time-group label,
    .file-group label {
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .time-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 6px;
    }

    .clip-meta {
      grid-area: meta;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: var(--text-muted);
    }
    .clip-meta .duration-value {
      font-weight: 700;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .clip-meta .hint {
      margin-left: auto;
      font-size: 11px;
    }
    kbd {
      display: inline-block;
      padding: 1px 5px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 4px;
      font-family: monospace;
      font-size: 11px;
    }

    .file-group {
      grid-area: file;
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    #chop { grid-area: chop; }

    /* ── Progress bar ── */
    .progress-track {
      height: 3px;
      background: var(--border);
      border-radius: 2px;
      overflow: hidden;
      opacity: 0;
      transition: opacity .2s;
    }
    .progress-track.active { opacity: 1; }
    .progress-fill {
      height: 100%;
      background: var(--accent);
      width: 40%;
      border-radius: 2px;
      transform: translateX(-150%);
    }
    .progress-fill.running {
      animation: sweep 1.4s ease-in-out infinite;
    }
    @keyframes sweep {
      0%   { transform: translateX(-150%); }
      100% { transform: translateX(350%); }
    }

    /* ── Status ── */
    .status {
      padding: 12px 14px;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      font-size: 14px;
      line-height: 1.5;
      white-space: pre-wrap;
      color: var(--text-muted);
      background: var(--surface2);
      transition: background .2s, border-color .2s, color .2s;
    }
    .status[data-type="success"] {
      background: var(--success-bg);
      border-color: var(--success-border);
      color: var(--success);
    }
    .status[data-type="error"] {
      background: var(--error-bg);
      border-color: var(--error-border);
      color: var(--error);
    }
    .status[data-type="loading"] {
      background: var(--loading-bg);
      border-color: var(--loading-border);
      color: var(--loading);
    }

    /* ── Download ── */
    .download-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      background: var(--accent);
      color: #fff;
      border-radius: var(--radius-sm);
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      transition: background .15s;
    }
    .download-btn:hover { background: var(--accent-dark); }

    /* ── Cookie bar ── */
    .cookie-bar {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      padding: 2px 2px;
    }
    .cookie-status {
      display: flex;
      align-items: center;
      gap: 5px;
      color: var(--text-muted);
    }
    .cookie-status.loaded  { color: var(--success); }
    .cookie-status.missing { color: #fb923c; }
    .cookie-dot {
      width: 7px; height: 7px;
      border-radius: 50%;
      background: currentColor;
      flex-shrink: 0;
    }
    .cookie-upload-label {
      margin-left: auto;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 4px 10px;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text-muted);
      cursor: pointer;
      transition: border-color .15s, color .15s;
      white-space: nowrap;
    }
    .cookie-upload-label:hover {
      border-color: var(--accent);
      color: var(--accent);
    }

    /* ── Responsive ── */
    @media (max-width: 620px) {
      .toolbar { grid-template-columns: 1fr; }
      .clip-controls.visible {
        grid-template-columns: 1fr;
        grid-template-areas: "start" "end" "meta" "file" "chop";
      }
      button { width: 100%; }
      .time-row button { width: auto; }
    }
  </style>
</head>
<body>
<main>
  <h1>YouTube Clipper</h1>

  <section class="card toolbar">
    <input id="url" type="text" autocomplete="off" spellcheck="false" placeholder="https://youtube.com/watch?v=…">
    <button id="grab">Grab Video</button>
  </section>

  <div class="cookie-bar">
    <span class="cookie-status" id="cookieStatus">
      <span class="cookie-dot"></span>
      <span id="cookieStatusText">Checking cookies…</span>
    </span>
    <label class="cookie-upload-label" title="Upload a Netscape cookies.txt file exported from your browser">
      ↑ Upload cookies.txt
      <input type="file" id="cookieFileInput" accept=".txt" style="display:none">
    </label>
  </div>

  <section class="preview" id="previewBox">
    <div class="empty-preview">Paste a YouTube URL above, then click Grab Video.</div>
  </section>

  <div class="clip-bar-wrapper" id="clipBarWrapper">
    <div class="clip-bar">
      <div class="clip-bar-fill" id="clipBarFill" style="left:0%;width:100%"></div>
    </div>
    <div class="clip-bar-labels">
      <span id="clipBarStart">0:00</span>
      <span id="clipBarDuration"></span>
      <span id="clipBarEnd"></span>
    </div>
  </div>

  <section class="card clip-controls" id="clipControls">
    <div class="time-group" style="grid-area:start">
      <label for="startTime">Start</label>
      <div class="time-row">
        <input id="startTime" type="text" autocomplete="off" inputmode="numeric" placeholder="0:00:00">
        <button id="markIn" class="secondary" title="Mark in at current position [ ">⬥ Mark In</button>
      </div>
    </div>

    <div class="time-group" style="grid-area:end">
      <label for="endTime">End</label>
      <div class="time-row">
        <input id="endTime" type="text" autocomplete="off" inputmode="numeric" placeholder="0:00:00">
        <button id="markOut" class="secondary" title="Mark out at current position ]">Mark Out ⬦</button>
      </div>
    </div>

    <div class="clip-meta">
      Duration: <span class="duration-value" id="durationValue">—</span>
      <span class="hint"><kbd>[</kbd> Mark In &nbsp; <kbd>]</kbd> Mark Out</span>
    </div>

    <div class="file-group">
      <label for="fileName">Output file</label>
      <input id="fileName" type="text" autocomplete="off">
    </div>

    <button id="chop">✂ Chop Clip</button>
  </section>

  <div class="progress-track" id="progressTrack">
    <div class="progress-fill" id="progressFill"></div>
  </div>

  <section class="status" id="status" data-type="idle">Ready.</section>
  <div id="downloadArea"></div>
</main>

<script>
  const defaults = __DEFAULTS__;

  // ── Element refs ──────────────────────────────────────────────────────────
  const urlInput        = document.getElementById("url");
  const grabButton      = document.getElementById("grab");
  const previewBox      = document.getElementById("previewBox");
  const clipControls    = document.getElementById("clipControls");
  const clipBarWrapper  = document.getElementById("clipBarWrapper");
  const clipBarFill     = document.getElementById("clipBarFill");
  const clipBarStart    = document.getElementById("clipBarStart");
  const clipBarDuration = document.getElementById("clipBarDuration");
  const clipBarEnd      = document.getElementById("clipBarEnd");
  const startInput      = document.getElementById("startTime");
  const endInput        = document.getElementById("endTime");
  const durationValue   = document.getElementById("durationValue");
  const fileNameInput   = document.getElementById("fileName");
  const chopButton      = document.getElementById("chop");
  const progressTrack   = document.getElementById("progressTrack");
  const progressFill    = document.getElementById("progressFill");
  const statusBox       = document.getElementById("status");
  const downloadArea    = document.getElementById("downloadArea");

  // ── Defaults ──────────────────────────────────────────────────────────────
  urlInput.value      = defaults.url;
  startInput.value    = defaults.start;
  endInput.value      = defaults.end;
  fileNameInput.value = defaults.fileName;

  // ── Time utilities ────────────────────────────────────────────────────────
  function parseTimeInput(text) {
    const t = String(text || "").trim();
    if (!t) return null;
    const parts = t.split(":");
    try {
      if (parts.length === 1) {
        const s = parseFloat(parts[0]);
        return isNaN(s) ? null : s;
      }
      if (parts.length === 2) {
        const m = parseInt(parts[0], 10), s = parseFloat(parts[1]);
        return (isNaN(m) || isNaN(s)) ? null : m * 60 + s;
      }
      const h = parseInt(parts[0], 10), m = parseInt(parts[1], 10), s = parseFloat(parts[2]);
      return (isNaN(h) || isNaN(m) || isNaN(s)) ? null : h * 3600 + m * 60 + s;
    } catch { return null; }
  }

  function formatTime(total) {
    if (total === null || isNaN(total) || total < 0) return "—";
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    // always two digits before any decimal point
    const sStr = s.toFixed(3).replace(/\.?0+$/, "");
    const sPad = sStr.replace(/^(\d)(?=\.|$)/, "0$1");
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${sPad}`;
    return `${m}:${sPad}`;
  }

  function validateClipWindow(requireBoth = false) {
    const start = parseTimeInput(startInput.value);
    const end   = parseTimeInput(endInput.value);
    if (requireBoth && start === null) throw new Error("Enter a start time before chopping.");
    if (requireBoth && end   === null) throw new Error("Enter an end time before chopping.");
    if (start !== null && end !== null && end <= start) {
      throw new Error("End time must be later than start time.");
    }
    return { start, end, startPayload: startInput.value.trim(), endPayload: endInput.value.trim() };
  }

  // ── Progress bar ──────────────────────────────────────────────────────────
  function startProgress() {
    progressTrack.classList.add("active");
    progressFill.classList.add("running");
  }
  function stopProgress() {
    progressFill.classList.remove("running");
    progressTrack.classList.remove("active");
  }

  // ── Status ────────────────────────────────────────────────────────────────
  function setStatus(message, type = "idle") {
    statusBox.textContent = message;
    statusBox.dataset.type = type;
  }

  // ── Busy state ────────────────────────────────────────────────────────────
  function setBusy(isBusy, message = "", type = "loading") {
    grabButton.disabled = isBusy;
    chopButton.disabled = isBusy;
    if (message) setStatus(message, type);
    if (isBusy) startProgress(); else stopProgress();
  }

  // ── Player accessors ──────────────────────────────────────────────────────
  function getPlayerDuration() {
    const video = previewBox.querySelector("video");
    if (video && isFinite(video.duration)) return video.duration;
    if (youtubePlayer && typeof youtubePlayer.getDuration === "function") {
      const d = youtubePlayer.getDuration();
      if (d > 0) return d;
    }
    return null;
  }

  function getCurrentPlayerTime() {
    const video = previewBox.querySelector("video");
    if (video) return video.currentTime;
    if (youtubePlayer && typeof youtubePlayer.getCurrentTime === "function") {
      return youtubePlayer.getCurrentTime();
    }
    return null;
  }

  // ── Duration display & clip range bar ────────────────────────────────────
  function updateDuration() {
    const start = parseTimeInput(startInput.value);
    const end   = parseTimeInput(endInput.value);
    durationValue.textContent = (start !== null && end !== null && end > start)
      ? formatTime(end - start) : "—";
    updateClipBar();
  }

  function updateClipBar() {
    const duration = getPlayerDuration();
    if (!duration) return;
    const start = parseTimeInput(startInput.value) ?? 0;
    const end   = parseTimeInput(endInput.value)   ?? duration;
    const s = Math.max(0, Math.min(100, (start / duration) * 100));
    const e = Math.max(0, Math.min(100, (end   / duration) * 100));
    clipBarFill.style.left  = `${s}%`;
    clipBarFill.style.width = `${e - s}%`;
    clipBarStart.textContent    = formatTime(start);
    clipBarEnd.textContent      = formatTime(end);
    clipBarDuration.textContent = (end > start) ? formatTime(end - start) : "";
  }

  function showClipBar() {
    clipBarWrapper.classList.add("visible");
    updateClipBar();
  }

  // ── Mark In / Mark Out ────────────────────────────────────────────────────
  function markIn() {
    const t = getCurrentPlayerTime();
    if (t === null) return;
    startInput.value = formatTime(t);
    updateDuration();
    refreshPreview().catch(() => {});
  }

  function markOut() {
    const t = getCurrentPlayerTime();
    if (t === null) return;
    endInput.value = formatTime(t);
    updateDuration();
  }

  document.getElementById("markIn").addEventListener("click", markIn);
  document.getElementById("markOut").addEventListener("click", markOut);

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "[") markIn();
    if (e.key === "]") markOut();
  });

  // ── Preview window enforcement ────────────────────────────────────────────
  function enforcePreviewWindow(video) {
    const { start, end } = validateClipWindow();
    if (start !== null && video.currentTime < start) video.currentTime = start;
    if (start !== null && end !== null && video.currentTime >= end) video.currentTime = start;
  }

  // ── YouTube player state ──────────────────────────────────────────────────
  let youtubePlayer     = null;
  let youtubeClampTimer = null;
  let youtubeApiPromise = null;

  function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (youtubeApiPromise) return youtubeApiPromise;
    youtubeApiPromise = new Promise((resolve) => {
      window.onYouTubeIframeAPIReady = () => resolve();
      const s = document.createElement("script");
      s.src = "https://www.youtube.com/iframe_api";
      document.head.appendChild(s);
    });
    return youtubeApiPromise;
  }

  function clearYouTubeClamp() {
    if (youtubeClampTimer) { clearInterval(youtubeClampTimer); youtubeClampTimer = null; }
  }

  function clampYouTubePlayer(player) {
    clearYouTubeClamp();
    youtubeClampTimer = setInterval(() => {
      try {
        const { start, end } = validateClipWindow();
        const cur = player.getCurrentTime();
        if (start !== null && cur < start) {
          player.seekTo(start, true);
        } else if (start !== null && end !== null && cur >= end) {
          player.pauseVideo();
          player.seekTo(start, true);
        }
      } catch { player.pauseVideo(); }
    }, 250);
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
      showClipBar();
    }, { once: true });
    video.addEventListener("play", () => {
      try { enforcePreviewWindow(video); }
      catch (err) { video.pause(); setStatus(err.message, "error"); }
    });
    video.addEventListener("seeking", () => {
      try {
        const { start, end } = validateClipWindow();
        if (start !== null && video.currentTime < start) video.currentTime = start;
        if (start !== null && end !== null && video.currentTime >= end) video.currentTime = start;
      } catch (err) { setStatus(err.message, "error"); }
    });
    video.addEventListener("timeupdate", () => {
      try {
        const { start, end } = validateClipWindow();
        if (start !== null && video.currentTime < start) {
          video.currentTime = start;
        } else if (start !== null && end !== null && video.currentTime >= end) {
          video.pause();
          video.currentTime = start;
        }
      } catch (err) { video.pause(); setStatus(err.message, "error"); }
    });
    previewBox.appendChild(video);
  }

  async function setYouTubePreview(videoId, startSeconds) {
    clearYouTubeClamp();
    previewBox.innerHTML = '<div id="youtubePlayer"></div>';
    await loadYouTubeApi();
    const playerVars = { autoplay: 0, controls: 1, enablejsapi: 1, origin: window.location.origin, rel: 0 };
    if (startSeconds !== null) playerVars.start = Math.floor(startSeconds);
    youtubePlayer = new YT.Player("youtubePlayer", {
      width: "100%", height: "100%", videoId, playerVars,
      events: {
        onReady: (e) => {
          if (startSeconds !== null) e.target.seekTo(startSeconds, true);
          clampYouTubePlayer(e.target);
          showClipBar();
        },
        onStateChange: (e) => {
          if (e.data === YT.PlayerState.PLAYING) clampYouTubePlayer(e.target);
        },
        onError: () => fallbackToLocalPreview("YouTube embed blocked — downloading for local preview…"),
      },
    });
  }

  async function fallbackToLocalPreview(message) {
    try {
      setBusy(true, message);
      const data = await postJson("/api/local-preview", {
        url: urlInput.value, start: startInput.value.trim(),
      });
      setLocalPreview(data.previewUrl, data.startSeconds);
      setStatus("Local preview loaded. Adjust clip times, then chop.", "idle");
    } catch (err) {
      setStatus(err.message, "error");
    } finally {
      setBusy(false);
    }
  }

  // ── API helpers ───────────────────────────────────────────────────────────
  async function postJson(path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || "Request failed.");
    return data;
  }

  async function refreshPreview() {
    const data = await postJson("/api/preview", { start: startInput.value.trim() });
    const video = previewBox.querySelector("video");
    if (video) {
      if (data.startSeconds !== null) video.currentTime = data.startSeconds;
      else enforcePreviewWindow(video);
    } else if (youtubePlayer) {
      if (data.startSeconds !== null) youtubePlayer.seekTo(data.startSeconds, true);
      clampYouTubePlayer(youtubePlayer);
    } else if (data.previewUrl) {
      setLocalPreview(data.previewUrl, data.startSeconds);
    }
  }

  // ── Grab ──────────────────────────────────────────────────────────────────
  grabButton.addEventListener("click", async () => {
    try {
      setBusy(true, "Loading preview…", "loading");
      downloadArea.innerHTML = "";
      const data = await postJson("/api/grab", { url: urlInput.value });
      await setYouTubePreview(data.videoId, data.startSeconds);
      clipControls.classList.add("visible");
      setStatus("Preview loaded. If the embed is blocked, local preview will download automatically.", "idle");
    } catch (err) {
      setStatus(err.message, "error");
    } finally {
      setBusy(false);
    }
  });

  // ── Chop ──────────────────────────────────────────────────────────────────
  chopButton.addEventListener("click", async () => {
    try {
      const { startPayload, endPayload } = validateClipWindow(true);
      setBusy(true, "Cutting clip…", "loading");
      const data = await postJson("/api/chop", {
        start: startPayload, end: endPayload, fileName: fileNameInput.value,
      });
      setStatus(`Clip saved:\n${data.outputPath}`, "success");
      downloadArea.innerHTML = "";
      const a = document.createElement("a");
      a.href = data.downloadUrl;
      a.download = data.fileName;
      a.className = "download-btn";
      a.textContent = "⬇ Download clip";
      downloadArea.appendChild(a);
      fileNameInput.value = data.nextFileName;
      clipControls.classList.remove("visible");
      clipBarWrapper.classList.remove("visible");
      previewBox.innerHTML = '<div class="empty-preview">Clip created. Grab another video to continue.</div>';
    } catch (err) {
      setStatus(err.message, "error");
    } finally {
      setBusy(false);
    }
  });

  // ── Time input listeners ──────────────────────────────────────────────────
  startInput.addEventListener("change", async () => {
    updateDuration();
    try { await refreshPreview(); } catch { /* ignore */ }
  });
  endInput.addEventListener("change", () => {
    updateDuration();
    const video = previewBox.querySelector("video");
    try {
      if (video) enforcePreviewWindow(video);
      if (youtubePlayer) clampYouTubePlayer(youtubePlayer);
    } catch { /* ignore */ }
  });
  startInput.addEventListener("input", updateDuration);
  endInput.addEventListener("input",   updateDuration);

  // ── Cookie status & upload ────────────────────────────────────────────────
  const cookieStatus     = document.getElementById("cookieStatus");
  const cookieStatusText = document.getElementById("cookieStatusText");
  const cookieFileInput  = document.getElementById("cookieFileInput");

  async function refreshCookieStatus() {
    try {
      const res  = await fetch("/api/cookie-status");
      const data = await res.json();
      if (data.loaded) {
        const label = data.source === "secret" ? "Secret file" : "Uploaded";
        cookieStatusText.textContent = `Cookies active (${label})`;
        cookieStatus.className = "cookie-status loaded";
      } else {
        cookieStatusText.textContent = "No cookies — YouTube may block downloads";
        cookieStatus.className = "cookie-status missing";
      }
    } catch {
      cookieStatusText.textContent = "Cookie status unknown";
      cookieStatus.className = "cookie-status";
    }
  }

  cookieFileInput.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const content = await file.text();
      await postJson("/api/cookies", { content });
      cookieStatusText.textContent = "Cookies uploaded — refreshing…";
      await refreshCookieStatus();
    } catch (err) {
      setStatus(err.message, "error");
    } finally {
      e.target.value = "";
    }
  });

  refreshCookieStatus();
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
        if route == "/api/cookie-status":
            self.handle_cookie_status()
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
            elif self.path == "/api/cookies":
                self.handle_upload_cookies()
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

    def handle_cookie_status(self) -> None:
        cookies = get_active_cookies_file()
        if cookies is None:
            self.send_json({"ok": True, "loaded": False})
            return
        source = "secret" if str(cookies) == YTDLP_COOKIES_FILE else "upload"
        self.send_json({"ok": True, "loaded": True, "source": source})

    def handle_upload_cookies(self) -> None:
        payload = self.read_json()
        content = str(payload.get("content", "")).strip()
        if not content:
            raise ValueError("Cookie file content is empty.")
        if "# Netscape HTTP Cookie File" not in content and "# HTTP Cookie File" not in content:
            raise ValueError("This does not look like a Netscape cookies.txt file. Export cookies using a browser extension such as 'Get cookies.txt LOCALLY'.")
        SESSION_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_COOKIES_FILE.write_text(content, encoding="utf-8")
        self.send_json({"ok": True})

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


def get_active_cookies_file() -> Path | None:
    """Return the first cookies file that exists: env-configured, then session-uploaded."""
    for path in (Path(YTDLP_COOKIES_FILE), SESSION_COOKIES_FILE):
        if path.exists():
            return path
    return None


def get_writable_cookies_file() -> Path | None:
    """Return a writable cookies file for yt-dlp.

    yt-dlp rewrites the cookie file after each run to persist rotated tokens.
    On Render, /etc/secrets/ is read-only, so we copy the secret file to a
    writable location inside OUTPUT_DIR before passing it to yt-dlp.
    """
    source = get_active_cookies_file()
    if source is None:
        return None
    if os.access(source, os.W_OK):
        return source
    writable = OUTPUT_DIR / "_cookies_working.txt"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, writable)
    return writable


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
        # Use the Android and web_creator clients. Android bypasses the bot-check
        # that YouTube applies to datacenter IPs; web_creator is a fallback that
        # also tends to avoid it. The default web client requires PO tokens from
        # datacenter IPs and will 403 even with valid cookies.
        "--extractor-args",
        "youtube:player_client=android,web_creator",
        "-f",
        DOWNLOAD_FORMAT,
        "-o",
        str(OUTPUT_DIR / SOURCE_FILE_TEMPLATE),
        "--print-to-file",
        "after_move:%(filepath)s",
        str(path_log),
    ]

    cookies_file = get_writable_cookies_file()
    if cookies_file:
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
            raise RuntimeError(
                f"{step_name} failed:\n{friendly_command_error(details)}"
            )
        raise RuntimeError(f"{step_name} failed with exit code {completed.returncode}.")


def friendly_command_error(details: str) -> str:
    """Add deployment-specific guidance for common yt-dlp failures."""
    if (
        "Sign in to confirm" in details
        or "not a bot" in details
        or "HTTP Error 429" in details
    ):
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
