"""GeeTest v4 slider image matching — pure NumPy/OpenCV.

Ported from YanamiNeko/NekoSportsWorldTool ``src/api/gt4image.rs``.  The
pipeline is deliberately identical to upstream so that a diff against their
Rust code stays mechanical:

    PNG -> gray (alpha composited onto black)
        -> 5x5 gaussian, sigma 1.1, edge-clamped
        -> Canny(100, 200)
        -> zero-mean normalised cross correlation (TM_CCOEFF_NORMED)

**Why this file is separate from** :mod:`replica.transport.gt4_payload`:
GeeTest changes the ``w`` field set far more often than it changes the image
pipeline.  Splitting them means a payload-only update does not force anyone to
re-validate the CV code, and a CV change cannot silently alter the payload.

Nothing here talks to the network.  ``gap_distance`` returns **image pixels**;
converting to slider-track pixels is the payload module's job.
"""

from __future__ import annotations

import cv2
import numpy as np


def decode_gray(png: bytes) -> np.ndarray:
    """PNG bytes -> uint8 grayscale (alpha composited onto black)."""
    arr = np.frombuffer(png, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError('PNG decode failed')
    if img.ndim == 3 and img.shape[2] == 4:
        b, g, r, a = (img[:, :, i].astype(np.uint32) for i in range(4))
        a = a // 255
        b, g, r = b * a, g * a, r * a
        gray = (r * 299 + g * 587 + b * 114) // 1000
    elif img.ndim == 3:
        b, g, r = (img[:, :, i].astype(np.uint32) for i in range(3))
        gray = (r * 299 + g * 587 + b * 114) // 1000
    else:
        gray = img.astype(np.uint32)
    return np.clip(gray, 0, 255).astype(np.uint8)


def dimensions(png: bytes) -> tuple[int, int]:
    """``(width, height)`` of a PNG, without running the whole pipeline."""
    gray = decode_gray(png)
    return int(gray.shape[1]), int(gray.shape[0])


def gaussian_blur(src: np.ndarray) -> np.ndarray:
    """Separable 5-tap gaussian, sigma = 1.1, clamped edges (matches upstream)."""
    sigma = 1.1
    xs = np.arange(5, dtype=np.float64) - 2.0
    kernel = np.exp(-(xs * xs) / (2 * sigma * sigma))
    kernel /= kernel.sum()
    f = src.astype(np.float64)
    pad = np.pad(f, ((0, 0), (2, 2)), mode='edge')
    tmp = sum(kernel[i] * pad[:, i:i + f.shape[1]] for i in range(5))
    pad = np.pad(tmp, ((2, 2), (0, 0)), mode='edge')
    return sum(kernel[i] * pad[i:i + f.shape[0], :] for i in range(5))


def canny(img: np.ndarray, low: float = 100.0, high: float = 200.0) -> np.ndarray:
    """Sobel -> magnitude/angle -> non-maximum suppression -> hysteresis."""
    f = img.astype(np.float64)
    p = np.pad(f, 1, mode='edge')
    gx = (-p[:-2, :-2] + p[:-2, 2:]
          - 2 * p[1:-1, :-2] + 2 * p[1:-1, 2:]
          - p[2:, :-2] + p[2:, 2:])
    gy = (-p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
          + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
    mag = np.hypot(gx, gy)
    ang = np.arctan2(gy, gx) % np.pi

    h, w = mag.shape
    nms = np.zeros_like(mag)
    pad = np.pad(mag, 1, mode='edge')
    for y in range(h):
        for x in range(w):
            a = ang[y, x]
            if a < np.pi / 8 or a >= 7 * np.pi / 8:
                (dx, dy) = (1, 0)
            elif a < 3 * np.pi / 8:
                (dx, dy) = (1, 1)
            elif a < 5 * np.pi / 8:
                (dx, dy) = (0, 1)
            else:
                (dx, dy) = (-1, 1)
            m = pad[y + 1, x + 1]
            if m >= pad[y + 1 + dy, x + 1 + dx] and m >= pad[y + 1 - dy, x + 1 - dx]:
                nms[y, x] = m

    out = np.zeros(img.shape, np.uint8)
    strong = nms >= high
    weak = nms >= low
    out[strong] = 255
    stack = list(zip(*np.nonzero(strong)))
    while stack:
        y, x = stack.pop()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and out[ny, nx] == 0 and weak[ny, nx]:
                    out[ny, nx] = 255
                    stack.append((ny, nx))
    return out


def locate_gap(bg_png: bytes, slice_png: bytes) -> dict:
    """Return ``{'x', 'bg_width', 'bg_height'}``; ``x`` is in image pixels.

    ``bg_width`` is what the payload module needs to decide whether the image
    is a normal-size render or a scaled one (retina / different widget width).
    """
    bg_gray = decode_gray(bg_png)
    sl_gray = decode_gray(slice_png)
    bg_edges = canny(gaussian_blur(bg_gray))
    sl_edges = canny(gaussian_blur(sl_gray))
    if int((sl_edges == 255).sum()) < 10:
        raise ValueError('slice has too few edge pixels (recognition failed)')
    if (sl_edges.shape[0] > bg_edges.shape[0]
            or sl_edges.shape[1] > bg_edges.shape[1]):
        raise ValueError('slice larger than background')
    res = cv2.matchTemplate(bg_edges, sl_edges, cv2.TM_CCOEFF_NORMED)
    _, _, _, max_loc = cv2.minMaxLoc(res)
    return {
        'x': int(max_loc[0]),
        'bg_width': int(bg_edges.shape[1]),
        'bg_height': int(bg_edges.shape[0]),
    }


def gap_distance(bg_png: bytes, slice_png: bytes) -> int:
    """Return the gap x offset in image pixels (left edge of the best match)."""
    return locate_gap(bg_png, slice_png)['x']


def self_test() -> list[tuple[str, bool, str]]:
    out = []

    bg = np.zeros((60, 200), np.uint8)
    bg[:] = 40
    cv2.rectangle(bg, (120, 20), (160, 55), 220, -1)
    cut_x = 100
    slice_img = bg[20:56, cut_x:cut_x + 41].copy()
    ok, bg_png = cv2.imencode('.png', bg)
    ok2, sl_png = cv2.imencode('.png', slice_img)
    found = locate_gap(bg_png.tobytes(), sl_png.tobytes())
    out.append(('gap localised', abs(found['x'] - cut_x) <= 3,
                'x=%d (want %d)' % (found['x'], cut_x)))
    out.append(('bg width reported', found['bg_width'] == 200,
                '%d px' % found['bg_width']))
    out.append(('dimensions helper', dimensions(bg_png.tobytes()) == (200, 60),
                str(dimensions(bg_png.tobytes()))))

    # An all-black slice has no edge pixels: must fail loudly, not return 0.
    flat = np.zeros((30, 30), np.uint8)
    _, flat_png = cv2.imencode('.png', flat)
    try:
        locate_gap(bg_png.tobytes(), flat_png.tobytes())
        out.append(('blank slice rejected', False, 'no exception'))
    except ValueError:
        out.append(('blank slice rejected', True, 'ValueError'))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-28s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)
