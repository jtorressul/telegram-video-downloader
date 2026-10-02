import os
import re
import html
import asyncio
import logging
import threading
import time
import types
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Optional, Set, Tuple
from dotenv import load_dotenv

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatAction, ChatType, ParseMode, ChatMemberStatus
from telegram.error import Conflict, NetworkError, Forbidden, BadRequest
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    filters,
)

import config
from downloaders import (
    VideoDownloader,
    detect_platform,
    format_duration,
    extract_youtube_id,
    is_youtube_url,
    is_spotify_url,
    normalize_instagram_url,
)
from downloaders import health
from downloaders.errors import Cancelled
from downloaders.diag import run_diagnostics
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
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
ADMIN_IDS = config.ADMIN_IDS

# Initialize services
downloader = VideoDownloader()
cache = VideoCache()
user_db = UserDatabase()

# Concurrency limiter (4 parallel downloads; other updates keep flowing)
download_semaphore = asyncio.Semaphore(4)

URL_REGEX = re.compile(r'(https?://[^\s]+)')
MAX_LINKS_PER_MESSAGE = 5

# In-flight downloads per user, so /cancel and the ✖️ button can stop them
active_downloads: Dict[int, Set[asyncio.Task]] = {}


def canonical_url(url: str) -> Tuple[str, str]:
    """Returns (url, cache_key) with the same normalization the cache uses."""
    if is_youtube_url(url):
        yt_id = extract_youtube_id(url)
        if yt_id:
            return f"https://www.youtube.com/watch?v={yt_id}", yt_id
    elif detect_platform(url)[0] == "Instagram":
        url = normalize_instagram_url(url)
    return url, url


def unique_urls(text: str) -> list:
    seen, result = set(), []
    for url in URL_REGEX.findall(text or ""):
        key = canonical_url(url.strip())[0]
        if key not in seen:
            seen.add(key)
            result.append(url.strip())
    return result


def audio_button(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎵 Solo audio", callback_data=f"aud:{cache.url_ref(url)}")]])


def retry_button(url: str, format_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔄 Reintentar", callback_data=f"retry:{format_type}:{cache.url_ref(url)}")]])


CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancelar", callback_data="cancel")]])


def cancel_user_downloads(user_id: int) -> int:
    tasks = [t for t in active_downloads.get(user_id, ()) if not t.done()]
    for task in tasks:
        task.cancel()
    return len(tasks)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy cloud health checks (Render / Koyeb)."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Downloader Bot con Sistema VIP y Arquitectura Zero-Cookies activa.")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    """Starts the health check HTTP server on PORT environment variable if available."""
    port_str = os.getenv("PORT", "8080")
    try:
        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"✅ Servidor web de salud iniciado en 0.0.0.0:{port}")
    except Exception as e:
        logger.warning(f"No se pudo iniciar el servidor web de salud: {e}")


def format_filesize(size_bytes: int) -> str:
    """Formats bytes to MB."""
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# --- Shared texts: single source for the rules so /start, /help, /panel and groups never disagree ---
FREE_PLATFORMS_TEXT = "Instagram, TikTok, X y Facebook"
VIP_PLATFORMS_TEXT = "todas las plataformas, incluidos YouTube (MP3/MP4) y Spotify"


def rules_text() -> str:
    return (
        f"• 🆓 <b>NO VIP PASS:</b> {NO_VIP_DAILY_LIMIT} descargas/día en {FREE_PLATFORMS_TEXT}.\n"
        f"• 👑 <b>VIP:</b> {VIP_DAILY_LIMIT} descargas/día en {VIP_PLATFORMS_TEXT}.\n"
        "• ⚡ Los enlaces que ya se descargaron antes se envían al instante y <b>no gastan cuota</b>."
    )


# (command, description, help text) — also used to build Telegram's "/" menu
USER_COMMANDS = [
    ("start", "Inicio y reglas del bot", "<code>/start</code> - Inicio y reglas del bot."),
    ("help", "Guía de uso y comandos", "<code>/help</code> - Esta guía."),
    ("stats", "Tus estadísticas y cuota diaria",
     "<code>/stats</code> (<code>/perfil</code>) - Tus estadísticas y cuota diaria."),
    ("mp3", "Descargar en audio MP3",
     "<code>/mp3 [enlace]</code> (<code>/audio</code>, <code>/musica</code>) - Descarga en audio MP3. "
     "También funciona respondiendo a un mensaje con enlace."),
    ("mp4", "Descargar en video MP4",
     "<code>/mp4 [enlace]</code> (<code>/video</code>) - Descarga en video MP4."),
    ("historial", "Tus últimas descargas",
     "<code>/historial</code> - Tus últimas 10 descargas, con botones para recibirlas de nuevo."),
    ("cancel", "Cancelar tu descarga en curso",
     "<code>/cancel</code> (<code>/cancelar</code>) - Cancela tu descarga en curso."),
    ("panel", "Añadir el bot a un grupo", "<code>/panel</code> (<code>/grupo</code>) - Añadir el bot a un grupo."),
    ("about", "Acerca del bot", "<code>/about</code> - Información del bot."),
]
GROUP_ADMIN_COMMANDS = [
    ("vip", "Dar VIP temporal (responde al usuario)",
     "<code>/vip</code> - Responde a un usuario (o usa <code>/vip [id]</code>) para darle VIP temporal. "
     "El VIP permanente se da poniéndole el título <b>'- VIP'</b> en el grupo."),
    ("unvip", "Quitar VIP (responde al usuario)", "<code>/unvip</code> - Quita el VIP del mismo modo."),
]
BOT_ADMIN_COMMANDS = [
    ("diag", "Diagnóstico de red y estrategias", "<code>/diag</code> - Diagnóstico de red y estrategias."),
]


def commands_help(commands) -> str:
    return "\n".join(f"• {line}" for _, _, line in commands)


async def schedule_midnight_quota_reset():
    """Background loop that resets all user daily quotas exactly at 12:00 AM (midnight) local time."""
    import datetime as dt
    while True:
        try:
            now = get_local_now()
            tomorrow = now.date() + dt.timedelta(days=1)
            next_midnight = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=now.tzinfo)
            wait_seconds = (next_midnight - now).total_seconds()
            logger.info(
                f"⏰ Próximo reinicio de cuotas programado para las 12:00 AM hora local "
                f"({next_midnight.strftime('%Y-%m-%d %H:%M:%S %Z')}), esperando {wait_seconds:.1f}s."
            )
            await asyncio.sleep(max(1.0, wait_seconds))
            await asyncio.sleep(2.0)

            count = user_db.reset_all_daily_quotas()
            logger.info(f"✅ Reinicio automático de medianoche completado: {count} usuarios reiniciados.")
        except asyncio.CancelledError:
            logger.info("🛑 Tarea de reinicio de cuotas cancelada limpiamente.")
            break
        except Exception as e:
            logger.error(f"Error en la tarea de reinicio a las 12:00 AM: {e}", exc_info=True)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break


async def register_command_menus(application: Application) -> None:
    """Publishes the "/" command menu: users everywhere, VIP tools for group admins, /diag for bot admins."""
    def to_bot_commands(commands):
        return [BotCommand(name, desc) for name, desc, _ in commands]

    try:
        await application.bot.set_my_commands(to_bot_commands(USER_COMMANDS),
                                              scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(to_bot_commands(USER_COMMANDS),
                                              scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(to_bot_commands(USER_COMMANDS + GROUP_ADMIN_COMMANDS),
                                              scope=BotCommandScopeAllChatAdministrators())
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.set_my_commands(
                    to_bot_commands(USER_COMMANDS + GROUP_ADMIN_COMMANDS + BOT_ADMIN_COMMANDS),
                    scope=BotCommandScopeChat(chat_id=admin_id))
            except BadRequest as e:
                # Fails until the admin has opened a private chat with the bot
                logger.info(f"No se pudo fijar el menú de admin para {admin_id}: {e}")
    except Exception as e:
        logger.warning(f"No se pudo registrar el menú de comandos: {e}")


async def post_init(application: Application) -> None:
    """Called after application initializes to fetch bot username and start background scheduler."""
    bot_info = await application.bot.get_me()
    application.bot_data['username'] = bot_info.username
    logger.info(f"Bot iniciado exitosamente como @{bot_info.username}")
    await register_command_menus(application)
    task = asyncio.create_task(schedule_midnight_quota_reset())
    application.bot_data['quota_reset_task'] = task
    application.bot_data['vip_audit_task'] = asyncio.create_task(schedule_vip_audit(application))


async def post_shutdown(application: Application) -> None:
    """Cleanly cancel and await background tasks when stopping."""
    for key in ('quota_reset_task', 'vip_audit_task'):
        task = application.bot_data.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


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


# A VIP verified less than this ago is trusted without asking Telegram again
VIP_RECHECK_SECONDS = 6 * 3600


async def is_vip_in_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> Optional[bool]:
    """
    VIP rule for one group: creator, administrator, or custom title containing 'VIP' / 'ADMIN'.
    Returns None when Telegram can't answer (bot removed, network error): callers must not
    revoke VIP on an unknown result.
    """
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logger.info(f"No se pudo verificar VIP de {user_id} en {chat_id}: {e}")
        return None
    status = str(getattr(member, 'status', '')).lower()
    if status in ('creator', 'owner', ChatMemberStatus.OWNER, 'administrator', ChatMemberStatus.ADMINISTRATOR):
        return True
    custom_title = (getattr(member, 'custom_title', '') or '').strip().upper()
    # Members who left or were kicked keep no title, so they fall through to False
    return 'VIP' in custom_title or 'ADMIN' in custom_title


async def resolve_vip(context: ContextTypes.DEFAULT_TYPE, user_id: int, group_ids) -> Optional[bool]:
    """VIP if VIP in ANY group. None if no group said yes and at least one couldn't be checked."""
    unknown = False
    for gid in group_ids:
        result = await is_vip_in_group(context, gid, user_id)
        if result:
            return True
        if result is None:
            unknown = True
    return None if unknown else False


def _vip_check_is_fresh(user: dict) -> bool:
    return time.time() - (user.get('vip_checked_at') or 0) < VIP_RECHECK_SECONDS


async def sync_user_vip_status(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    group_title: Optional[str] = None
) -> bool:
    """Updates VIP when the user writes in a group. Being VIP in another known group also counts."""
    if chat_id < 0:
        user_db.register_group(chat_id, group_title)

    if user_id in ADMIN_IDS:
        user_db.set_vip_status(user_id, True, username, first_name)
        return True

    user = user_db.get_or_create_user(user_id, username, first_name)
    here = await is_vip_in_group(context, chat_id, user_id)
    if here:
        user_db.set_vip_status(user_id, True, username, first_name)
        return True
    if here is None:
        return bool(user.get('is_vip'))

    # Not VIP in this group: a recent VIP verification may come from another group
    if user.get('is_vip'):
        if _vip_check_is_fresh(user):
            return True
        others = [g for g in user_db.get_known_groups() if g != chat_id]
        elsewhere = await resolve_vip(context, user_id, others)
        if elsewhere is None:
            return True
        user_db.set_vip_status(user_id, elsewhere, username, first_name)
        return elsewhere

    user_db.set_vip_status(user_id, False, username, first_name)
    return False


async def sync_user_vip_from_all_groups(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None
) -> bool:
    """Private chat: re-verifies across known groups unless the last check is recent."""
    if user_id in ADMIN_IDS:
        user_db.set_vip_status(user_id, True, username, first_name)
        return True

    user = user_db.get_or_create_user(user_id, username, first_name)
    was_vip = bool(user.get('is_vip'))
    if was_vip and _vip_check_is_fresh(user):
        return True

    is_vip = await resolve_vip(context, user_id, user_db.get_known_groups())
    if is_vip is None:
        return was_vip
    if is_vip or was_vip:
        user_db.set_vip_status(user_id, is_vip, username, first_name)
    return is_vip


async def audit_vip_users(application: Application) -> int:
    """Re-verifies every VIP against the groups and revokes those without a VIP title anywhere."""
    revoked = 0
    groups = user_db.get_known_groups()
    ctx = types.SimpleNamespace(bot=application.bot)
    for user_id in user_db.get_vip_user_ids():
        if user_id in ADMIN_IDS:
            continue
        is_vip = await resolve_vip(ctx, user_id, groups)
        await asyncio.sleep(0.2)  # stay far below Telegram's API limits
        if is_vip is None:
            continue
        user_db.set_vip_status(user_id, is_vip)
        if not is_vip:
            revoked += 1
            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=("ℹ️ Tu estado <b>VIP</b> ha terminado porque ya no tienes el título "
                          "'- VIP' en ningún grupo del bot. Ahora tienes <b>NO VIP PASS</b>.\n"
                          f"Pide a un administrador que te lo vuelva a asignar para recuperar "
                          f"{VIP_PLATFORMS_TEXT}."),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass  # user never opened a private chat with the bot or blocked it
    return revoked


async def schedule_vip_audit(application: Application):
    """Runs the VIP audit once a day (first run shortly after start)."""
    await asyncio.sleep(300)
    while True:
        try:
            revoked = await audit_vip_users(application)
            logger.info(f"✅ Auditoría VIP diaria completada: {revoked} VIP retirados.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error en la auditoría VIP: {e}", exc_info=True)
        try:
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break


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
    bot_username = await get_bot_username(context)

    user_db.get_or_create_user(user.id, user.username, user.first_name)

    if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await sync_user_vip_status(context, chat.id, user.id, user.username, user.first_name, chat.title)
        panel_url = f"https://t.me/{bot_username}?start=help" if bot_username else "https://t.me"
        keyboard = [[InlineKeyboardButton("⚙️ Ver Panel del Bot", url=panel_url)]]
        await update.message.reply_html(
            f"👋 ¡Hola a todos! Soy el bot descargador de videos y música.\n\n"
            "✨ <b>Condiciones del Grupo:</b>\n"
            f"{rules_text()}\n\n"
            "🧹 <i>Con permisos de Administrador (Eliminar mensajes), borro los enlaces automáticamente.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    await sync_user_vip_from_all_groups(context, user.id, user.username, user.first_name)

    # Deep link from inline mode: /start dl_<ref> downloads that link here
    arg = context.args[0] if context.args else ""
    if arg.startswith("dl_") and arg[3:].isdigit():
        url = cache.url_from_ref(int(arg[3:]))
        if url:
            await download_links(update, context, [url])
            return

    await update.message.reply_html(
        welcome_text(user),
        reply_markup=get_start_keyboard(bot_username)
    )


def welcome_text(user) -> str:
    name = html.escape(user.first_name) if user and user.first_name else "amigo"
    return (
        f"👋 ¡Hola, <b>{name}</b>!\n\n"
        "Soy tu bot para descargar videos, fotos y música con sistema de membresías <b>VIP</b>.\n\n"
        "✨ <b>Niveles de Membresía:</b>\n"
        f"{rules_text()}\n\n"
        "📊 Usa <code>/stats</code> para ver tu consumo diario y estado.\n\n"
        "📥 <b>¿Cómo usarlo?</b> Envíame cualquier enlace para comenzar."
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


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: shows egress routes, blocked platforms and per-strategy health."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    msg = await update.message.reply_text("🔎 Ejecutando diagnóstico de red...")
    report = await asyncio.to_thread(run_diagnostics)
    stats_lines = [
        f"{name}: ✅{st['ok']} ❌{st['fail']}" + (f" ⏸{st['paused_s']}s" if st['paused_s'] else "")
        for name, st in health.snapshot().items()
    ]
    text = report + ("\n\n── Estrategias\n" + "\n".join(stats_lines) if stats_lines else "")
    await msg.edit_text(f"<pre>{html.escape(text[:3900])}</pre>", parse_mode=ParseMode.HTML)


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /panel or /grupo command."""
    bot_username = await get_bot_username(context)
    panel_text = (
        "👥 <b>PANEL DE GESTIÓN PARA GRUPOS</b>\n\n"
        "Añade este bot a cualquier grupo para activar el sistema de descargas con soporte VIP.\n\n"
        "⚡ <b>Condiciones Oficiales:</b>\n"
        f"{rules_text()}\n"
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
    bot_username = await get_bot_username(context)
    inline_example = f"@{bot_username} enlace" if bot_username else "@bot enlace"
    help_text = (
        "📖 <b>Guía de Uso & Límites del Bot:</b>\n\n"
        "1️⃣ Copia el enlace del video, foto o audio que deseas.\n"
        f"2️⃣ Envíalo al chat privado o en el grupo (hasta {MAX_LINKS_PER_MESSAGE} enlaces por mensaje).\n"
        "3️⃣ En YouTube elegirás entre MP3 y MP4; en otros videos tienes el botón 🎵 <b>Solo audio</b>.\n\n"
        f"🔎 <b>Modo inline:</b> escribe <code>{inline_example}</code> en cualquier chat para "
        "compartir al instante lo que ya se descargó antes.\n\n"
        "📋 <b>Reglas de Acceso (Privado y Grupos):</b>\n"
        f"{rules_text()}\n\n"
        "💡 <b>Comandos:</b>\n"
        f"{commands_help(USER_COMMANDS)}\n\n"
        "👥 <b>En grupos:</b> el bot detecta los enlaces solo y, si es administrador, "
        "borra el mensaje con el enlace una vez enviado el archivo.\n"
        "👮 <b>Admins del grupo:</b>\n"
        f"{commands_help(GROUP_ADMIN_COMMANDS)}"
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
        "🤖 <b>Versión:</b> 4.1.0 (Zero-Cookies)\n"
        "🔒 <b>Privacidad:</b> descarga solo contenido público, sin cuentas ni cookies propias.\n"
        "⚡ <b>Motor:</b> yt-dlp + TikWM + FxTwitter + Polaris GraphQL + igexport + Cobalt\n"
        "🌐 <b>Red:</b> varias rutas de salida (directa, IPv6, Cloudflare WARP) con reintento automático\n"
        "🗄️ <b>Base de Datos:</b> SQLite para usuarios, cuotas y caché instantánea\n"
        "🧹 <b>Auto-Limpieza:</b> borra los enlaces en grupos y mantiene el chat ordenado."
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
    note = ("\n\nℹ️ El VIP se verifica con el título del grupo: si no tiene el título <b>'- VIP'</b>, "
            "lo perderá en la próxima verificación (unas horas).") if make_vip else ""
    await update.message.reply_html(
        f"✅ El usuario <code>{target_id}</code> ahora tiene estado: {estado_str}.{note}")


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await vip_management_command(update, context, True)


async def unvip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await vip_management_command(update, context, False)


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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the user's in-flight downloads."""
    count = cancel_user_downloads(update.effective_user.id)
    if count:
        await update.message.reply_html(f"🛑 Cancelando {count} descarga{'s' if count > 1 else ''}...")
    else:
        await update.message.reply_html("ℹ️ No tienes descargas en curso.")


HISTORY_SHOWN = 10
FORMAT_ICONS = {"mp3": "🎵", "mp4": "🎬"}


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists the user's last downloads with buttons to get them again (instant from cache)."""
    user = update.effective_user
    items = user_db.get_history(user.id, HISTORY_SHOWN)
    if not items:
        await update.message.reply_html("📭 Aún no tienes descargas. Envíame un enlace para empezar.")
        return

    lines = ["🕘 <b>Tus últimas descargas</b>\n"]
    buttons = []
    for idx, item in enumerate(items, 1):
        title = html.escape((item['title'] or "Sin título")[:60])
        icon = FORMAT_ICONS.get(item['format'], "📥")
        lines.append(f"{idx}. {icon} {title} — <i>{html.escape(item['platform'] or '')}</i>")
        buttons.append(InlineKeyboardButton(
            str(idx), callback_data=f"hist:{item['format']}:{cache.url_ref(item['url'])}"))
    lines.append("\nPulsa un número para recibirlo de nuevo (no gasta cuota si sigue en caché).")
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    await update.message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard),
                                    disable_web_page_preview=True)


def _inline_caption(cached: dict, url: str, emoji: str, bot_username: str) -> str:
    title = html.escape((cached.get('title') or 'Media')[:200])
    via = f"\n🤖 vía @{bot_username}" if bot_username else ""
    return f"{emoji} <b>{title}</b>\n🔗 <a href=\"{html.escape(url)}\">Link original</a>{via}"


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """@bot <link>: sends cached media instantly; otherwise offers to download it in private."""
    query = update.inline_query
    urls = unique_urls(query.query)
    if not urls:
        await query.answer([], cache_time=60, button=InlineQueryResultsButton(
            text="📥 Pega un enlace después de mi nombre", start_parameter="inline"))
        return

    url, cache_key = canonical_url(urls[0])
    platform, emoji = detect_platform(url)
    allowed, reason, _ = user_db.check_download_permission(query.from_user.id, platform)
    if not allowed and reason == "platform_restricted":
        await query.answer([], cache_time=10, is_personal=True, button=InlineQueryResultsButton(
            text=f"🔒 {platform} es solo para VIP", start_parameter="vip"))
        return

    bot_username = await get_bot_username(context)
    results = []
    for fmt in ("mp4", "mp3"):
        cached = cache.get(cache_key, fmt)
        if not cached:
            continue
        caption = _inline_caption(cached, url, emoji, bot_username)
        result_id = f"{fmt}-{cache.url_ref(url)}"
        title = (cached.get('title') or platform)[:60]
        if cached.get('media_type') == 'photo':
            results.append(InlineQueryResultCachedPhoto(
                id=result_id, photo_file_id=cached['file_id'], title=title,
                caption=caption, parse_mode=ParseMode.HTML))
        elif cached.get('media_type') == 'audio' or fmt == 'mp3':
            results.append(InlineQueryResultCachedAudio(
                id=result_id, audio_file_id=cached['file_id'], caption=caption, parse_mode=ParseMode.HTML))
        else:
            results.append(InlineQueryResultCachedVideo(
                id=result_id, video_file_id=cached['file_id'], title=f"🎬 {title}",
                caption=caption, parse_mode=ParseMode.HTML))

    # Uncached links can't be downloaded within the inline answer timeout: hand off to the private chat
    button_text = "📥 Otro formato en el bot" if results else f"📥 Descargar de {platform} en el bot"
    await query.answer(results, cache_time=10, is_personal=True, button=InlineQueryResultsButton(
        text=button_text, start_parameter=f"dl_{cache.url_ref(url)}"))


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the group when added and registers it."""
    chat = update.effective_chat
    if chat and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        user_db.register_group(chat.id, chat.title)

    bot_id = context.bot.id
    for member in update.message.new_chat_members:
        if member.id == bot_id:
            await update.message.reply_html(
                "🎉 <b>¡Hola! Gracias por añadirme a este grupo.</b>\n\n"
                "📹 Descargaré videos y música automáticamente.\n\n"
                "💎 <b>Sistema VIP</b> (los miembros con título '- VIP' en el grupo son VIP):\n"
                f"{rules_text()}\n\n"
                "💡 Hazme administrador con permiso de <b>'Eliminar mensajes'</b> para activar la auto-limpieza."
            )
            break


LOW_QUOTA_THRESHOLD = 2


def daily_limit_text(user_data) -> str:
    max_daily = VIP_DAILY_LIMIT if user_data.get('is_vip') else NO_VIP_DAILY_LIMIT
    return (
        f"📉 <b>Límite diario alcanzado ({max_daily}/{max_daily})</b>\n\n"
        f"Has utilizado tus <b>{max_daily} descargas diarias</b> de hoy.\n"
        "Tu cuota se reiniciará automáticamente a la medianoche (00:00).\n"
        "⚡ Los enlaces que ya se descargaron antes siguen funcionando."
    )


async def notify_low_quota(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, user_mention: str):
    """Warns the user when only a few downloads are left for today."""
    used, max_daily = user_db.get_quota(user_id)
    remaining = max_daily - used
    if remaining > LOW_QUOTA_THRESHOLD:
        return
    if remaining <= 0:
        text = (f"📉 {user_mention}, has usado tus <b>{max_daily}</b> descargas de hoy. "
                "Se reinician a medianoche (00:00). Los enlaces ya descargados antes siguen funcionando.")
    else:
        plural = "descarga" if remaining == 1 else "descargas"
        text = f"⚠️ {user_mention}, te quedan <b>{remaining}</b> {plural} hoy ({used}/{max_daily})."
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.info(f"No se pudo enviar el aviso de cuota: {e}")


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
    """Core download execution with quota checking, caching, and stats recording.

    Returns False when the user was denied (VIP-only platform or daily limit), True otherwise.
    """
    platform, emoji = detect_platform(url)
    is_spotify = is_spotify_url(url)
    if is_spotify:
        format_type = "mp3"
    format_type = format_type.lower().strip()
    is_audio = (format_type == 'mp3')
    url, cache_key = canonical_url(url)
    clean_url = html.escape(url)
    # Offer "audio only" under videos, except where audio is the only format anyway
    video_markup = None if is_spotify else audio_button(url)

    # 1. Quota & Permission Verification (cached links don't spend quota, so the daily limit doesn't block them)
    cached_data = cache.get(cache_key, format_type)
    allowed, reason, user_data = user_db.check_download_permission(user_id, platform)
    if not allowed and not (reason == "daily_limit_reached" and cached_data):
        if status_message:
            try:
                await status_message.delete()
            except Exception:
                pass

        if reason == "platform_restricted":
            deny_text = (
                "🔒 <b>Función Exclusiva VIP</b>\n\n"
                "Tu estado actual es <b>NO VIP PASS</b>.\n"
                f"Plataformas permitidas para tu rango: <b>{FREE_PLATFORMS_TEXT}</b>.\n\n"
                f"Para descargar de <b>{platform}</b>, solicita tu rango VIP a un administrador del grupo."
            )
            await context.bot.send_message(chat_id=chat_id, text=deny_text, parse_mode=ParseMode.HTML)
            return False

        elif reason == "daily_limit_reached":
            await context.bot.send_message(chat_id=chat_id, text=daily_limit_text(user_data),
                                           parse_mode=ParseMode.HTML)
            return False

    # 2. Cache hit: instant delivery (<0.5s)
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
                    reply_markup=video_markup,
                )

            user_db.record_download_success(user_id, platform, count_quota=False)
            user_db.add_history(user_id, url, format_type, platform, cached_title)

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

            return True
        except Exception as e:
            logger.info(f"Cached delivery failed, downloading fresh: {e}")
            if not allowed:
                # Only the cache hit let this user past the daily limit
                await context.bot.send_message(chat_id=chat_id, text=daily_limit_text(user_data),
                                               parse_mode=ParseMode.HTML)
                return False

    # 3. Fresh download
    item_label = "audio MP3" if is_audio else "contenido"
    if status_message:
        try:
            await status_message.edit_text(
                f"⏳ <b>Descargando {item_label} de {platform}...</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=CANCEL_MARKUP,
            )
        except Exception:
            pass
    else:
        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ <b>Descargando {item_label} de {platform}...</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=CANCEL_MARKUP,
        )

    stop_chat_action = asyncio.Event()
    chat_action = ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO
    chat_action_task = asyncio.create_task(
        keep_chat_action(context, chat_id, chat_action, stop_event=stop_chat_action)
    )

    async def run_download():
        async with download_semaphore:
            return await downloader.download(url, format_type=format_type)

    download_result = None
    download_task = asyncio.create_task(run_download())
    active_downloads.setdefault(user_id, set()).add(download_task)
    try:
        try:
            download_result = await download_task
        finally:
            active_downloads.get(user_id, set()).discard(download_task)

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

        elif res_type in ['album', 'carousel']:
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
            for idx, item in enumerate(media_items):
                item_fp = open(item['file_path'], 'rb')
                open_files.append(item_fp)
                item_caption = caption if idx == 0 else None
                if item.get('type') == 'audio':
                    t_p = item.get('thumbnail_path')
                    t_fp = open(t_p, 'rb') if (t_p and os.path.exists(t_p)) else None
                    if t_fp:
                        open_files.append(t_fp)
                    media_group.append(
                        InputMediaAudio(
                            media=item_fp,
                            thumbnail=t_fp,
                            caption=item_caption,
                            parse_mode=ParseMode.HTML if item_caption else None,
                            title=item.get('title'),
                            performer=item.get('artist'),
                            duration=item.get('duration'),
                        )
                    )
                elif item.get('type') == 'video':
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
                # Telegram allows at most 10 items per media group
                for start in range(0, len(media_group), 10):
                    await context.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group[start:start + 10],
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
                        reply_markup=video_markup,
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

        user_db.record_download_success(user_id, platform)
        user_db.add_history(user_id, url, format_type, platform, title)
        await notify_low_quota(context, chat_id, user_id, user_mention)

        try:
            await status_message.delete()
        except Exception:
            pass

        if is_group and original_message:
            try:
                await original_message.delete()
            except Exception as del_err:
                logger.info(f"No se pudo eliminar el link original en el grupo: {del_err}")

    except asyncio.CancelledError:
        if not download_task.cancelled() or asyncio.current_task().cancelling():
            raise  # the handler itself is being cancelled (shutdown), not a user cancel
        try:
            await status_message.edit_text(Cancelled.user_message)
        except Exception:
            pass

    except ValueError as val_err:
        logger.warning(f"Download error for {url}: {val_err} | {getattr(val_err, 'detail', '')[:300]}")
        # Content errors (private, deleted, too large...) won't change by retrying
        markup = None if getattr(val_err, 'content_error', False) else retry_button(url, format_type)
        try:
            await status_message.edit_text(
                f"❌ <b>No se pudo descargar:</b>\n\n{html.escape(str(val_err))}",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
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
            await status_message.edit_text(err_msg, parse_mode=ParseMode.HTML,
                                           reply_markup=retry_button(url, format_type))
        except Exception:
            pass

    finally:
        stop_chat_action.set()
        await chat_action_task
        if download_result:
            downloader.cleanup(download_result)
    return True


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

    # 2. Cancel button on the "Descargando..." message (only cancels the presser's own downloads)
    if data == "cancel":
        count = cancel_user_downloads(user.id)
        await query.answer("🛑 Cancelando..." if count else "No tienes descargas en curso.", show_alert=not count)
        return

    # 3. Buttons that act on a stored link: aud:<ref>, retry:<fmt>:<ref>, hist:<fmt>:<ref>
    if data.startswith(("aud:", "retry:", "hist:")):
        parts = data.split(":")
        action = parts[0]
        format_type, ref = ("mp3", parts[1]) if action == "aud" else (parts[1], parts[-1])
        url = cache.url_from_ref(int(ref)) if ref.isdigit() else None
        if not url or format_type not in ("mp3", "mp4"):
            await query.answer("Este botón ya no es válido.", show_alert=True)
            return
        await query.answer()
        await execute_download(
            context=context,
            chat_id=chat.id,
            user_id=user.id,
            user_mention=user.mention_html(),
            url=url,
            format_type=format_type,
            # Retry reuses the error message as its status message
            status_message=query.message if action == "retry" else None,
            original_message=None,
            is_group=chat.type in [ChatType.GROUP, ChatType.SUPERGROUP],
        )
        return

    # 4. Stats
    if data == "show_stats":
        stats_text = user_db.get_stats_message(user.id, user.username, user.first_name)
        await query.answer()
        keyboard = [[InlineKeyboardButton("◀️ Volver al Inicio", callback_data="main_menu")]]
        await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return

    # 5. Menu navigation
    if data == "main_menu":
        await query.answer()
        await query.edit_message_text(
            welcome_text(user),
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
            f"{commands_help(USER_COMMANDS)}\n\n"
            "👮 <b>Solo administradores del grupo:</b>\n"
            f"{commands_help(GROUP_ADMIN_COMMANDS)}"
        )
        keyboard = [[InlineKeyboardButton("◀️ Volver al Panel", callback_data="group_panel")]]
        await query.answer()
        await query.edit_message_text(cmd_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming messages to detect media links."""
    chat = update.effective_chat
    user = update.effective_user
    if not update.message:
        return

    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]

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
                "Por favor, envíame un enlace de <b>Instagram, TikTok, X, YouTube, Spotify o Facebook</b>.",
                reply_markup=get_start_keyboard(bot_username)
            )
        return

    await download_links(update, context, unique_urls(text))


async def download_links(update: Update, context: ContextTypes.DEFAULT_TYPE, urls: list):
    """Processes up to MAX_LINKS_PER_MESSAGE links from one message, in order."""
    chat = update.effective_chat
    user = update.effective_user
    is_group = chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]
    user_mention = user.mention_html() if user else "Usuario"

    if len(urls) > MAX_LINKS_PER_MESSAGE:
        await update.message.reply_html(
            f"ℹ️ Proceso como máximo <b>{MAX_LINKS_PER_MESSAGE}</b> enlaces por mensaje; "
            "envía el resto en otro mensaje.")
        urls = urls[:MAX_LINKS_PER_MESSAGE]

    # In groups the link message is deleted after the last download; not when a YouTube
    # format picker still needs it as context
    has_youtube = any(is_youtube_url(u) for u in urls)
    last_direct = next((u for u in reversed(urls) if not is_youtube_url(u)), None)

    for url in urls:
        if is_youtube_url(url):
            platform, emoji = detect_platform(url)
            await update.message.reply_html(
                f"{emoji} <b>{platform} detectado:</b>\n\n"
                "¿En qué formato deseas descargarlo?",
                reply_markup=get_format_selection_keyboard(extract_youtube_id(url))
            )
            continue

        delivered = await execute_download(
            context=context,
            chat_id=chat.id,
            user_id=user.id,
            user_mention=user_mention,
            url=url,
            format_type="mp3" if is_spotify_url(url) else "mp4",
            status_message=None,
            original_message=update.message if (url == last_direct and not has_youtube) else None,
            is_group=is_group,
        )
        if delivered is False:
            break  # denied (limit / VIP): the user already got the explanation once


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manejo global de excepciones para el bot de Telegram."""
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "⚠️ [Conflict] Telegram terminó esta sesión de getUpdates porque otra instancia del bot se inició con este token. "
            "Esto ocurre normalmente durante un nuevo despliegue o reinicio en Render mientras el contenedor anterior se apaga. "
            "Esta instancia cederá el paso y se detendrá de forma ordenada."
        )
        return
    elif isinstance(err, (NetworkError, asyncio.TimeoutError)):
        logger.warning(f"⚠️ Error temporal de red con Telegram: {err}")
        return
    elif isinstance(err, Forbidden):
        logger.warning(f"⚠️ Telegram Forbidden: el bot fue bloqueado o carece de permisos: {err}")
        return
    elif isinstance(err, BadRequest):
        logger.warning(f"⚠️ Telegram BadRequest: {err}")
        return

    logger.error(f"❌ Error no controlado procesando actualización: {err}", exc_info=err)


def main():
    """Main entrypoint for the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "TU_TOKEN_AQUI":
        print("\n" + "=" * 60)
        print("❌ ERROR: TELEGRAM_BOT_TOKEN no está configurado.")
        print("1. Abre el archivo .env en esta carpeta.")
        print("2. Añade tu token obtenido de @BotFather:")
        print("   TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ")
        print("=" * 60 + "\n")
        return

    print("🚀 Iniciando Bot con Sistema VIP, Estadísticas (/stats) y Arquitectura Zero-Cookies...")
    start_health_server()

    request = HTTPXRequest(
        read_timeout=300,
        write_timeout=300,
        connect_timeout=60,
        pool_timeout=60
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        # Without this, updates run one at a time: one slow download blocked every other user
        # (and /cancel). Parallel downloads stay capped by download_semaphore.
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Global error handler
    app.add_error_handler(global_error_handler)

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler(["stats", "estadisticas", "perfil"], stats_command))
    app.add_handler(CommandHandler(["panel", "grupo", "grupos", "anadir", "agregar", "addgroup"], panel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("diag", diag_command))
    app.add_handler(CommandHandler("unvip", unvip_cmd))
    app.add_handler(CommandHandler(["mp3", "audio", "musica"], mp3_command))
    app.add_handler(CommandHandler(["mp4", "video"], mp4_command))
    app.add_handler(CommandHandler(["cancel", "cancelar"], cancel_command))
    app.add_handler(CommandHandler(["historial", "history"], history_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("✅ Bot listo y a la escucha. Presiona Ctrl+C para detenerlo.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
