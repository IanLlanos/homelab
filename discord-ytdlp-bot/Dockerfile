FROM python:3.12-slim

# ffmpeg merges video+audio and embeds thumbnails/subtitles/chapters
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime (Deno) for full YouTube support
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py entrypoint.sh ./
# strip Windows line endings in case the script was edited on Windows
RUN sed -i 's/\r$//' entrypoint.sh

CMD ["sh", "/app/entrypoint.sh"]
