"""
Shared image processing utilities: background detection, background removal
(edge-guided flood fill), and upscale-to-canvas.

Used by fetch_images.py and validate_images.py to auto-upscale undersized
images that pass aspect-ratio checks but are below the 800x800 minimum.
"""

import io
import numpy as np
from PIL import Image, ImageFilter
from collections import deque


def detect_non_white_background(pil_img):
    """
    Sample border pixels to determine if the background is non-white.
    Returns True if background appears colored/gradient (needs removal).
    Returns False if white or transparent (skip bg removal).
    """
    img = pil_img.convert('RGBA')
    arr = np.array(img)
    h, w = arr.shape[:2]

    # Collect border pixels (top/bottom rows, left/right columns)
    border = np.concatenate([
        arr[0, :, :],       # top row
        arr[h-1, :, :],     # bottom row
        arr[:, 0, :],       # left column
        arr[:, w-1, :],     # right column
    ], axis=0)

    # If mostly transparent borders, bg is already transparent — no removal needed
    alphas = border[:, 3]
    if np.mean(alphas < 200) > 0.8:
        return False

    # Check RGB distance from white
    rgb = border[:, :3].astype(float)
    avg_color = rgb.mean(axis=0)
    distance = np.sqrt(np.sum((avg_color - 255.0) ** 2))

    return distance > 30.0


def remove_background(pil_img):
    """
    Edge-guided flood fill background removal.
    Returns a cropped RGBA PIL Image with transparent background, or None
    if all pixels are removed.
    """
    img = pil_img.convert('RGBA')
    arr = np.array(img)
    h, w = arr.shape[:2]
    rgb = arr[:, :, :3].astype(float)

    # Sobel gradient magnitude
    gray = rgb.mean(axis=2)
    padded = np.pad(gray, 1, mode='edge')
    gx = padded[1:-1, 2:] - padded[1:-1, :-2]
    gy = padded[2:, 1:-1] - padded[:-2, 1:-1]
    gradient = np.sqrt(gx**2 + gy**2)
    grad_max = gradient.max()
    if grad_max > 0:
        gradient = gradient / grad_max * 255
    is_edge = gradient > 15

    # Flood fill from borders, stop at edges
    visited = np.zeros((h, w), dtype=bool)
    bg_mask = np.zeros((h, w), dtype=bool)
    queue = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not visited[y, x] and not is_edge[y, x]:
                visited[y, x] = True
                bg_mask[y, x] = True
                queue.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if not visited[y, x] and not is_edge[y, x]:
                visited[y, x] = True
                bg_mask[y, x] = True
                queue.append((y, x))
    while queue:
        cy, cx = queue.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx]:
                visited[ny, nx] = True
                if not is_edge[ny, nx]:
                    bg_mask[ny, nx] = True
                    queue.append((ny, nx))

    # Remove artifact edge pixels (mostly surrounded by bg)
    result = arr.copy()
    result[bg_mask, 3] = 0
    edge_ys, edge_xs = np.where(is_edge & ~bg_mask)
    for ey, ex in zip(edge_ys, edge_xs):
        y_lo, y_hi = max(0, ey - 1), min(h, ey + 2)
        x_lo, x_hi = max(0, ex - 1), min(w, ex + 2)
        patch = bg_mask[y_lo:y_hi, x_lo:x_hi]
        if patch.sum() / patch.size > 0.6:
            result[ey, ex, 3] = 0

    # Smooth alpha
    alpha_ch = Image.fromarray(result[:, :, 3])
    alpha_sm = np.array(alpha_ch.filter(ImageFilter.GaussianBlur(radius=1)))
    result[:, :, 3] = np.where(alpha_sm < 128, 0, 255).astype(np.uint8)

    # Crop to content
    nz = np.where(result[:, :, 3] > 0)
    if len(nz[0]) == 0:
        return None
    y_min = max(0, nz[0].min() - 3)
    y_max = min(h - 1, nz[0].max() + 3)
    x_min = max(0, nz[1].min() - 3)
    x_max = min(w - 1, nz[1].max() + 3)
    return Image.fromarray(result[y_min:y_max+1, x_min:x_max+1], 'RGBA')


def upscale_to_canvas(pil_img, min_side=1000, max_canvas=1920, padding=80):
    """
    Upscale image so shortest side >= min_side, place on white square canvas.
    Returns PNG bytes.
    """
    if pil_img.mode != 'RGBA':
        pil_img = pil_img.convert('RGBA')

    pw, ph = pil_img.size
    scale = max(min_side / min(pw, ph), 1.0)
    nw, nh = int(pw * scale), int(ph * scale)

    if max(nw, nh) > max_canvas:
        cap_scale = max_canvas / max(nw, nh)
        nw, nh = int(nw * cap_scale), int(nh * cap_scale)

    product_up = pil_img.resize((nw, nh), Image.LANCZOS)

    cs = max(nw, nh) + padding
    canvas = Image.new('RGBA', (cs, cs), (255, 255, 255, 255))
    canvas.paste(product_up, ((cs - nw) // 2, (cs - nh) // 2), product_up)
    final = canvas.convert('RGB')

    buf = io.BytesIO()
    final.save(buf, 'PNG')
    return buf.getvalue()


def auto_upscale(image_bytes):
    """
    Upscale image so shortest side >= 1000px, place on white square canvas.
    Returns (png_bytes, width, height) or None on failure.

    NOTE: Background removal is intentionally NOT performed here.
    Per project rules, background removal is never run automatically —
    a bad cut-out is worse than a coloured background, and dental
    professionals will notice any artefacts immediately. If the source
    image has a non-white background the padded white canvas still looks
    acceptable; the team can source a cleaner image manually if needed.
    """
    try:
        pil_img = Image.open(io.BytesIO(image_bytes)).convert('RGBA')
    except Exception:
        return None

    png_bytes = upscale_to_canvas(pil_img)

    final = Image.open(io.BytesIO(png_bytes))
    return png_bytes, final.size[0], final.size[1]
