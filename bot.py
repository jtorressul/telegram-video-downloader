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

from downloader import (
    VideoDownloader,
    detect_platform,
    format_duration,
    extract_youtube_id,
    is_youtube_url,
)
from cache import VideoCache

# Load environment variables
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

# Initialize downloader and cache
downloader = VideoDownloader(cookies_file=COOKIES_FILE if os.path.exists(COOKIES_FILE) else None)
cache = VideoCache()

# Concurrency limiter
download_semaphore = asyncio.Semaphore(4)

URL_REGEX = re.compile(
    r'(https?://(?:www\.|m\.|music\.)?(?:youtube\.com/[^\s]+|youtu\.be/[a-zA-Z0-9_-]{11}[^\s]*))'
)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy cloud health checks (Render / Koyeb)."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - YouTube & YouTube Music Bot activo.")

    def log_message(self, format, *args):
        pass


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


def get_format_selection_keyboard(video_id: str) -> InlineKeyboardMarkup:
    """Keyboard to choose between MP3 (audio) and MP4 (video)."""
    keyboard = [
        [
            InlineKeyboardButton("🎵 Descargar MP3 (Audio)", callback_data=f"dl:mp3:{video_id}"),
            InlineKeyboardButton("🎬 Descargar MP4 (Video)", callback_data=f"dl:mp4:{video_id}")
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
            f"👋 ¡Hola a todos! Soy el bot descargador de <b>YouTube</b> y <b>YouTube Music</b>.\n\n"
            "✨ <b>¿Cómo usarme en este grupo?</b>\n"
            "• Compartan cualquier enlace de <b>YouTube</b> o <b>YouTube Music</b>.\n"
            "• Les permitiré elegir si desean descargarlo en <b>MP3</b> (Audio) o <b>MP4</b> (Video).\n"
            "• O usen directamente <code>/mp3 [enlace]</code> o <code>/mp4 [enlace]</code>.\n\n"
            "🧹 <i>Si me dan permisos de Administrador (Eliminar mensajes), borraré automáticamente los enlaces y dejaré solo el archivo limpio.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    welcome_text = (
        f"👋 ¡Hola, <b>{name}</b>!\n\n"
        "Soy tu bot especialista en descargar de <b>YouTube</b> y <b>YouTube Music</b>.\n\n"
        "✨ <b>Formatos disponibles:</b>\n"
        "• 🎵 <b>MP3:</b> Audio en alta calidad (192 kbps) con carátula y etiquetas.\n"
        "• 🎬 <b>MP4:</b> Video en alta resolución compatible con Telegram.\n\n"
        "📥 <b>¿Cómo usarlo?</b>\n"
        "Envíame cualquier enlace de YouTube o YouTube Music y elige el formato deseado.\n\n"
        "👥 <b>¡También funciono en grupos!</b> Añádeme con los botones de abajo."
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
        "Añade este bot a cualquier grupo para que todos puedan descargar música y videos de YouTube.\n\n"
        "⚡ <b>Ventajas en Grupos:</b>\n"
        "• <b>Selector MP3 / MP4:</b> Cada usuario elige si quiere audio o video con 1 clic.\n"
        "• <b>Limpieza de chat:</b> Elimina el mensaje del link una vez enviado el archivo.\n"
        "• <b>Mención al usuario:</b> Indica quién solicitó la descarga.\n"
        "• <b>Caché Compartida:</b> Si alguien pide una canción o video ya descargado, se envía en 0.5s.\n"
        "• <b>Anti-Spam:</b> No responde a charlas normales del grupo."
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
        "📖 <b>Guía de Uso de YouTube & YouTube Music:</b>\n\n"
        "1️⃣ Ve a YouTube o YouTube Music.\n"
        "2️⃣ Pulsa <i>Compartir</i> y luego <i>Copiar enlace</i>.\n"
        "3️⃣ Envíalo al chat privado o en cualquier grupo con el bot.\n"
        "4️⃣ Pulsa en <b>🎵 Descargar MP3</b> para audio o <b>🎬 Descargar MP4</b> para video.\n\n"
        "💡 <b>Comandos directos:</b>\n"
        "• <code>/mp3 [enlace]</code> - Descarga directa en audio MP3.\n"
        "• <code>/mp4 [enlace]</code> - Descarga directa en video MP4.\n"
        "• <code>/start</code> - Menú principal y bienvenida.\n"
        "• <code>/panel</code> - Panel para añadir a grupos.\n\n"
        "⚠️ <b>Límite de tamaño:</b> Telegram permite enviar archivos de hasta <b>50 MB</b>."
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
        "🤖 <b>Especialidad:</b> YouTube y YouTube Music (MP3 / MP4)\n"
        "⚡ <b>Motor:</b> yt-dlp + FFmpeg\n"
        "🗄️ <b>Base de Datos:</b> SQLite Cache para envíos instantáneos\n"
        "🧹 <b>Auto-Limpieza:</b> Borra links en grupos y elimina temporales del servidor."
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


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the group when added and explains features."""
    bot_id = context.bot.id
    for member in update.message.new_chat_members:
        if member.id == bot_id:
            await update.message.reply_html(
                "🎉 <b>¡Hola! Gracias por añadirme a este grupo.</b>\n\n"
                "📹 Descargaré de <b>YouTube</b> y <b>YouTube Music</b> en formato <b>MP3</b> (Audio) o <b>MP4</b> (Video).\n\n"
                "💡 <b>Para activar la auto-limpieza:</b>\n"
                "Hazme administrador con permiso de <b>'Eliminar mensajes'</b> para que pueda borrar los enlaces y dejar solo la música o videos limpios.\n\n"
                "¡Ya pueden empezar a compartir enlaces de YouTube!"
            )
            break


async def execute_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: Optional[int],
    user_mention: str,
    video_id: str,
    format_type: str,
    status_message=None,
    original_message=None,
    is_group: bool = False
):
    """Core download execution for both MP3 and MP4 with caching and cleanup."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    platform, emoji = detect_platform(url)
    format_type = format_type.lower().strip()
    is_audio = (format_type == 'mp3')
    clean_url = html.escape(url)

    # 1. Check cache for instant delivery
    cached_data = cache.get(video_id, format_type)
    if cached_data:
        try:
            cached_title = cached_data.get('title', 'YouTube Media')
            clean_title = html.escape(cached_title[:200] + ('...' if len(cached_title) > 200 else ''))
            duration = cached_data.get('duration')
            filesize = cached_data.get('filesize', 0)
            performer = cached_data.get('performer')
            clean_performer = html.escape(performer or '')

            if is_audio:
                caption = (
                    f"🎵 <b>{clean_title}</b>\n"
                    + (f"🎤 <b>Canal/Artista:</b> {clean_performer}\n" if clean_performer else "")
                    + f"{emoji} <b>Plataforma:</b> {cached_data.get('platform', platform)}\n"
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

            # Clean up status and original messages
            if status_message:
                try:
                    await status_message.delete()
                except Exception:
                    pass

            if is_group and original_message:
                try:
                    await original_message.delete()
                except Exception as del_err:
                    logger.debug(f"No se pudo eliminar mensaje en grupo: {del_err}")

            return
        except Exception as e:
            logger.info(f"Cached media failed, downloading fresh: {e}")

    # 2. Fresh download
    item_label = "audio MP3" if is_audio else "video MP4"
    if status_message:
        try:
            await status_message.edit_text(
                f"⏳ <b>Descargando {item_label} de {platform}...</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
    else:
        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ <b>Descargando {item_label} de {platform}...</b>",
            parse_mode=ParseMode.HTML
        )

    stop_chat_action = asyncio.Event()
    chat_action = ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO
    chat_action_task = asyncio.create_task(
        keep_chat_action(context, chat_id, chat_action, stop_event=stop_chat_action)
    )

    download_result = None
    try:
        async with download_semaphore:
            download_result = await downloader.download(url, format_type=format_type)

        try:
            await status_message.edit_text(
                f"⬆️ <b>Subiendo {item_label} a Telegram...</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        file_path = download_result['file_path']
        thumb_path = download_result.get('thumbnail_path')
        title = download_result.get('title', 'YouTube')
        duration = download_result.get('duration')
        width = download_result.get('width')
        height = download_result.get('height')
        filesize = download_result.get('filesize', 0)
        artist = download_result.get('artist')
        clean_title = html.escape(title[:200] + ('...' if len(title) > 200 else ''))
        clean_artist = html.escape(artist or '')

        if is_audio:
            caption = (
                f"🎵 <b>{clean_title}</b>\n"
                + (f"🎤 <b>Canal/Artista:</b> {clean_artist}\n" if clean_artist else "")
                + f"{emoji} <b>Plataforma:</b> {platform}\n"
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
                            video_id_or_url=video_id,
                            format_type="mp3",
                            file_id=sent_msg.audio.file_id,
                            title=title,
                            platform=platform,
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
                    if sent_msg and sent_msg.video:
                        cache.set(
                            video_id_or_url=video_id,
                            format_type="mp4",
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

        # Delete progress message
        try:
            await status_message.delete()
        except Exception:
            pass

        # In groups: Delete the original message containing the link!
        if is_group and original_message:
            try:
                await original_message.delete()
            except Exception as del_err:
                logger.info(f"No se pudo eliminar el link original en el grupo: {del_err}")

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
            "Verifica que el video sea público y esté disponible."
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


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline keyboard button callbacks."""
    query = update.callback_query
    data = query.data
    bot_username = await get_bot_username(context)

    # 1. Download format selection: dl:mp3:<id> or dl:mp4:<id>
    if data.startswith("dl:"):
        parts = data.split(":")
        if len(parts) == 3:
            _, format_type, video_id = parts
            await query.answer()

            user = update.effective_user
            user_mention = user.mention_html() if user else "Usuario"
            chat = update.effective_chat
            is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

            await execute_download(
                context=context,
                chat_id=chat.id,
                user_id=user.id if user else None,
                user_mention=user_mention,
                video_id=video_id,
                format_type=format_type,
                status_message=query.message,
                original_message=None,
                is_group=is_group,
            )
            return

    # 2. Menu navigation
    if data == "main_menu":
        user = update.effective_user
        name = html.escape(user.first_name) if user and user.first_name else "amigo"
        welcome_text = (
            f"👋 ¡Hola, <b>{name}</b>!\n\n"
            "Soy tu bot para descargar de <b>YouTube</b> y <b>YouTube Music</b>.\n\n"
            "✨ <b>Formatos disponibles:</b>\n"
            "• 🎵 <b>MP3:</b> Audio en alta calidad con etiquetas y portada.\n"
            "• 🎬 <b>MP4:</b> Video optimizado para Telegram.\n\n"
            "📥 Envíame cualquier enlace de YouTube o YouTube Music para comenzar."
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
            "Permite al bot borrar el enlace original enviado por el usuario, dejando solo la música o video limpios en el chat.\n\n"
            "2. 📤 <b>Enviar archivos multimedia (Audio y Video):</b>\n"
            "Necesario para subir archivos MP3 y MP4 al grupo.\n\n"
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
            "• <code>/mp3 [enlace]</code> - Descarga directa en audio MP3.\n"
            "• <code>/mp4 [enlace]</code> - Descarga directa en video MP4.\n"
            "• <code>/dl [enlace]</code> - Abre el selector interactivo MP3/MP4.\n"
            "• <b>Pega directa:</b> Si el bot es Admin o tiene privacidad desactivada, muestra el selector al pegar un link.\n"
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


async def direct_format_command(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_format: str):
    """Handler for direct /mp3 or /mp4 commands."""
    text = " ".join(context.args) if context.args else ""
    urls = URL_REGEX.findall(text)

    if not urls and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        urls = URL_REGEX.findall(reply_text)

    if not urls:
        await update.message.reply_html(
            f"ℹ️ <b>Uso del comando:</b>\n<code>/{forced_format} [enlace de YouTube]</code>\n"
            f"O responde a un mensaje que contenga un enlace de YouTube usando <code>/{forced_format}</code>."
        )
        return

    url = urls[0].strip()
    video_id = extract_youtube_id(url)
    if not video_id:
        await update.message.reply_html("❌ No se reconoció un ID válido de YouTube.")
        return

    user = update.effective_user
    user_mention = user.mention_html() if user else "Usuario"
    chat = update.effective_chat
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    await execute_download(
        context=context,
        chat_id=chat.id,
        user_id=user.id if user else None,
        user_mention=user_mention,
        video_id=video_id,
        format_type=forced_format,
        status_message=None,
        original_message=update.message,
        is_group=is_group,
    )


async def mp3_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await direct_format_command(update, context, "mp3")


async def mp4_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await direct_format_command(update, context, "mp4")


async def dl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /dl command."""
    text = " ".join(context.args) if context.args else ""
    urls = URL_REGEX.findall(text)

    if not urls and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        urls = URL_REGEX.findall(reply_text)

    if not urls:
        await update.message.reply_html(
            "ℹ️ <b>Uso del comando:</b>\n<code>/dl [enlace de YouTube]</code>"
        )
        return

    url = urls[0].strip()
    video_id = extract_youtube_id(url)
    if not video_id:
        await update.message.reply_html("❌ Por favor envía un enlace válido de YouTube o YouTube Music.")
        return

    platform, emoji = detect_platform(url)
    reply_markup = get_format_selection_keyboard(video_id)
    await update.message.reply_html(
        f"{emoji} <b>{platform} detectado</b>\n\n"
        "¿En qué formato deseas descargarlo?",
        reply_markup=reply_markup
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming messages from users and groups."""
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    urls = URL_REGEX.findall(text)
    is_group = update.effective_chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if not urls:
        # In groups, stay silent on normal chatter to avoid spamming members
        if not is_group:
            bot_username = await get_bot_username(context)
            await update.message.reply_html(
                "ℹ️ <b>Enlace no detectado.</b>\n\n"
                "Por favor, envíame un enlace de <b>YouTube</b> o <b>YouTube Music</b> "
                "para descargarlo en formato <b>MP3</b> o <b>MP4</b>.",
                reply_markup=get_start_keyboard(bot_username)
            )
        return

    url = urls[0].strip()
    video_id = extract_youtube_id(url)

    if not video_id:
        if not is_group:
            await update.message.reply_html(
                "❌ <b>Enlace no compatible:</b>\n"
                "Este bot está configurado para descargar de <b>YouTube</b> y <b>YouTube Music</b>.\n\n"
                "Formatos admitidos: <code>youtube.com</code>, <code>youtu.be</code>, <code>music.youtube.com</code>."
            )
        return

    platform, emoji = detect_platform(url)
    reply_markup = get_format_selection_keyboard(video_id)

    await update.message.reply_html(
        f"{emoji} <b>{platform} detectado:</b>\n\n"
        "¿En qué formato deseas descargarlo?",
        reply_markup=reply_markup
    )


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

    print("🚀 Iniciando Bot de YouTube y YouTube Music (MP3 / MP4)...")
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
    app.add_handler(CommandHandler(["mp3", "audio", "musica"], mp3_command))
    app.add_handler(CommandHandler(["mp4", "video"], mp4_command))
    app.add_handler(CommandHandler(["dl", "descargar", "bajar"], dl_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("✅ Bot listo y a la escucha. Presiona Ctrl+C para detenerlo.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
