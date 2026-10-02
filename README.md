# 🎬 Bot de Telegram: Descargador Multi-Plataforma Anónimo & Sistema VIP

Bot de Telegram desarrollado en Python para descargar videos, fotos y música desde **TikTok, X (Twitter), Instagram, YouTube, Spotify y Facebook**, con membresías **VIP vs NO VIP PASS**, cuotas diarias y estadísticas. Funciona **sin cookies, tokens ni cuentas** de las plataformas: lo único obligatorio es el token del bot de Telegram.

---

## ✨ Características Principales

1. **Descargas 100% anónimas (sin cookies ni sesiones):** cada plataforma tiene una cadena de estrategias que se prueban en orden, por cada ruta de red disponible (IPv4 directa → IPv6 rotativa → Cloudflare WARP). Si una estrategia falla varias veces por bloqueo de IP, se pausa 10 minutos (circuit breaker).

   | Plataforma | Estrategias (en orden) |
   | :--- | :--- |
   | 🎵 TikTok | API TikWM → yt-dlp → Cobalt |
   | 🐦 X (Twitter) | FxTwitter → VxTwitter → API syndication → yt-dlp |
   | 📸 Instagram | Página embed → Polaris GraphQL anónimo → yt-dlp → Cobalt → OpenGraph |
   | ▶️ YouTube | yt-dlp con PO tokens sin cuenta (bgutil) y rotación de clientes → Cobalt |
   | 🟢 Spotify | Metadatos del embed público (canción, álbum o playlist) → audio equivalente en SoundCloud/YouTube elegido por duración, etiquetas ID3 y portada |
   | 👥 Facebook | yt-dlp → OpenGraph |

   Errores claros: el bot distingue entre *IP bloqueada*, *demasiadas peticiones*, *+18/región*, *privado* y *no existe*. El contenido privado o con restricción de edad **no** se puede descargar sin cuenta.

2. **👑 Sistema de Membresías y Límites:**
   | Rango | Estado en `/stats` | Cuota Diaria | Plataformas Permitidas |
   | :--- | :--- | :--- | :--- |
   | 🆓 **NO VIP** | `NO VIP PASS` | **5 descargas / día** | 📸 **Instagram**, 🎵 **TikTok**, 🐦 **X (Twitter)** |
   | 👑 **VIP** | `VIP` | **15 descargas / día** | 🌐 **Todas** (YouTube MP3/MP4, Spotify MP3, Facebook, IG, TikTok, X) |

3. **🔍 Detección Automática de VIP en Grupos:**
   * Reconoce de forma automática como **VIP** a cualquier miembro cuyo **Título Personalizado (Custom Title)** en el grupo contenga la palabra **`VIP`** (ejemplos: `DROGUITA - VIP`, `ALFREDO PC - VIP`, `ADMIN`), o a los creadores/administradores del grupo.

4. **🧹 Auto-Limpieza en Grupos:**
   * Si el bot cuenta con el permiso de Administrador (*Eliminar mensajes*), borra automáticamente el mensaje con el enlace original una vez enviado el archivo multimedia.

5. **⚡ Caché Ultra Rápida (SQLite):**
   * Si un enlace ya fue descargado previamente, se entrega en menos de **0.5 segundos** mediante el `file_id` de Telegram.

6. **⏰ Reinicio de Cuotas a las 12:00 AM (Medianoche Local):**
   * Tarea programada en segundo plano que reinicia las cuotas diarias automáticamente a las 00:00:00 hora local (configurable con `TIMEZONE` en `.env`).

---

## 📋 Lista de Comandos

| Comando | Descripción |
| :--- | :--- |
| `/start` | Muestra el mensaje de bienvenida y menú principal con botones interactivos. |
| `/stats` o `/estadisticas` | Muestra tu consumo diario, estado VIP y descargas totales. |
| `/panel` o `/grupo` | Menú interactivo con enlace para añadir el bot a tu grupo. |
| `/help` | Guía de uso y reglas de acceso. |
| `/about` | Información técnica del bot y versión. |
| `/vip [user_id]` | (Solo Admins) Asigna manualmente el rango VIP a un usuario. |
| `/unvip [user_id]` | (Solo Admins) Quita el rango VIP a un usuario. |
| `/mp3 [enlace]` | Fuerza la descarga en formato de audio MP3. |
| `/mp4 [enlace]` | Fuerza la descarga en formato de video MP4. |
| `/diag` | (Solo Admins) Muestra la IP de salida, qué plataformas bloquean al servidor y la salud de cada estrategia. |

---

## 🛠️ Instalación y Ejecución Local

### 1. Clonar o ingresar a la carpeta
```bash
cd botnew
```

### 2. Configurar el archivo `.env`
```bash
cp .env.example .env
nano .env
```
Añade tu token de Telegram:
```env
TELEGRAM_BOT_TOKEN=tu_token_aqui
TIMEZONE=America/New_York
```

### 3. Instalar dependencias
```bash
pip install -r requirements-dev.txt   # incluye pytest
pytest                                 # tests unitarios (sin red)
pytest -m live                         # descargas reales contra cada plataforma
```

### 4. Probar una descarga en la terminal (Sin abrir Telegram)
```bash
python3 test_download.py "https://www.tiktok.com/@tiktok/video/..."
```

### 5. Iniciar el bot
```bash
./run.sh
# O directamente:
python3 bot.py
```

---

## 🖥️ Despliegue en un VPS (recomendado)

Las plataformas bloquean las IPs de datacenter (Render, VPN, VPS). El bot lo compensa usando
servicios externos cuando puede y repartiendo el tráfico entre varias salidas **gratuitas** del propio VPS.

```bash
# 1. Diagnóstico inicial: ¿qué plataformas bloquean tu VPS?
python3 diag.py

# 2. Rotación IPv6 (la mejora más grande; casi todos los VPS traen un /64)
sudo bash deploy/setup_ipv6.sh          # imprime IPV6_PREFIX=... para el .env

# 3. Cloudflare WARP como salida alternativa (gratis, sin cuenta)
sudo bash deploy/setup_warp.sh          # imprime WARP_PROXY=... para el .env

# 4. Arrancar bot + servidor de PO tokens de YouTube
docker compose up -d --build

# 5. Diagnóstico otra vez, ahora por cada ruta
docker compose exec telegram-downloader-bot python diag.py
```

`yt-dlp` se actualiza solo en cada arranque del contenedor (`AUTO_UPDATE_YTDLP=true`). Para mantenerlo al día,
puedes reiniciarlo una vez al día con cron: `0 5 * * * cd /ruta/botnew && docker compose restart telegram-downloader-bot`.

> ⚠️ No configures cuentas de Instagram en el VPS: iniciar sesión desde una IP de datacenter hace que Meta
> suspenda la cuenta. Por eso `ANONYMOUS_ONLY=true` viene activado por defecto.

---

## ☁️ Despliegue en Render (Plan Gratuito)

Este bot está 100% preparado para desplegarse como **Web Service** gratuito en Render.

1. Sube este repositorio a tu cuenta de **GitHub**.
2. Entra a [dashboard.render.com](https://dashboard.render.com) y haz clic en **New +** -> **Web Service**.
3. Selecciona tu repositorio de GitHub.
4. Render detectará automáticamente el archivo `Dockerfile`.
5. En la sección de **Environment Variables** (Variables de entorno), agrega:
   * `TELEGRAM_BOT_TOKEN`: El token obtenido de @BotFather.
   * `TIMEZONE`: `America/New_York` (o tu zona horaria preferida).
   * `PORT`: `8080` (para el servidor de salud).
6. Haz clic en **Create Web Service**.
7. En unos minutos, Render construirá la imagen Docker (con Python, FFmpeg y Node.js), levantará el servidor web en el puerto 8080 para los chequeos de salud (`/`), y el bot comenzará a responder en Telegram inmediatamente.
