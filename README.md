# 🎬 Bot de Telegram: Descargador Multi-Plataforma con Arquitectura Zero-Cookies & Sistema VIP

Bot de Telegram desarrollado en Python para descargar videos, fotos y música en alta calidad desde **TikTok, X (Twitter), Instagram, YouTube, Spotify y Facebook**, con sistema integrado de membresías **VIP vs NO VIP PASS**, control de cuotas diarias, estadísticas personales y arquitectura **Zero-Cookies** optimizada para despliegue en **Render**.

---

## ✨ Características Principales

1. **Arquitectura Zero-Cookies (Sin Depender de Sesiones ni Cookies):**
   * **TikTok:** Extracción directa sin marca de agua vía `TikWM API`.
   * **X (Twitter):** Extracción directa en HD y fotos vía `FxTwitter API` (soporta contenido +18/sensible sin requerir inicio de sesión).
   * **Instagram:** Extracción multi-capa anónima (Polaris Web GraphQL con impersonación TLS Chrome 124 mediante `curl_cffi` + Bridge APIs). No exige `INSTAGRAM_SESSIONID`.
   * **YouTube:** Descarga asistida por Bridge API y clientes móviles rotativos de `yt-dlp` (`visionos`, `android_vr`, `tv`) con `yt-dlp-ejs` para resolver firmas JavaScript sin error 429 en Render.
   * **Spotify:** Extracción de metadatos oficiales vía oEmbed, descarga de audio en alta fidelidad y etiquetado ID3 completo (portada HD, título y artista incrustados con `mutagen`).
   * **Facebook:** Extracción de reels y videos públicos.

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

---

## 🛠️ Instalación y Ejecución Local

### 1. Clonar o ingresar a la carpeta
```bash
cd /home/jonparrow/Documentos/bot2
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
pip install -r requirements.txt
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
