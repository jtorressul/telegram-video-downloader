# 🎬 Bot de Telegram: YouTube & YouTube Music Downloader (MP3 & MP4)

Bot de Telegram desarrollado en Python especializado en descargar de **YouTube** y **YouTube Music**:
- 🎵 **MP3 (Audio):** Canciones y audios en alta calidad (192 kbps) con carátula oficial y etiquetas de artista.
- 🎬 **MP4 (Video):** Videos en alta resolución optimizados para Telegram (hasta 50 MB con compresión automática).

---

## ✨ Características Principales

1. **Selector Interactivo de Formato:** Al enviar cualquier enlace de YouTube o YouTube Music, el bot muestra dos botones:
   - `[ 🎵 Descargar MP3 (Audio) ]`
   - `[ 🎬 Descargar MP4 (Video) ]`
2. **Comandos Directos:**
   - `/mp3 [enlace]` - Descarga directamente en audio MP3.
   - `/mp4 [enlace]` - Descarga directamente en video MP4.
   - `/dl [enlace]` - Muestra el selector de formato.
3. **Anti-Bloqueo de YouTube:**
   - Emula clientes oficiales (Android / iOS) para evitar los bloqueos de *"Sign in to confirm you're not a bot"*.
4. **Optimizado para Grupos:**
   - Botón directo para añadir a grupos con permisos de Administrador.
   - **Auto-eliminación:** Elimina el mensaje del enlace original en grupos tras enviar el archivo descargado.
   - Modo silencioso contra spam de mensajes normales.
5. **Caché Ultra Rápida (SQLite):**
   - Si un usuario pide una canción o video ya descargado, se reenvía en **menos de 0.5 segundos**.
6. **24/7 en la Nube (Render / Koyeb):**
   - Incluye microservidor web interno en `$PORT` y `Dockerfile` para funcionar en la nube sin costo.

---

## 🛠️ Instalación y Configuración Local

1. Configura tu token en `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=tu_token_de_botfather
   ```
2. Ejecuta el script:
   ```bash
   ./run.sh
   ```
