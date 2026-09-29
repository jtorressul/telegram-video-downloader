import os
import sys
import asyncio
from downloader import VideoDownloader, detect_platform, format_duration, is_youtube_url, is_spotify_url

async def main():
    print("=" * 60)
    print("🧪 PRUEBA LOCAL: YOUTUBE, YOUTUBE MUSIC & SPOTIFY (MP3 / MP4)")
    print("=" * 60)

    # URL
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        url = input("👉 Pega la URL (YouTube, YouTube Music o Spotify): ").strip()

    if not url:
        print("❌ No ingresaste ninguna URL.")
        return

    if not is_youtube_url(url) and not is_spotify_url(url):
        print("❌ La URL no parece pertenecer a YouTube, YouTube Music o Spotify.")
        return

    platform, emoji = detect_platform(url)

    # Format
    if is_spotify_url(url):
        fmt = "mp3"
    elif len(sys.argv) > 2:
        fmt = sys.argv[2].strip().lower()
    else:
        fmt_choice = input("👉 Elige formato (1: MP3 Audio, 2: MP4 Video) [default: 1]: ").strip()
        fmt = "mp4" if fmt_choice == "2" else "mp3"

    platform, emoji = detect_platform(url)
    print(f"\n{emoji} Plataforma: {platform}")
    print(f"📦 Formato seleccionado: {fmt.upper()}")
    print("⏳ Descargando y procesando...")

    output_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(output_dir, exist_ok=True)

    downloader = VideoDownloader()

    try:
        result = await downloader.download(url, format_type=fmt)

        original_file = result['file_path']
        filename = os.path.basename(original_file)
        dest_file = os.path.join(output_dir, filename)

        import shutil
        shutil.copy2(original_file, dest_file)

        print(f"\n✅ ¡DESCARGA {fmt.upper()} EXITOSA!")
        print("-" * 60)
        print(f"🎵/🎬 Título:   {result.get('title')}")
        print(f"🎤 Artista:     {result.get('artist')}")
        print(f"⏱️ Duración:    {format_duration(result.get('duration'))}")
        print(f"📦 Tamaño:      {result.get('filesize', 0) / (1024 * 1024):.2f} MB")
        print(f"📁 Guardado en: {dest_file}")
        if result.get('thumbnail_path'):
            print(f"🖼️ Carátula:   {result.get('thumbnail_path')}")
        print("-" * 60)
        print(f"\nPuedes abrir y reproducir el archivo guardado en:\n{dest_file}")

        downloader.cleanup(result)

    except Exception as e:
        print(f"\n❌ Error al descargar: {e}")

if __name__ == "__main__":
    asyncio.run(main())
