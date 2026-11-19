#!/bin/sh
# Update yt-dlp on every start: sites like YouTube break older versions often.
echo "Updating yt-dlp..."
if pip install --no-cache-dir --quiet --upgrade "yt-dlp[default]"; then
    echo "yt-dlp $(yt-dlp --version) ready"
else
    echo "yt-dlp update failed, using the installed version"
fi
exec python -u /app/bot.py
