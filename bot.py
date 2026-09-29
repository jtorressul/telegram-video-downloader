import os
import re
import html
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from downloader import VideoDownloader, detect_platform, format_duration
from cache import VideoCache

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt").strip()

# Initialize video downloader and cache
downloader = VideoDownloader(cookies_file=COOKIES_FILE if os.path.exists(COOKIES_FILE) else None)
cache = VideoCache()

# Concurrency limiter (allows up to 4 parallel downloads)
download_semaphore = asyncio.Semaphore(4)

URL_REGEX = re.compile(
    r'(https?://(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?://[a-zA-Z0-9]+\.[^\s]{2,})'
)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy cloud health checks (like Render Web Service)."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Bot de Telegram activo y funcionando.")

    def log_message(self, format, *args):
        pass  # Silence access logs


def start_health_server():
    """Starts the health check HTTP server on PORT environment variable if available."""
    port_str = os.getenv("PORT")
    if port_str:
        try:
            port = int(port_str)
            server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logger.info(f"Servidor web de salud iniciado en el puerto {port} (Render / Cloud)")
        except Exception as e:
            logger.warning(f"No se pudo iniciar el servidor web de salud: {e}")


def format_filesize(size_bytes: int) -> str:
    """Formats bytes to MB."""
    return f"{size_bytes / (1024 * 1024):.1f} MB"


async def post_init(application: Application) -> None:
    """Called after application initializes to fetch and cache bot username."""
    bot_info = await application.bot.get_me()
    application.bot_data['username'] = bot_info.username
    logger.info(f"Bot iniciado exitosamente como @{bot_info.username}")


async def keep_chat_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: ChatAction, stop_event: asyncio.Event):
    """Periodically sends chat action while downloading/uploading."""
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Gets the bot username reliably, fetching from API if not yet cached."""
    username = context.bot_data.get('username')
    if not username:
        try:
            bot_info = await context.bot.get_me()
            username = bot_info.username
            context.bot_data['username'] = username
        except Exception as e:
            logger.warning(f"Error fetching bot username: {e}")
            username = ""
    return username or ""


def get_start_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Creates the main interactive keyboard with direct Add to Group buttons."""
    # Deep links for adding the bot to a group:
    # 1. With admin rights (delete_messages) for auto-cleaning links
    # 2. As standard member
    add_group_admin_url = (
        f"https://t.me/{bot_username}?startgroup=botstart&admin=delete_messages"
        if bot_username else "https://t.me"
    )
    add_group_normal_url = (
        f"https://t.me/{bot_username}?startgroup=botstart"
        if bot_username else "https://t.me"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Añadir a un Grupo (Auto-Borrar Links)", url=add_group_admin_url),
        ],
        [
            InlineKeyboardButton("➕ Añadir a un Grupo (Miembro Normal)", url=add_group_normal_url),
        ],
        [
            InlineKeyboardButton("👥 Panel de Grupos", callback_data="group_panel"),
            InlineKeyboardButton("📖 Ayuda & Comandos", callback_data="help_menu")
        ],
        [
            InlineKeyboardButton("ℹ️ Acerca de", callback_data="about_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_group_panel_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Creates keyboard for group management panel."""
    add_group_url = (
        f"https://t.me/{bot_username}?startgroup=botstart&admin=delete_messages"
        if bot_username else "https://t.me"
    )
    keyboard = [
        [
            InlineKeyboardButton("➕ Añadir este Bot a mi Grupo", url=add_group_url),
        ],
        [
            InlineKeyboardButton("⚙️ Permisos Recomendados", callback_data="group_perms"),
            InlineKeyboardButton("📖 Comandos para Grupos", callback_data="group_commands"),
        ],
        [
            InlineKeyboardButton("◀️ Volver al Inicio", callback_data="main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /start command."""
    user = update.effective_user
    name = html.escape(user.first_name) if user and user.first_name else "amigo"
    chat_type = update.effective_chat.type
    bot_username = await get_bot_username(context)

    if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        panel_url = f"https://t.me/{bot_username}?start=help" if bot_username else "https://t.me"
        keyboard = [[InlineKeyboardButton("⚙️ Ver Panel del Bot", url=panel_url)]]
        await update.message.reply_html(
            f"👋 ¡Hola a todos! Soy el bot descargador de videos.\n\n"
            "✨ <b>¿Cómo usarme en este grupo?</b>\n"
            "• Compartan cualquier enlace de <b>Instagram, TikTok, Facebook o YouTube</b>.\n"
            "• Usen el comando <code>/dl [enlace]</code> si lo prefieren.\n\n"
            "🧹 <i>Si me dan permisos de Administrador (Eliminar mensajes), borraré automáticamente los enlaces y dejaré solo el video limpio.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    welcome_text = (
        f"👋 ¡Hola, <b>{name}</b>!\n\n"
        "Soy tu bot para descargar videos y música a <b>máxima velocidad</b>.\n\n"
        "✨ <b>Plataformas compatibles:</b>\n"
        "• 🟢 <b>Spotify:</b> Canciones en MP3 con carátula oficial\n"
        "• 📸 <b>Instagram:</b> Reels, videos y publicaciones\n"
        "• 🎵 <b>TikTok:</b> Videos sin marca de agua\n"
        "• 👥 <b>Facebook:</b> Reels y videos públicos\n"
        "• ▶️ <b>YouTube:</b> Shorts y videos\n"
        "• 🐦 <b>X (Twitter), Threads, Reddit</b> y más\n\n"
        "👥 <b>¡Añádeme a tus grupos con un solo toque!</b>\n"
        "Usa los botones de abajo para añadirme y activaré la descarga y auto-limpieza de links en tu grupo.\n\n"
        "📥 <b>O pruébame aquí:</b> Envíame cualquier enlace de video o canción."
    )

    await update.message.reply_html(
        welcome_text,
        reply_markup=get_start_keyboard(bot_username)
    )


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /panel or /grupo command."""
    bot_username = await get_bot_username(context)
    panel_text = (
        "👥 <b>PANEL DE GESTIÓN PARA GRUPOS</b>\n\n"
        "¡Puedes añadir este bot a cualquier grupo de amigos, familia o trabajo!\n\n"
        "⚡ <b>Ventajas en Grupos:</b>\n"
        "• <b>Detección automática:</b> Descarga enlaces sin obligar a escribir comandos.\n"
        "• <b>Limpieza de chat:</b> Elimina el mensaje del link una vez enviado el video.\n"
        "• <b>Mención al usuario:</b> Indica quién solicitó el video para no perder el hilo.\n"
        "• <b>Caché Compartida:</b> Si dos miembros piden el mismo video, se envía en menos de 0.5 segundos.\n"
        "• <b>Anti-Spam:</b> No responde a mensajes comunes de charla."
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            panel_text,
            reply_markup=get_group_panel_keyboard(bot_username),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_html(
            panel_text,
            reply_markup=get_group_panel_keyboard(bot_username)
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /help command."""
    help_text = (
        "📖 <b>Guía de Uso & Consejos:</b>\n\n"
        "1️⃣ Ve a Instagram, TikTok, Facebook o YouTube.\n"
        "2️⃣ Pulsa <i>Compartir</i> y luego <i>Copiar enlace</i>.\n"
        "3️⃣ Envíalo al chat privado o en cualquier grupo con el bot.\n\n"
        "💡 <b>Comandos disponibles:</b>\n"
        "• <code>/start</code> - Menú principal y bienvenida\n"
        "• <code>/panel</code> o <code>/grupo</code> - Panel para añadir a grupos\n"
        "• <code>/dl [enlace]</code> - Descarga manual en grupos\n"
        "• <code>/help</code> - Esta ayuda interactiva\n\n"
        "⚠️ <b>Límite de tamaño:</b> Telegram permite enviar videos de hasta <b>50 MB</b>. "
        "El bot optimiza y comprime automáticamente los videos que superen ligeramente este límite."
    )

    keyboard = [
        [InlineKeyboardButton("◀️ Volver al Inicio", callback_data="main_menu")]
    ]

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_html(
            help_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /about command."""
    about_text = (
        "ℹ️ <b>Acerca de este Bot:</b>\n\n"
        "🤖 <b>Versión:</b> 2.1.0\n"
        "⚡ <b>Motor:</b> Python + yt-dlp + FFmpeg\n"
        "🗄️ <b>Base de Datos:</b> SQLite Cache para envíos instantáneos\n"
        "🧹 <b>Auto-Limpieza:</b> Borra links en grupos y elimina archivos temporales del servidor."
    )

    keyboard = [
        [InlineKeyboardButton("◀️ Volver al Inicio", callback_data="main_menu")]
    ]

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            about_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_html(
            about_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard button callbacks."""
    query = update.callback_query
    data = query.data
    bot_username = await get_bot_username(context)

    if data == "main_menu":
        user = update.effective_user
        name = html.escape(user.first_name) if user and user.first_name else "amigo"
        welcome_text = (
            f"👋 ¡Hola, <b>{name}</b>!\n\n"
            "Soy tu bot para descargar videos de redes sociales a <b>máxima velocidad</b>.\n\n"
            "✨ <b>Plataformas compatibles:</b>\n"
            "• 📸 <b>Instagram</b> • 🎵 <b>TikTok</b> • 👥 <b>Facebook</b> • ▶️ <b>YouTube</b>\n\n"
            "📥 Envíame cualquier enlace para comenzar."
        )
        await query.answer()
        await query.edit_message_text(
            welcome_text,
            reply_markup=get_start_keyboard(bot_username),
            parse_mode=ParseMode.HTML
        )
    elif data == "group_panel":
        await panel_command(update, context)
    elif data == "help_menu":
        await help_command(update, context)
    elif data == "about_menu":
        await about_command(update, context)
    elif data == "group_perms":
        perms_text = (
            "⚙️ <b>PERMISOS RECOMENDADOS EN GRUPOS</b>\n\n"
            "Para una experiencia óptima, asigna estos permisos al bot en el grupo:\n\n"
            "1. 🗑️ <b>Eliminar mensajes:</b>\n"
            "Permite al bot borrar el enlace original enviado por el usuario, dejando solo el video limpio en el chat.\n\n"
            "2. 📤 <b>Enviar archivos multimedia (Videos):</b>\n"
            "Necesario para poder subir los videos al grupo.\n\n"
            "3. 👁️ <b>Leer mensajes (Modo Privacidad Desactivado):</b>\n"
            "En @BotFather ejecuta <code>/setprivacy</code> -> <i>Disable</i>, o haz al bot Administrador para que detecte los links automáticamente."
        )
        keyboard = [
            [InlineKeyboardButton("◀️ Volver al Panel", callback_data="group_panel")]
        ]
        await query.answer()
        await query.edit_message_text(
            perms_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    elif data == "group_commands":
        cmd_text = (
            "📖 <b>COMANDOS PARA GRUPOS</b>\n\n"
            "• <code>/dl [enlace]</code> - Descarga manual de un video.\n"
            "• <b>Respuesta con /dl:</b> Responde a cualquier mensaje que contenga un link escribiendo <code>/dl</code>.\n"
            "• <b>Pega directa:</b> Si el bot es Admin o tiene el modo privacidad desactivado, detecta el link automáticamente sin comandos.\n"
            "• <code>/panel</code> - Abre el panel interactivo."
        )
        keyboard = [
            [InlineKeyboardButton("◀️ Volver al Panel", callback_data="group_panel")]
        ]
        await query.answer()
        await query.edit_message_text(
            cmd_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the group when added and explains features."""
    bot_id = context.bot.id
    for member in update.message.new_chat_members:
        if member.id == bot_id:
            await update.message.reply_html(
                "🎉 <b>¡Hola! Gracias por añadirme a este grupo.</b>\n\n"
                "📹 Descargaré videos de <b>Instagram, TikTok, Facebook y YouTube</b> automáticamente.\n\n"
                "💡 <b>Para activar la auto-limpieza:</b>\n"
                "Hazme administrador del grupo con permiso de <b>'Eliminar mensajes'</b> para que pueda borrar los enlaces y dejar solo los videos limpios.\n\n"
                "¡Ya pueden empezar a compartir enlaces!"
            )
            break


async def process_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Handles the downloading, caching, sending, and auto-deletion in groups."""
    chat = update.effective_chat
    chat_id = chat.id
    user_msg = update.message
    message_id = user_msg.message_id
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
    platform, emoji = detect_platform(url)

    # Format user mention and safe URL for hyperlink
    user = update.effective_user
    user_mention = user.mention_html() if user else "Usuario"
    clean_url = html.escape(url)

    # 1. Check Cache for Instant Delivery (< 0.5s)
    cached_data = cache.get(url)
    if cached_data:
        try:
            cached_title = cached_data.get('title', 'Video')
            clean_title = html.escape(cached_title[:200] + ('...' if len(cached_title) > 200 else ''))
            duration = cached_data.get('duration')
            filesize = cached_data.get('filesize', 0)
            is_audio = bool(cached_data.get('is_audio'))
            performer = cached_data.get('performer')
            clean_artist = html.escape(performer or '')

            if is_audio:
                caption = (
                    f"🎵 <b>{clean_title}</b>\n"
                    + (f"🎤 <b>Artista:</b> {clean_artist}\n" if clean_artist else "")
                    + f"🟢 <b>Plataforma:</b> Spotify\n"
                    f"⏱ <b>Duración:</b> {format_duration(duration)}\n"
                    f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                    f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                    f"🔗 <a href=\"{clean_url}\">Link original</a>\n"
                    f"⚡ <i>Descarga instantánea</i>"
                )
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=cached_data['file_id'],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=cached_title,
                    performer=performer,
                    duration=duration,
                )
            else:
                caption = (
                    f"🎬 <b>{clean_title}</b>\n\n"
                    f"{emoji} <b>Plataforma:</b> {cached_data.get('platform', platform)}\n"
                    f"⏱ <b>Duración:</b> {format_duration(duration)}\n"
                    f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                    f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                    f"🔗 <a href=\"{clean_url}\">Link original</a>\n"
                    f"⚡ <i>Descarga instantánea</i>"
                )
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=cached_data['file_id'],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )

            # Auto-delete original message with link in groups
            if is_group:
                try:
                    await user_msg.delete()
                except Exception as del_err:
                    logger.debug(f"No se pudo eliminar mensaje en grupo (requiere admin): {del_err}")

            return
        except Exception as e:
            logger.info(f"Cached video failed to send, downloading fresh: {e}")

    # 2. Fresh download
    item_type = "canción" if emoji == "🟢" else "video"
    status_message = await user_msg.reply_html(
        f"⏳ {emoji} <b>Procesando {item_type} de {platform}...</b>"
    )

    stop_chat_action = asyncio.Event()
    chat_action = ChatAction.UPLOAD_VOICE if emoji == "🟢" else ChatAction.UPLOAD_VIDEO
    chat_action_task = asyncio.create_task(
        keep_chat_action(context, chat_id, chat_action, stop_chat_action)
    )

    download_result = None
    try:
        async with download_semaphore:
            download_result = await downloader.download(url)

        try:
            await status_message.edit_text(
                f"⬆️ {emoji} <b>Subiendo {item_type} a Telegram...</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        file_path = download_result['file_path']
        thumb_path = download_result.get('thumbnail_path')
        title = download_result.get('title', 'Audio' if emoji == "🟢" else 'Video')
        duration = download_result.get('duration')
        width = download_result.get('width')
        height = download_result.get('height')
        filesize = download_result.get('filesize', 0)
        is_audio = bool(download_result.get('is_audio'))
        artist = download_result.get('artist')
        clean_artist = html.escape(artist or '')

        clean_title = html.escape(title[:200] + ('...' if len(title) > 200 else ''))

        if is_audio:
            caption = (
                f"🎵 <b>{clean_title}</b>\n"
                + (f"🎤 <b>Artista:</b> {clean_artist}\n" if clean_artist else "")
                + f"🟢 <b>Plataforma:</b> Spotify\n"
                f"⏱ <b>Duración:</b> {format_duration(duration)}\n"
                f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                f"🔗 <a href=\"{clean_url}\">Link original</a>"
            )
            with open(file_path, 'rb') as audio_fp:
                thumb_fp = open(thumb_path, 'rb') if (thumb_path and os.path.exists(thumb_path)) else None
                try:
                    sent_msg = await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_fp,
                        thumbnail=thumb_fp,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        title=title,
                        performer=artist,
                        duration=duration,
                        read_timeout=300,
                        write_timeout=300,
                    )
                    if sent_msg and sent_msg.audio:
                        cache.set(
                            url=url,
                            file_id=sent_msg.audio.file_id,
                            title=title,
                            platform='Spotify',
                            duration=duration,
                            width=None,
                            height=None,
                            filesize=filesize,
                            is_audio=True,
                            performer=artist,
                        )
                finally:
                    if thumb_fp:
                        thumb_fp.close()
        else:
            caption = (
                f"🎬 <b>{clean_title}</b>\n\n"
                f"{emoji} <b>Plataforma:</b> {platform}\n"
                f"⏱ <b>Duración:</b> {format_duration(duration)}\n"
                f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                f"🔗 <a href=\"{clean_url}\">Link original</a>"
            )

            with open(file_path, 'rb') as video_fp:
                thumb_fp = open(thumb_path, 'rb') if (thumb_path and os.path.exists(thumb_path)) else None
                try:
                    sent_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_fp,
                        thumbnail=thumb_fp,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        duration=duration,
                        width=width,
                        height=height,
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                    )

                    # Save file_id to cache
                    if sent_msg and sent_msg.video:
                        cache.set(
                            url=url,
                            file_id=sent_msg.video.file_id,
                            title=title,
                            platform=platform,
                            duration=duration,
                            width=width,
                            height=height,
                            filesize=filesize,
                            is_audio=False,
                        )
                finally:
                    if thumb_fp:
                        thumb_fp.close()

        # Delete status message
        try:
            await status_message.delete()
        except Exception:
            pass

        # In groups: Delete the original message containing the link!
        if is_group:
            try:
                await user_msg.delete()
            except Exception as del_err:
                logger.info(f"No se pudo eliminar el link original en el grupo (¿falta permiso de Eliminar mensajes?): {del_err}")

    except ValueError as val_err:
        logger.warning(f"Validation error for {url}: {val_err}")
        try:
            await status_message.edit_text(
                f"❌ <b>No se pudo descargar:</b>\n\n{html.escape(str(val_err))}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Unexpected error downloading {url}: {e}", exc_info=True)
        err_msg = (
            "❌ <b>Ocurrió un error al procesar el enlace.</b>\n"
            "Verifica que el enlace sea válido y el video sea público."
        )
        try:
            await status_message.edit_text(err_msg, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    finally:
        stop_chat_action.set()
        await chat_action_task
        if download_result:
            downloader.cleanup(download_result)


async def dl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /dl or /descargar command."""
    text = " ".join(context.args) if context.args else ""
    urls = URL_REGEX.findall(text)

    # Check if replying to another message with link
    if not urls and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        urls = URL_REGEX.findall(reply_text)

    if not urls:
        await update.message.reply_html(
            "ℹ️ <b>Uso del comando:</b>\n<code>/dl [enlace del video]</code>\n"
            "O responde a un mensaje con link escribiendo <code>/dl</code>."
        )
        return

    url = urls[0].strip()
    await process_video_link(update, context, url)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming messages."""
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    urls = URL_REGEX.findall(text)
    is_group = update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if not urls:
        if not is_group:
            bot_username = await get_bot_username(context)
            await update.message.reply_html(
                "ℹ️ <b>Enlace no detectado.</b>\n\n"
                "Por favor, envíame un enlace de video de <b>Instagram, TikTok, Facebook o YouTube</b>.",
                reply_markup=get_start_keyboard(bot_username)
            )
        return

    url = urls[0].strip()
    await process_video_link(update, context, url)


def main():
    """Main entrypoint for the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "TU_TOKEN_DE_TELEGRAM_AQUI":
        print("\n" + "=" * 60)
        print("❌ ERROR: TELEGRAM_BOT_TOKEN no está configurado.")
        print("1. Abre el archivo .env en esta carpeta.")
        print("2. Añade tu token obtenido de @BotFather:")
        print("   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ")
        print("=" * 60 + "\n")
        return

    print("🚀 Iniciando Bot de Descarga de Videos (Auto-Delete & Panel de Grupos)...")
    start_health_server()

    request = HTTPXRequest(
        read_timeout=300,
        write_timeout=300,
        connect_timeout=60,
        pool_timeout=60
    )

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(request).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["panel", "grupo", "grupos", "anadir", "agregar", "addgroup"], panel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler(["dl", "descargar", "bajar"], dl_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("✅ Bot listo y a la escucha. Presiona Ctrl+C para detenerlo.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
