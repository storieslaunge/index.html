import argparse
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
IMAGE_ROOT = ROOT / 'pictures' / 'stories_kamiqr' / 'menuItems'
MAX_EDGE = 1440
QUALITY = 82


def dimensions(ffprobe, image):
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
         '-of', 'csv=s=x:p=0', str(image)],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(map(int, result.stdout.strip().split('x')))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create mobile-sized WebP derivatives of menu photos.')
    parser.add_argument('--force', action='store_true', help='Rebuild existing WebP derivatives')
    args = parser.parse_args(argv)

    cwebp = shutil.which('cwebp')
    ffprobe = shutil.which('ffprobe')
    if not cwebp or not ffprobe:
        parser.error('Install cwebp and ffprobe (provided by the WebP and FFmpeg packages).')

    images = [image for image in IMAGE_ROOT.rglob('*') if image.is_file() and image.suffix.lower() in {'.jpg', '.jpeg', '.png'}]
    original_bytes = sum(image.stat().st_size for image in images)
    converted = 0
    for image in images:
        webp = image.with_suffix('.webp')
        if webp.exists() and not args.force:
            continue
        width, height = dimensions(ffprobe, image)
        command = [cwebp, '-quiet', '-m', '6', '-q', str(QUALITY)]
        if max(width, height) > MAX_EDGE:
            command.extend(['-resize', str(MAX_EDGE), str(MAX_EDGE)])
        command.extend([str(image), '-o', str(webp)])
        subprocess.run(command, check=True)
        converted += 1

    webp_images = [image.with_suffix('.webp') for image in images if image.with_suffix('.webp').is_file()]
    webp_bytes = sum(image.stat().st_size for image in webp_images)
    print(f'Converted {converted} images; {original_bytes:,} original bytes -> {webp_bytes:,} WebP bytes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())