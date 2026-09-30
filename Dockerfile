FROM python:3.11-slim

# Instalar dependencias del sistema: FFmpeg para audio/video, Node.js para yt-dlp-ejs y curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código del bot
COPY . .

# Puerto para health checks de Render
EXPOSE 8080

CMD ["python", "bot.py"]
