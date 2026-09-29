# 🎬 Bot de Telegram: Descargador Multi-Plataforma con Sistema VIP y Estadísticas

Bot de Telegram desarrollado en Python para descargar videos y música en alta calidad con sistema integrado de membresías **VIP vs NO VIP PASS**, control de cuotas diarias y estadísticas personales.

---

## 👑 Sistema de Membresías y Límites

| Rango | Estado en `/stats` | Cuota Diaria | Plataformas Permitidas |
| :--- | :--- | :--- | :--- |
| 🆓 **NO VIP** | `NO VIP PASS` | **5 descargas / día** | 📸 **Instagram**, 🎵 **TikTok**, 🐦 **X (Twitter)** |
| 👑 **VIP** | `VIP` | **15 descargas / día** | 🌐 **Todas** (▶️ YouTube MP3/MP4, 🟢 Spotify MP3, 👥 Facebook, IG, TikTok, X) |

### 🔍 Detección Automática de VIP en Grupos:
El bot detecta automáticamente a los miembros VIP si su **Título Personalizado (Custom Title)** en el grupo de Telegram contiene la palabra **`VIP`** (por ejemplo: `DROGUITA - VIP`, `SIN SOMBRA - VIP`, `ALFREDO PC - VIP`), o si son los creadores del grupo. ¡Cero configuración manual requerida!

---

## 📊 Estadísticas Personales (`/stats`)

Cada usuario puede consultar sus estadísticas en tiempo real enviando `/stats` o `/estadisticas`:

```text
📊 Tus Estadísticas

💎 Estado: VIP (o NO VIP PASS)
📥 Descargas: 14
📱 Social: 10 | 🌐 Otras: 4
🗓 Cuota diaria: 14/15 (o 3/5 si es NO VIP)
```

- **Social:** Contador de descargas de Instagram, TikTok y X.
- **Otras:** Contador de descargas de YouTube, Spotify, Facebook, etc.
- **Cuota diaria:** Se reinicia automáticamente cada día a la medianoche (00:00).

---

## ✨ Características Principales

1. **Soporte Spotify & YouTube MP3:**
   - Descarga música de **Spotify** y **YouTube** en MP3 de 192kbps con carátula de alta resolución, artista y título incrustados.
2. **Selector MP3 / MP4:**
   - Para enlaces de YouTube, muestra botones para elegir entre **🎵 Descargar MP3** (Audio 192k con carátula) o **🎬 Descargar MP4** (Video en HD).
3. **Descarga de Fotos, Álbumes y Carruseles:**
   - Soporte completo para descargar imágenes individuales o álbumes completos agrupados (hasta 10 fotos/videos por mensaje) desde **Instagram**, **X (Twitter)** y presentaciones fotográficas de **TikTok**.
4. **Soporte para Contenido +18 / Sensible / Restringido:**
   - Descarga videos e imágenes sensibles / +18 de **X (Twitter)** automáticamente sin requerir inicio de sesión mediante el extractor directo FxTwitter.
   - Para **Instagram**, soporte directo de publicaciones restringidas con cookies o `INSTAGRAM_SESSIONID`.
5. **Auto-Eliminación en Grupos:**
   - Si el bot tiene permisos de Administrador (*Eliminar mensajes*), borra automáticamente el enlace original del chat una vez enviado el contenido.
6. **Control de Restricciones y Cuotas:**
   - Si un usuario `NO VIP PASS` intenta descargar de YouTube, Spotify o Facebook, el bot le informa que esas plataformas son exclusivas para miembros VIP.
   - Si se supera el límite diario (5 para NO VIP o 15 para VIP), notifica el reinicio a las 00:00.
7. **Comandos de Administración:**
   - `/vip [user_id]` (o respondiendo a un usuario): Activa manualmente el rango VIP.
   - `/unvip [user_id]`: Quita el rango VIP.
8. **Caché Ultra Rápida (SQLite):**
   - Si un archivo (video, audio o foto) ya fue descargado previamente, se entrega en menos de **0.5 segundos**.
9. **Reinicio de Cuotas a las 12:00 AM (Hora Local):**
   - Tarea programada en segundo plano que reinicia automáticamente las cuotas diarias a las 00:00:00 (12:00 AM) hora local todos los días (configurable con `TIMEZONE` en `.env`).
10. **Compatibilidad Total con Instagram:**
    - Soporta enlaces de Reels, Posts, Carruseles, enlaces compartidos desde la app móvil (`/share/reel/`, etc.) y autenticación con `cookies.txt` o `INSTAGRAM_SESSIONID`.

---

## 🛠️ Configuración y Ejecución

```bash
# 1. Configura tu token y variables opcionales en .env
TELEGRAM_BOT_TOKEN=tu_token_aqui
INSTAGRAM_SESSIONID=tu_session_id_aqui  # Opcional para desbloquear cualquier video de IG
TIMEZONE=America/New_York                # Opcional (por defecto usa la hora de la máquina)

# 2. Inicia el bot
./run.sh
```
