import os
import re
import html
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatAction, ChatType, ParseMode, ChatMemberStatus
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
    is_spotify_url,
    normalize_instagram_url,
)
from cache import VideoCache
from db import (
    UserDatabase,
    NO_VIP_DAILY_LIMIT,
    VIP_DAILY_LIMIT,
    get_local_now,
    get_local_today_str,
)

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
ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()]

# Initialize services
downloader = VideoDownloader(cookies_file=COOKIES_FILE)
cache = VideoCache()
user_db = UserDatabase()

logger.info(
    f"Configuración de Cookies: archivo={downloader.cookies_file}, "
    f"INSTAGRAM_SESSIONID={'PRESENTE' if (os.getenv('INSTAGRAM_SESSIONID') or os.getenv('IG_SESSIONID')) else 'NO DEFINIDO'}"
)

# Concurrency limiter (4 parallel downloads)
download_semaphore = asyncio.Semaphore(4)

URL_REGEX = re.compile(r'(https?://[^\s]+)')


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy cloud health checks (Render / Koyeb)."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Downloader Bot con Sistema VIP activo.")

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
            logger.info(f"Servidor web de salud iniciado en el puerto {port}")
        except Exception as e:
            logger.warning(f"No se pudo iniciar el servidor web de salud: {e}")


def format_filesize(size_bytes: int) -> str:
    """Formats bytes to MB."""
    return f"{size_bytes / (1024 * 1024):.1f} MB"


async def schedule_midnight_quota_reset():
    """Background loop that resets all user daily quotas exactly at 12:00 AM (midnight) local time."""
    import datetime as dt
    while True:
        try:
            now = get_local_now()
            # Calculate next midnight (00:00:00) in local timezone
            tomorrow = now.date() + dt.timedelta(days=1)
            next_midnight = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=now.tzinfo)
            wait_seconds = (next_midnight - now).total_seconds()
            logger.info(
                f"⏰ Próximo reinicio de cuotas programado para las 12:00 AM hora local "
                f"({next_midnight.strftime('%Y-%m-%d %H:%M:%S %Z')}), esperando {wait_seconds:.1f}s."
            )
            await asyncio.sleep(max(1.0, wait_seconds))
            # Sleep 2 extra seconds to ensure date rollover is complete
            await asyncio.sleep(2.0)

            count = user_db.reset_all_daily_quotas()
            logger.info(f"✅ Reinicio automático de medianoche completado: {count} usuarios reiniciados.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en la tarea de reinicio a las 12:00 AM: {e}", exc_info=True)
            await asyncio.sleep(60)


async def post_init(application: Application) -> None:
    """Called after application initializes to fetch bot username and start background scheduler."""
    bot_info = await application.bot.get_me()
    application.bot_data['username'] = bot_info.username
    logger.info(f"Bot iniciado exitosamente como @{bot_info.username}")
    asyncio.create_task(schedule_midnight_quota_reset())


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Gets the bot username reliably."""
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


async def sync_user_vip_status(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    group_title: Optional[str] = None
) -> bool:
    """
    Checks member status/custom title in group.
    - If user is in ADMIN_IDS -> VIP
    - If user is Creator / Owner of the group -> VIP
    - If user custom_title contains 'VIP' (e.g. 'DROGUITA - VIP', 'MIYAGIPEOS - VIP') -> VIP
    - If user is Administrator with title 'ADMIN' or is Group Admin -> VIP
    - If regular member without VIP title -> NO VIP PASS
    """
    if chat_id < 0:
        user_db.register_group(chat_id, group_title)

    if user_id in ADMIN_IDS:
        user_db.set_vip_status(user_id, True, username, first_name)
        return True

    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = str(getattr(member, 'status', '')).lower()
        is_creator = status in ['creator', 'owner', ChatMemberStatus.OWNER]
        is_admin = status in ['administrator', ChatMemberStatus.ADMINISTRATOR]
        custom_title = (getattr(member, 'custom_title', '') or '').strip().upper()

        logger.info(f"Sync VIP chat={chat_id}, user={user_id}: status={status}, title='{custom_title}'")

        if is_creator or 'VIP' in custom_title or 'ADMIN' in custom_title or is_admin:
            user_db.set_vip_status(user_id, True, username, first_name)
            return True
        else:
            user_db.set_vip_status(user_id, False, username, first_name)
            return False
    except Exception as e:
        logger.warning(f"No se pudo verificar estado VIP de {user_id} en chat {chat_id}: {e}")
        user_db.get_or_create_user(user_id, username, first_name)
    return False


async def sync_user_vip_from_all_groups(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None
) -> bool:
    """Checks VIP status across all known groups when in private chat."""
    if user_id in ADMIN_IDS:
        user_db.set_vip_status(user_id, True, username, first_name)
        return True

    # If already marked VIP in DB, preserve it
    user = user_db.get_or_create_user(user_id, username, first_name)
    if user.get('is_vip'):
        return True

    known_groups = user_db.get_known_groups()
    for gid in known_groups:
        try:
            is_vip = await sync_user_vip_status(context, gid, user_id, username, first_name)
            if is_vip:
                return True
        except Exception:
            continue
    return False



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


def get_start_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Creates the main interactive keyboard."""
    add_group_admin_url = (
        f"https://t.me/{bot_username}?startgroup=botstart&admin=delete_messages"
        if bot_username else "https://t.me"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Añadir a un Grupo (Auto-Borrar Links)", url=add_group_admin_url),
        ],
        [
            InlineKeyboardButton("📊 Mis Estadísticas", callback_data="show_stats"),
            InlineKeyboardButton("👥 Panel de Grupos", callback_data="group_panel"),
        ],
        [
            InlineKeyboardButton("📖 Ayuda & Límites", callback_data="help_menu"),
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
    """Keyboard to choose between MP3 (audio) and MP4 (video) for YouTube."""
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
    chat = update.effective_chat
    name = html.escape(user.first_name) if user and user.first_name else "amigo"
    bot_username = await get_bot_username(context)

    user_db.get_or_create_user(user.id, user.username, user.first_name)

    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
        panel_url = f"https://t.me/{bot_username}?start=help" if bot_username else "https://t.me"
        keyboard = [[InlineKeyboardButton("⚙️ Ver Panel del Bot", url=panel_url)]]
        await update.message.reply_html(
            f"👋 ¡Hola a todos! Soy el bot descargador de videos y música.\n\n"
            "✨ <b>Condiciones del Grupo:</b>\n"
            f"• 🆓 <b>NO VIP PASS:</b> {NO_VIP_DAILY_LIMIT} descargas diarias (X, Instagram, TikTok).\n"
            f"• 👑 <b>VIP:</b> {VIP_DAILY_LIMIT} descargas diarias (Todas las plataformas: YouTube, Facebook, etc.).\n\n"
            "🧹 <i>Con permisos de Administrador (Eliminar mensajes), borro los enlaces automáticamente.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    welcome_text = (
        f"👋 ¡Hola, <b>{name}</b>!\n\n"
        "Soy tu bot para descargar videos y música con sistema de membresías <b>VIP</b>.\n\n"
        "✨ <b>Niveles de Membresía:</b>\n"
        f"• 🆓 <b>NO VIP PASS:</b> {NO_VIP_DAILY_LIMIT} descargas al día (Instagram, TikTok, X).\n"
        f"• 👑 <b>VIP:</b> {VIP_DAILY_LIMIT} descargas al día (Todas las plataformas: YouTube MP3/MP4, Spotify, Facebook, etc.).\n\n"
        "📊 Usa <code>/stats</code> para ver tu consumo diario y estado.\n\n"
        "📥 <b>¿Cómo usarlo?</b> Envíame cualquier enlace para comenzar."
    )

    await update.message.reply_html(
        welcome_text,
        reply_markup=get_start_keyboard(bot_username)
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /stats or /estadisticas command."""
    user = update.effective_user
    chat = update.effective_chat

    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
    else:
        await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    stats_text = user_db.get_stats_message(user.id, user.username, user.first_name)
    await update.message.reply_html(stats_text)


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /panel or /grupo command."""
    bot_username = await get_bot_username(context)
    panel_text = (
        "👥 <b>PANEL DE GESTIÓN PARA GRUPOS</b>\n\n"
        "Añade este bot a cualquier grupo para activar el sistema de descargas con soporte VIP.\n\n"
        "⚡ <b>Condiciones Oficiales:</b>\n"
        f"• 🆓 <b>NO VIP PASS:</b> Límite de {NO_VIP_DAILY_LIMIT} descargas diarias (X, Instagram, TikTok).\n"
        f"• 👑 <b>VIP:</b> Límite de {VIP_DAILY_LIMIT} descargas diarias (YouTube, Facebook, X, IG, TikTok).\n"
        "• <b>Detección automática:</b> Los miembros con título '- VIP' en el grupo son reconocidos automáticamente.\n"
        "• <b>Limpieza de chat:</b> Elimina el mensaje del link una vez enviado el archivo."
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
        "📖 <b>Guía de Uso & Límites del Bot:</b>\n\n"
        "1️⃣ Copia el enlace del video o audio que deseas.\n"
        "2️⃣ Envíalo al chat privado o en el grupo.\n\n"
        "📋 <b>Reglas de Acceso (Privado y Grupos):</b>\n"
        f"• <b>NO VIP PASS:</b> {NO_VIP_DAILY_LIMIT} descargas/día en <b>X, Instagram y TikTok</b>.\n"
        f"• <b>VIP:</b> {VIP_DAILY_LIMIT} descargas/día en <b>todas las plataformas</b> (YouTube MP3/MP4, Spotify, Facebook, etc.).\n\n"
        "💡 <b>Comandos disponibles:</b>\n"
        "• <code>/stats</code> - Muestra tus estadísticas y cuota diaria.\n"
        "• <code>/mp3 [enlace]</code> - Descarga directa en audio MP3.\n"
        "• <code>/mp4 [enlace]</code> - Descarga directa en video MP4.\n"
        "• <code>/panel</code> - Panel para añadir a grupos."
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
        "🤖 <b>Versión:</b> 3.0.0 (Sistema VIP & Estadísticas)\n"
        "⚡ <b>Motor:</b> yt-dlp + FFmpeg\n"
        "🗄️ <b>Base de Datos:</b> SQLite para Usuarios, Cuotas y Caché de medios\n"
        "🧹 <b>Auto-Limpieza:</b> Borra links en grupos y mantiene el chat limpio."
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


async def vip_management_command(update: Update, context: ContextTypes.DEFAULT_TYPE, make_vip: bool):
    """Allows group admins or bot admins to grant or revoke VIP status manually."""
    user = update.effective_user
    chat = update.effective_chat
    is_authorized = user.id in ADMIN_IDS

    if not is_authorized and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        await update.message.reply_html("❌ Solo los administradores pueden gestionar el estado VIP.")
        return

    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if not target_id:
        await update.message.reply_html(
            "ℹ️ <b>Uso:</b> Responde al mensaje de un usuario con <code>/vip</code> o usa <code>/vip [user_id]</code>."
        )
        return

    user_db.set_vip_status(target_id, make_vip)
    estado_str = "<b>VIP</b> 👑" if make_vip else "<b>NO VIP PASS</b> 🆓"
    await update.message.reply_html(f"✅ El usuario <code>{target_id}</code> ahora tiene estado: {estado_str}.")


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await vip_management_command(update, context, True)


async def unvip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await vip_management_command(update, context, False)


async def set_ig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to set or update Instagram session cookie directly via chat."""
    user = update.effective_user
    chat = update.effective_chat
    is_authorized = user.id in ADMIN_IDS

    if not is_authorized and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        await update.message.reply_html("❌ Solo los administradores pueden configurar la sesión de Instagram.")
        return

    # Delete command message if in group to keep cookie secret
    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            await update.message.delete()
        except Exception:
            pass

    if not context.args:
        await update.message.reply_html(
            "ℹ️ <b>Uso:</b> <code>/set_ig TU_SESSION_ID</code>\n\n"
            "Ejemplo: <code>/set_ig 11702076966%3AoJDj7KwxfUam8r...</code>\n\n"
            "<i>(En grupos, el bot borra automáticamente tu mensaje para proteger la clave).</i>"
        )
        return

    sid = context.args[0].strip().strip('"').strip("'").strip()
    if "sessionid=" in sid:
        sid = sid.split("sessionid=")[1].split(";")[0].strip()

    os.environ["INSTAGRAM_SESSIONID"] = sid
    from downloader import setup_cookies_file
    updated_path = setup_cookies_file(COOKIES_FILE)
    downloader.cookies_file = updated_path

    await update.message.reply_html(
        "✅ <b>Sesión de Instagram configurada y guardada con éxito.</b>\n\n"
        f"📁 Archivo: <code>{updated_path}</code>\n"
        "🔓 Ya puedes descargar cualquier Reel o publicación de Instagram con restricción."
    )


async def set_yt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to set YouTube cookies directly or get instructions to upload cookies.txt."""
    user = update.effective_user
    chat = update.effective_chat
    is_authorized = user.id in ADMIN_IDS

    if not is_authorized and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        await update.message.reply_html("❌ Solo los administradores pueden configurar cookies.")
        return

    # Delete command message if in group to keep data private
    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            await update.message.delete()
        except Exception:
            pass

    if not context.args:
        await update.message.reply_html(
            "🍪 <b>Configuración de Cookies para YouTube</b>\n\n"
            "Tienes 2 opciones sencillas:\n\n"
            "1️⃣ <b>Enviar el archivo (Recomendado):</b>\n"
            "Adjunta y envía tu archivo <code>cookies.txt</code> como documento a este chat. El bot lo importará automáticamente.\n\n"
            "2️⃣ <b>Por comando:</b>\n"
            "Usa <code>/set_yt [contenido_de_cookies]</code>\n\n"
            "<i>(En grupos, tus mensajes con cookies se eliminan automáticamente por seguridad).</i>"
        )
        return

    content = " ".join(context.args).strip()
    os.environ["YOUTUBE_COOKIES"] = content
    from downloader import setup_cookies_file
    updated_path = setup_cookies_file(COOKIES_FILE)
    downloader.cookies_file = updated_path

    await update.message.reply_html(
        "✅ <b>Cookies de YouTube configuradas con éxito.</b>\n\n"
        f"📁 Archivo: <code>{updated_path}</code>\n"
        "🔓 Ya puedes descargar contenido protegido de YouTube."
    )


async def cookies_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to send a cookies.txt file directly to update YouTube/Instagram cookies."""
    user = update.effective_user
    chat = update.effective_chat
    is_authorized = user.id in ADMIN_IDS

    if not is_authorized and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        return

    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not (fname.endswith('.txt') or 'cookie' in fname):
        return

    # Delete message in group for security
    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            await update.message.delete()
        except Exception:
            pass

    status_msg = await context.bot.send_message(
        chat_id=chat.id,
        text="⏳ <i>Procesando archivo de cookies...</i>",
        parse_mode="HTML"
    )

    try:
        tg_file = await doc.get_file()
        content_bytes = await tg_file.download_as_bytearray()
        content_text = content_bytes.decode('utf-8', errors='ignore')

        if not any(d in content_text for d in ['youtube.com', 'instagram.com', 'google.com', 'Netscape']):
            await status_msg.edit_text("❌ El archivo no parece ser un archivo de cookies válido de Netscape.")
            return

        from downloader import setup_cookies_file
        proj_dir = os.path.dirname(os.path.abspath(__file__))
        c_path = os.path.join(proj_dir, COOKIES_FILE)
        tmp_path = os.path.join(tempfile.gettempdir(), os.path.basename(COOKIES_FILE))

        for target in [c_path, tmp_path]:
            try:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content_text)
            except Exception:
                pass

        updated_path = setup_cookies_file(COOKIES_FILE)
        downloader.cookies_file = updated_path

        has_yt = 'youtube.com' in content_text
        has_ig = 'instagram.com' in content_text

        await status_msg.edit_text(
            "✅ <b>¡Archivo de cookies actualizado con éxito!</b>\n\n"
            f"▶️ <b>YouTube:</b> {'Activado ✅' if has_yt else 'No detectado ⚠️'}\n"
            f"📸 <b>Instagram:</b> {'Activado ✅' if has_ig else 'No detectado ⚠️'}\n"
            f"📁 Guardado en: <code>{updated_path}</code>\n\n"
            "Ya puedes descargar cualquier video protegido de YouTube e Instagram.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error procesando archivo de cookies: {e}")
        await status_msg.edit_text(f"❌ Error al procesar archivo de cookies: {e}")


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the group when added and explains features."""
    chat = update.effective_chat
    if chat and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        user_db.register_group(chat.id, chat.title)

    bot_id = context.bot.id
    for member in update.message.new_chat_members:
        if member.id == bot_id:
            await update.message.reply_html(
                "🎉 <b>¡Hola! Gracias por añadirme a este grupo.</b>\n\n"
                "📹 Descargaré videos y música automáticamente.\n\n"
                f"💎 <b>Sistema VIP:</b> Los miembros con título '- VIP' en el grupo tienen {VIP_DAILY_LIMIT} descargas diarias y acceso a YouTube/Facebook. "
                f"Los miembros estándar tienen {NO_VIP_DAILY_LIMIT} descargas diarias en Instagram, TikTok y X.\n\n"
                "💡 Hazme administrador con permiso de <b>'Eliminar mensajes'</b> para activar la auto-limpieza."
            )
            break


async def execute_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    user_mention: str,
    url: str,
    format_type: str = "mp4",
    status_message=None,
    original_message=None,
    is_group: bool = False
):
    """Core download execution with quota checking, caching, and stats recording."""
    platform, emoji = detect_platform(url)
    is_yt = is_youtube_url(url)
    is_spotify = is_spotify_url(url)
    if is_spotify:
        format_type = "mp3"
    format_type = format_type.lower().strip()
    is_audio = (format_type == 'mp3')
    if is_yt:
        yt_id = extract_youtube_id(url)
        if yt_id:
            url = f"https://www.youtube.com/watch?v={yt_id}"
    elif platform == "Instagram":
        url = normalize_instagram_url(url)
    clean_url = html.escape(url)

    # 1. Quota & Permission Verification (Enforced both in private and groups)
    allowed, reason, user_data = user_db.check_download_permission(user_id, platform)
    if not allowed:
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass

        if reason == "platform_restricted":
            deny_text = (
                "🔒 <b>Función Exclusiva VIP</b>\n\n"
                "Tu estado actual es <b>NO VIP PASS</b>.\n"
                "Plataformas permitidas para tu rango:\n"
                "• 🐦 <b>X (Twitter)</b>\n"
                "• 📸 <b>Instagram</b>\n"
                "• 🎵 <b>TikTok</b>\n\n"
                f"Para descargar de <b>{platform}</b>, solicita tu rango VIP a un administrador del grupo."
            )
            await context.bot.send_message(chat_id=chat_id, text=deny_text, parse_mode=ParseMode.HTML)
            return

        elif reason == "daily_limit_reached":
            max_daily = VIP_DAILY_LIMIT if user_data.get('is_vip') else NO_VIP_DAILY_LIMIT
            limit_text = (
                f"📉 <b>Límite diario alcanzado ({max_daily}/{max_daily})</b>\n\n"
                f"Has utilizado tus <b>{max_daily} descargas diarias</b> de hoy.\n"
                "Tu cuota se reiniciará automáticamente a la medianoche (00:00)."
            )
            await context.bot.send_message(chat_id=chat_id, text=limit_text, parse_mode=ParseMode.HTML)
            return

    # 2. Check cache for instant delivery
    cache_key = extract_youtube_id(url) if is_yt else url
    cached_data = cache.get(cache_key, format_type)
    if cached_data:
        try:
            cached_title = cached_data.get('title', 'Media')
            clean_title = html.escape(cached_title[:200] + ('...' if len(cached_title) > 200 else ''))
            duration = cached_data.get('duration')
            filesize = cached_data.get('filesize', 0)
            performer = cached_data.get('performer')
            clean_performer = html.escape(performer or '')

            if cached_data.get('media_type') == 'photo':
                caption = (
                    f"📸 <b>{clean_title}</b>\n\n"
                    f"{emoji} <b>Plataforma:</b> {cached_data.get('platform', platform)}\n"
                    f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                    f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                    f"🔗 <a href=\"{clean_url}\">Link original</a>\n"
                    f"⚡ <i>Descarga instantánea</i>"
                )
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=cached_data['file_id'],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            elif is_audio or cached_data.get('media_type') == 'audio':
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

            # Record stats
            user_db.record_download_success(user_id, platform)

            if status_message:
                try:
                    await status_message.delete()
                except Exception:
                    pass

            if is_group and original_message:
                try:
                    await original_message.delete()
                except Exception:
                    pass

            return
        except Exception as e:
            logger.info(f"Cached delivery failed, downloading fresh: {e}")

    # 3. Fresh download
    item_label = "audio MP3" if is_audio else "contenido"
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

        res_type = download_result.get('type')
        if not res_type:
            res_type = 'audio' if download_result.get('is_audio') else 'video'

        title = download_result.get('title', 'Media')
        duration = download_result.get('duration')
        width = download_result.get('width')
        height = download_result.get('height')
        filesize = download_result.get('filesize', 0)
        artist = download_result.get('artist')
        clean_title = html.escape(title[:200] + ('...' if len(title) > 200 else ''))
        clean_artist = html.escape(artist or '')

        if res_type == 'audio':
            file_path = download_result['file_path']
            thumb_path = download_result.get('thumbnail_path')
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
                            video_id_or_url=cache_key,
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
                            media_type='audio',
                        )
                finally:
                    if thumb_fp:
                        thumb_fp.close()

        elif res_type == 'photo':
            file_path = download_result['file_path']
            caption = (
                f"📸 <b>{clean_title}</b>\n\n"
                f"{emoji} <b>Plataforma:</b> {platform}\n"
                f"📦 <b>Tamaño:</b> {format_filesize(filesize)}\n"
                f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                f"🔗 <a href=\"{clean_url}\">Link original</a>"
            )
            with open(file_path, 'rb') as photo_fp:
                sent_msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_fp,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    read_timeout=300,
                    write_timeout=300,
                )
                if sent_msg and sent_msg.photo:
                    cache.set(
                        video_id_or_url=cache_key,
                        format_type=format_type,
                        file_id=sent_msg.photo[-1].file_id,
                        title=title,
                        platform=platform,
                        duration=None,
                        width=width,
                        height=height,
                        filesize=filesize,
                        is_audio=False,
                        media_type='photo',
                    )

        elif res_type == 'album':
            caption = (
                f"📸 <b>{clean_title}</b>\n\n"
                f"{emoji} <b>Plataforma:</b> {platform}\n"
                f"📦 <b>Tamaño total:</b> {format_filesize(filesize)}\n"
                f"👤 <b>Pedido por:</b> {user_mention}\n\n"
                f"🔗 <a href=\"{clean_url}\">Link original</a>"
            )
            open_files = []
            media_group = []
            media_items = download_result.get('media_items', [])
            for idx, item in enumerate(media_items[:10]):
                item_fp = open(item['file_path'], 'rb')
                open_files.append(item_fp)
                item_caption = caption if idx == 0 else None
                if item.get('type') == 'video':
                    t_p = item.get('thumbnail_path')
                    t_fp = open(t_p, 'rb') if (t_p and os.path.exists(t_p)) else None
                    if t_fp:
                        open_files.append(t_fp)
                    media_group.append(
                        InputMediaVideo(
                            media=item_fp,
                            thumbnail=t_fp,
                            caption=item_caption,
                            parse_mode=ParseMode.HTML if item_caption else None,
                            supports_streaming=True
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=item_fp,
                            caption=item_caption,
                            parse_mode=ParseMode.HTML if item_caption else None
                        )
                    )

            try:
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    read_timeout=300,
                    write_timeout=300,
                )
            finally:
                for fp in open_files:
                    try:
                        fp.close()
                    except Exception:
                        pass

        else:
            # Video
            file_path = download_result['file_path']
            thumb_path = download_result.get('thumbnail_path')
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
                            video_id_or_url=cache_key,
                            format_type="mp4",
                            file_id=sent_msg.video.file_id,
                            title=title,
                            platform=platform,
                            duration=duration,
                            width=width,
                            height=height,
                            filesize=filesize,
                            is_audio=False,
                            media_type='video',
                        )
                finally:
                    if thumb_fp:
                        thumb_fp.close()

        # Successfully downloaded & sent -> record stats
        user_db.record_download_success(user_id, platform)

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
    user = update.effective_user
    chat = update.effective_chat

    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
    else:
        await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    # 1. Download format selection: dl:mp3:<id> or dl:mp4:<id>
    if data.startswith("dl:"):
        parts = data.split(":")
        if len(parts) == 3:
            _, format_type, video_id = parts
            await query.answer()

            user_mention = user.mention_html() if user else "Usuario"
            is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
            yt_url = f"https://www.youtube.com/watch?v={video_id}"

            await execute_download(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                user_mention=user_mention,
                url=yt_url,
                format_type=format_type,
                status_message=query.message,
                original_message=None,
                is_group=is_group,
            )
            return

    # 2. Stats
    if data == "show_stats":
        stats_text = user_db.get_stats_message(user.id, user.username, user.first_name)
        await query.answer()
        keyboard = [[InlineKeyboardButton("◀️ Volver al Inicio", callback_data="main_menu")]]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    # 3. Menu navigation
    if data == "main_menu":
        name = html.escape(user.first_name) if user and user.first_name else "amigo"
        welcome_text = (
            f"👋 ¡Hola, <b>{name}</b>!\n\n"
            "Soy tu bot para descargar videos y música con sistema de membresías <b>VIP</b>.\n\n"
            "✨ <b>Niveles de Membresía:</b>\n"
            f"• 🆓 <b>NO VIP PASS:</b> {NO_VIP_DAILY_LIMIT} descargas/día (Instagram, TikTok, X).\n"
            f"• 👑 <b>VIP:</b> {VIP_DAILY_LIMIT} descargas/día (YouTube MP3/MP4, Spotify, Facebook, etc.).\n\n"
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
            "1. 🗑️ <b>Eliminar mensajes:</b> Permite al bot borrar el enlace original enviado por el usuario.\n"
            "2. 📤 <b>Enviar archivos multimedia:</b> Para enviar audios y videos.\n"
            "3. 👁️ <b>Leer mensajes:</b> Para detectar enlaces automáticamente."
        )
        keyboard = [[InlineKeyboardButton("◀️ Volver al Panel", callback_data="group_panel")]]
        await query.answer()
        await query.edit_message_text(perms_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    elif data == "group_commands":
        cmd_text = (
            "📖 <b>COMANDOS PARA GRUPOS</b>\n\n"
            "• <code>/stats</code> - Consulta tus estadísticas y cuota.\n"
            "• <code>/mp3 [enlace]</code> - Descarga directa en MP3.\n"
            "• <code>/mp4 [enlace]</code> - Descarga directa en MP4.\n"
            "• <code>/panel</code> - Panel para añadir a grupos."
        )
        keyboard = [[InlineKeyboardButton("◀️ Volver al Panel", callback_data="group_panel")]]
        await query.answer()
        await query.edit_message_text(cmd_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def direct_format_command(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_format: str):
    """Handler for /mp3 or /mp4 direct commands."""
    text = " ".join(context.args) if context.args else ""
    urls = URL_REGEX.findall(text)

    if not urls and update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        urls = URL_REGEX.findall(reply_text)

    if not urls:
        await update.message.reply_html(
            f"ℹ️ <b>Uso del comando:</b>\n<code>/{forced_format} [enlace de video o música]</code>"
        )
        return

    url = urls[0].strip()
    user = update.effective_user
    chat = update.effective_chat
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    if is_group:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
    else:
        await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    user_mention = user.mention_html() if user else "Usuario"

    await execute_download(
        context=context,
        chat_id=chat.id,
        user_id=user.id,
        user_mention=user_mention,
        url=url,
        format_type=forced_format,
        status_message=None,
        original_message=update.message,
        is_group=is_group,
    )


async def mp3_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await direct_format_command(update, context, "mp3")


async def mp4_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await direct_format_command(update, context, "mp4")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming messages from users and groups."""
    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

    # Sync VIP status in group or private
    if is_group:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
    else:
        await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    text = update.message.text or update.message.caption or ""
    urls = URL_REGEX.findall(text)

    if not urls:
        if not is_group:
            bot_username = await get_bot_username(context)
            await update.message.reply_html(
                "ℹ️ <b>Enlace no detectado.</b>\n\n"
                "Por favor, envíame un enlace de <b>Instagram, TikTok, X, YouTube o Facebook</b>.",
                reply_markup=get_start_keyboard(bot_username)
            )
        return

    url = urls[0].strip()
    platform, emoji = detect_platform(url)
    is_yt = is_youtube_url(url)
    is_spotify = is_spotify_url(url)
    user_mention = user.mention_html() if user else "Usuario"

    # If YouTube, offer MP3 vs MP4 selector
    if is_yt:
        video_id = extract_youtube_id(url)
        reply_markup = get_format_selection_keyboard(video_id)
        await update.message.reply_html(
            f"{emoji} <b>{platform} detectado:</b>\n\n"
            "¿En qué formato deseas descargarlo?",
            reply_markup=reply_markup
        )
        return

    # Spotify defaults to MP3 audio, others to MP4 video
    forced_format = "mp3" if is_spotify else "mp4"

    # For other platforms (Instagram, TikTok, X, Facebook, Spotify): start download directly
    await execute_download(
        context=context,
        chat_id=chat.id,
        user_id=user.id,
        user_mention=user_mention,
        url=url,
        format_type=forced_format,
        status_message=None,
        original_message=update.message,
        is_group=is_group,
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

    print("🚀 Iniciando Bot con Sistema VIP, Estadísticas (/stats) y Control de Cuotas...")
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
    app.add_handler(CommandHandler(["stats", "estadisticas", "perfil"], stats_command))
    app.add_handler(CommandHandler(["panel", "grupo", "grupos", "anadir", "agregar", "addgroup"], panel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("unvip", unvip_cmd))
    app.add_handler(CommandHandler(["set_ig", "setig"], set_ig_command))
    app.add_handler(CommandHandler(["set_yt", "setyt", "cookie", "cookies"], set_yt_command))
    app.add_handler(MessageHandler(filters.Document.ALL, cookies_document_handler))
    app.add_handler(CommandHandler(["mp3", "audio", "musica"], mp3_command))
    app.add_handler(CommandHandler(["mp4", "video"], mp4_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("✅ Bot listo y a la escucha. Presiona Ctrl+C para detenerlo.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
