FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=10000 \
    AUTO_OPEN_BROWSER=0 \
    OUTPUT_DIR=/tmp/youtube_clips

WORKDIR /app

# ffmpeg does the actual clipping. ca-certificates helps yt-dlp reach HTTPS
# video hosts reliably from the slim base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip yt-dlp

COPY clipper.py /app/clipper.py

# Render's default web-service port is 10000, and it also injects PORT at
# runtime. clipper.py reads PORT, so overriding it in Render still works.
EXPOSE 10000

CMD ["python", "clipper.py"]
