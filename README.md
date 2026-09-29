# 🎬 Bot de Telegram para Descarga de Videos (Instagram, TikTok, Facebook, YouTube)

Bot de Telegram desarrollado en Python capaz de descargar videos automáticamente de múltiples plataformas:
- 📸 **Instagram:** Reels, videos y publicaciones públicas.
- 🎵 **TikTok:** Videos en alta calidad y sin marca de agua.
- 👥 **Facebook:** Reels y videos públicos de páginas y perfiles.
- ▶️ **YouTube:** Shorts y videos estándar.
- 🐦 **X (Twitter), Reddit, Threads** y muchas otras plataformas compatibles con `yt-dlp`.

---

## 🚀 Requisitos Previos

- **Python 3.10+** (probado con éxito en Python 3.14)
- **FFmpeg** (ya instalado en tu sistema en `/usr/bin/ffmpeg`)

---

## 🛠️ Instalación y Configuración

### 1. Obtener el Token del Bot en Telegram

1. Abre la app de Telegram y busca al usuario [@BotFather](https://t.me/BotFather).
2. Envía el comando `/newbot`.
3. Dale un nombre a tu bot (ejemplo: `Mi Descargador de Videos`).
4. Dale un nombre de usuario que termine en `bot` (ejemplo: `mivid_downloader_bot`).
5. Copia el **HTTP API Token** que te proporciona BotFather (ejemplo: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`).

### 2. Configurar el archivo `.env`

Abre el archivo `.env` en este directorio y añade tu token:

```env
TELEGRAM_BOT_TOKEN=tu_token_aqui_sin_comillas
COOKIES_FILE=cookies.txt
```

---

## ▶️ Ejecución del Bot

Puedes iniciar el bot de dos maneras:

### Opción A: Usando el script automático (Recomendado)

```bash
./run.sh
```

### Opción B: Ejecutando manualmente con el entorno virtual

```bash
source venv/bin/activate
python bot.py
```

---

## 👥 Funciones Especiales para Grupos

- **➕ Panel para añadir a grupos:**
  - El bot incluye botones interactivos para añadirlo directamente a cualquier grupo de Telegram con un solo toque.
  - Comando `/panel` o `/grupo` para ver instrucciones y permisos recomendados.
- **🧹 Auto-Eliminación del enlace original:**
  - Cuando alguien envía un enlace en el grupo, el bot descarga el video, lo envía mencionando a la persona (`👤 Pedido por: @usuario`) y **elimina automáticamente el mensaje que contenía el link** para mantener el grupo limpio y sin spam.
  - *(Nota: Para que el bot pueda borrar los enlaces de otros usuarios, debe tener permiso de Administrador con **"Eliminar mensajes"**)*.
- **⚡ Envíos Instantáneos con Caché SQLite:**
  - Si un usuario pide un video y otro usuario (o en otro grupo) piden el mismo video, se envía en menos de 0.5 segundos usando el almacenamiento interno de Telegram.
- **🤫 Modo Anti-Spam:**
  - El bot ignora los mensajes normales de conversación en los grupos; solo interviene si se detecta un enlace de video o el comando `/dl`.

---

## 📱 ¿Cómo funciona el Bot?

1. En Telegram, busca tu bot y pulsa el botón **Iniciar** o envía `/start`.
2. Ve a Instagram, TikTok, Facebook o YouTube, copia el enlace del video que desees y pégalo directamente en el chat con tu bot.
3. El bot:
   - Identificará automáticamente la red social.
   - Mostrará el estado de la descarga en tiempo real.
   - Extraerá o generará la miniatura (thumbnail) adecuada.
   - Subirá el video en formato MP4 con información de título, duración y tamaño.
   - Limpiará automáticamente los archivos del disco al finalizar para no ocupar espacio.

---

## ⚠️ Notas Importantes y Límites

- **Límite de tamaño de Telegram:** La API estándar de bots de Telegram impone un límite máximo de **50 MB** por archivo.
  - Para videos que superan ligeramente este límite, el bot incluye un sistema de compresión automática con FFmpeg para intentar ajustarlo por debajo de los 50 MB.
- **Videos Privados:** Los videos deben ser públicos para que el bot pueda acceder a ellos.
- **Cookies (Opcional):** Si Instagram o Facebook llegan a solicitar inicio de sesión en tu servidor, puedes exportar tus cookies en formato Netscape (`cookies.txt`) y colocarlas en la carpeta raíz del proyecto.
