import os
import sys
import asyncio
from downloader import VideoDownloader, detect_platform, format_duration

async def main():
    print("=" * 60)
    print("🧪 PRUEBA LOCAL DE DESCARGA DE VIDEOS")
    print("=" * 60)
    print("Puedes probar enlaces de Instagram, TikTok, Facebook, YouTube, etc.\n")

    # Get URL from command line argument or prompt user
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
    else:
        url = input("👉 Pega la URL del video que deseas probar: ").strip()

    if not url:
        print("❌ No ingresaste ninguna URL.")
        return

    platform, emoji = detect_platform(url)
    print(f"\n{emoji} Plataforma detectada: {platform}")
    print("⏳ Descargando video y procesando información...")

    output_dir = os.path.join(os.path.dirname(__file__), "downloads")
    os.makedirs(output_dir, exist_ok=True)

    downloader = VideoDownloader()

    try:
        result = await downloader.download(url)

        # Move the downloaded file to downloads/ folder for inspection
        original_file = result['file_path']
        filename = os.path.basename(original_file)
        dest_file = os.path.join(output_dir, filename)

        import shutil
        shutil.copy2(original_file, dest_file)

        print("\n✅ ¡DESCARGA EXITOSA!")
        print("-" * 60)
        print(f"🎬 Título:      {result.get('title')}")
        print(f"⏱️ Duración:    {format_duration(result.get('duration'))}")
        print(f"📦 Tamaño:      {result.get('filesize', 0) / (1024 * 1024):.2f} MB")
        print(f"📐 Resolución:  {result.get('width')}x{result.get('height')}")
        print(f"📁 Guardado en: {dest_file}")
        if result.get('thumbnail_path'):
            print(f"🖼️ Miniatura:   {result.get('thumbnail_path')}")
        print("-" * 60)
        print(f"\nPuedes abrir y reproducir el video guardado en:\n{dest_file}")

        # Clean up temp files
        downloader.cleanup(result)

    except Exception as e:
        print(f"\n❌ Error al descargar el video: {e}")

if __name__ == "__main__":
    asyncio.run(main())
