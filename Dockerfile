FROM python:3.11-slim

# FFmpeg para audio/video, Node.js para yt-dlp-ejs, curl para diagnósticos
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x deploy/entrypoint.sh

EXPOSE 8080

CMD ["deploy/entrypoint.sh"]
