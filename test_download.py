import os
import sys
import asyncio
from downloaders import VideoDownloader, detect_platform, format_duration, is_youtube_url, is_spotify_url


async def main():
    print("=" * 65)
    print("🧪 PRUEBA LOCAL ZERO-COOKIES: TIKTOK, X, IG, YT, SPOTIFY, FB")
    print("=" * 65)

    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        url = input("👉 Pega la URL del enlace a probar: ").strip()

    if not url:
        print("❌ No ingresaste ninguna URL.")
        return

    platform, emoji = detect_platform(url)

    if is_spotify_url(url):
        fmt = "mp3"
    elif len(sys.argv) > 2:
        fmt = sys.argv[2].strip().lower()
    else:
        fmt_choice = input("👉 Elige formato (1: MP3 Audio, 2: MP4 Video) [default: 2]: ").strip()
        fmt = "mp3" if fmt_choice == "1" else "mp4"

    print(f"\n{emoji} Plataforma detectada: {platform}")
    print(f"📦 Formato seleccionado: {fmt.upper()}")
    print("⏳ Descargando y procesando (Zero-Cookies)...")

    output_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(output_dir, exist_ok=True)

    downloader = VideoDownloader()

    try:
        result = await downloader.download(url, format_type=fmt)

        res_type = result.get('type')
        print(f"\n✅ ¡DESCARGA EXITOSA ({res_type.upper()})!")
        print("-" * 65)
        print(f"🎵/🎬 Título:       {result.get('title')}")
        print(f"🎤 Artista/Canal: {result.get('artist')}")
        if result.get('duration'):
            print(f"⏱️ Duración:      {format_duration(result.get('duration'))}")
        print(f"📦 Tamaño total:  {result.get('filesize', 0) / (1024 * 1024):.2f} MB")

        import shutil
        if res_type == 'carousel':
            items = result.get('media_items', [])
            print(f"📸 Elementos en carrusel: {len(items)}")
            for idx, item in enumerate(items):
                dest = os.path.join(output_dir, f"carousel_item_{idx}_{os.path.basename(item['file_path'])}")
                shutil.copy2(item['file_path'], dest)
                print(f"   -> Guardado: {dest}")
        else:
            original_file = result['file_path']
            dest_file = os.path.join(output_dir, os.path.basename(original_file))
            shutil.copy2(original_file, dest_file)
            print(f"📁 Guardado en:   {dest_file}")
            if result.get('thumbnail_path'):
                print(f"🖼️ Carátula:       {result.get('thumbnail_path')}")

        print("-" * 65)
        downloader.cleanup(result)

    except Exception as e:
        print(f"\n❌ Error al descargar: {e}")
        if getattr(e, "detail", ""):
            print(f"   Detalle técnico: {e.detail[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
