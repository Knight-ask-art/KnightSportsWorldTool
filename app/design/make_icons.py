"""Reproducible app-icon generator (favicon + Windows .ico).

The shipped .ico used to contain a single 16x16 frame, so Windows upscaled it
to 32/48/256 and the mark turned into a blur.  This script rebuilds the cream
rounded tile from the 512 px transparent mark at every size Windows asks for.

Small frames are written as 32-bit DIBs (maximum compatibility with the NSIS
installer and older shells); the 256 px frame is PNG-compressed, which is the
normal Vista+ layout.

Run from the repository root:

    python app/design/make_icons.py
"""
import struct
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
MARK = ROOT / 'frontend' / 'logo.png'
FAVICON = ROOT / 'frontend' / 'favicon.png'
ICO = ROOT / 'packaging' / 'assets' / 'logo.ico'
# The runtime window icon: pywebview's WinForms backend sets Form.Icon from a
# file next to the bundled frontend, so the title bar does not depend on the
# exe resource being refreshed in Explorer's cache.
APP_ICO = ROOT / 'frontend' / 'app.ico'

SIZE = 1024
BG = (246, 241, 227, 255)          # sampled from the original favicon
RADIUS = round(SIZE * 0.22)        # ~14 px on the old 64 px tile
MARK_WIDTH = 0.62                  # mark occupies 62 % of the tile width
                                   # (a bit larger than the old 53 % so the
                                   # 16/24 px taskbar frames stay legible)
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48),
             (64, 64), (128, 128), (256, 256)]


def build_tile() -> Image.Image:
    mark = Image.open(MARK).convert('RGBA')
    box = mark.getbbox()
    if box:
        mark = mark.crop(box)
    target_w = round(SIZE * MARK_WIDTH)
    target_h = round(mark.height * target_w / mark.width)
    mark = mark.resize((target_w, target_h), Image.LANCZOS)

    tile = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], RADIUS, fill=BG)
    tile.alpha_composite(
        mark, ((SIZE - target_w) // 2, (SIZE - target_h) // 2))
    return tile


def _dib_frame(image: Image.Image) -> bytes:
    """32-bit bottom-up DIB + empty AND mask (alpha carries transparency)."""
    width, height = image.size
    pixels = image.load()
    xor = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            xor += bytes((b, g, r, a))
    mask_row = ((width + 31) // 32) * 4
    mask = bytes(mask_row * height)
    header = struct.pack('<IiiHHIIiiII', 40, width, height * 2, 1, 32, 0,
                         len(xor) + len(mask), 0, 0, 0, 0)
    return header + bytes(xor) + mask


def _png_frame(image: Image.Image) -> bytes:
    from io import BytesIO
    buf = BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


def write_ico(path: Path, frames: list[tuple[int, bytes]]) -> None:
    header = struct.pack('<HHH', 0, 1, len(frames))
    entries = bytearray()
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        entries += struct.pack('<BBBBHHII',
                               0 if size >= 256 else size,
                               0 if size >= 256 else size,
                               0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(header + bytes(entries) + b''.join(d for _, d in frames))


def main() -> None:
    tile = build_tile()
    frames = []
    for size, _ in ICO_SIZES:
        image = tile.resize((size, size), Image.LANCZOS)
        frames.append((size, _png_frame(image) if size >= 256
                       else _dib_frame(image)))
    ICO.parent.mkdir(parents=True, exist_ok=True)
    # A multi-frame ICO is the actual fix: 16-only icons get stretched by the
    # taskbar, Explorer and the installer.
    write_ico(ICO, frames)
    write_ico(APP_ICO, frames)
    tile.resize((256, 256), Image.LANCZOS).save(FAVICON, format='PNG')
    print('wrote', ICO, ICO.stat().st_size, 'bytes')
    print('wrote', APP_ICO, APP_ICO.stat().st_size, 'bytes')
    print('wrote', FAVICON, FAVICON.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
