"""
Batch: download variante images from medesy.it, remove background via
edge-guided flood fill, upscale to white square canvas, upload to Zoho.
"""
import json, os, sys, io, time, requests, numpy as np
from PIL import Image, ImageFilter
from collections import deque
from urllib.request import Request, urlopen

ZOHO_API_BASE = "https://www.zohoapis.com/inventory/v1"
ENV_PATH = "/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho/.env"
SCAN_PATH = os.environ.get("SCAN_OVERRIDE", "/Users/Leke_Kar/Antigravity/SNL/Dental Solutions/Zoho/medesy_image_scan.json")

def load_env():
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v
    return env

def get_zoho_token(env):
    resp = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
        "refresh_token": env["ZOHO_REFRESH_TOKEN"],
        "client_id": env["ZOHO_CLIENT_ID"],
        "client_secret": env["ZOHO_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    }, timeout=15)
    return resp.json()["access_token"]

def remove_bg_and_upscale(img_bytes):
    """Edge-guided flood fill bg removal + upscale to white square canvas."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
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
                visited[y, x] = True; bg_mask[y, x] = True; queue.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if not visited[y, x] and not is_edge[y, x]:
                visited[y, x] = True; bg_mask[y, x] = True; queue.append((y, x))
    while queue:
        cy, cx = queue.popleft()
        for dy, dx in ((-1,0),(1,0),(0,-1),(0,1)):
            ny, nx = cy+dy, cx+dx
            if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx]:
                visited[ny, nx] = True
                if not is_edge[ny, nx]:
                    bg_mask[ny, nx] = True; queue.append((ny, nx))

    # Remove artifact edge pixels (mostly surrounded by bg)
    result = arr.copy()
    result[bg_mask, 3] = 0
    edge_ys, edge_xs = np.where(is_edge & ~bg_mask)
    for ey, ex in zip(edge_ys, edge_xs):
        y_lo, y_hi = max(0, ey-1), min(h, ey+2)
        x_lo, x_hi = max(0, ex-1), min(w, ex+2)
        patch = bg_mask[y_lo:y_hi, x_lo:x_hi]
        if patch.sum() / patch.size > 0.6:
            result[ey, ex, 3] = 0

    # Smooth alpha
    alpha_ch = Image.fromarray(result[:,:,3])
    alpha_sm = np.array(alpha_ch.filter(ImageFilter.GaussianBlur(radius=1)))
    result[:,:,3] = np.where(alpha_sm < 128, 0, 255).astype(np.uint8)

    # Crop to content
    nz = np.where(result[:,:,3] > 0)
    if len(nz[0]) == 0:
        return None  # all removed — skip
    y_min, y_max = max(0, nz[0].min()-3), min(h-1, nz[0].max()+3)
    x_min, x_max = max(0, nz[1].min()-3), min(w-1, nz[1].max()+3)
    product = Image.fromarray(result[y_min:y_max+1, x_min:x_max+1], "RGBA")

    # Upscale shortest side to 1000px, but cap at 2000px canvas (Zoho 49MP limit)
    pw, ph = product.size
    scale = max(1000 / min(pw, ph), 1.0)
    nw, nh = int(pw * scale), int(ph * scale)
    # Cap so canvas stays under 49MP (7000x7000) — target max 2000x2000
    max_dim = 1920
    if max(nw, nh) > max_dim:
        cap_scale = max_dim / max(nw, nh)
        nw, nh = int(nw * cap_scale), int(nh * cap_scale)
    product_up = product.resize((nw, nh), Image.LANCZOS)

    # White square canvas
    cs = max(nw, nh) + 80
    canvas = Image.new("RGBA", (cs, cs), (255, 255, 255, 255))
    canvas.paste(product_up, ((cs-nw)//2, (cs-nh)//2), product_up)
    final = canvas.convert("RGB")

    buf = io.BytesIO()
    final.save(buf, "PNG")
    return buf.getvalue()

def upload_image_to_zoho(token, org_id, item_id, png_bytes, filename):
    """Upload image to Zoho item."""
    resp = requests.post(
        f"{ZOHO_API_BASE}/items/{item_id}/images",
        headers={"Authorization": f"Bearer {token}"},
        params={"organization_id": org_id},
        files={"image": (filename, png_bytes, "image/png")},
        timeout=30,
    )
    return resp.status_code, resp.text[:200]

def set_zoho_status(token, org_id, item_id, status, source_url=""):
    """Set cf_shopify_status (and optionally cf_source_url) on a Zoho item."""
    fields = [{"api_name": "cf_shopify_status", "value": status}]
    if source_url:
        fields.append({"api_name": "cf_source_url", "value": source_url[:100]})
    resp = requests.put(
        f"{ZOHO_API_BASE}/items/{item_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"organization_id": org_id},
        json={"custom_fields": fields},
        timeout=15,
    )
    return resp.status_code

def main():
    env = load_env()
    token = get_zoho_token(env)
    org_id = env["ZOHO_ORGANIZATION_ID"]

    with open(SCAN_PATH) as f:
        scan = json.load(f)
    items = scan["variante"]
    print(f"Processing {len(items)} variante items\n")

    ok, fail, skip = 0, 0, 0
    for i, item in enumerate(items):
        sku = item["sku"]
        url = item["url"]
        item_id = item["item_id"]
        tag = f"[{i+1}/{len(items)}] {sku}"

        # Download
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"{tag} DOWNLOAD FAIL: {e}")
            fail += 1; continue

        # Process
        try:
            png = remove_bg_and_upscale(raw)
        except Exception as e:
            print(f"{tag} PROCESS FAIL: {e}")
            fail += 1; continue
        if png is None:
            print(f"{tag} SKIP (all pixels removed)")
            skip += 1; continue

        # Upload
        fname = f"{sku}_cutout.png"
        code, body = upload_image_to_zoho(token, org_id, item_id, png, fname)
        if code not in (200, 201):
            # Maybe stale item_id — search by SKU
            search = requests.get(
                f"{ZOHO_API_BASE}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"organization_id": org_id, "search_text": sku},
                timeout=15,
            )
            found = False
            if search.status_code == 200:
                for it in search.json().get("items", []):
                    if it.get("sku") == sku:
                        item_id = it["item_id"]
                        code, body = upload_image_to_zoho(token, org_id, item_id, png, fname)
                        found = True; break
            if code not in (200, 201):
                print(f"{tag} UPLOAD FAIL ({code}): {body}")
                fail += 1; continue

        # Set status
        sc = set_zoho_status(token, org_id, item_id, "Image Validated", url[:100])
        print(f"{tag} OK (upload={code}, status={sc})")
        ok += 1

        # Pace to avoid rate limits
        if (i + 1) % 10 == 0:
            time.sleep(1)

    print(f"\nDone: {ok} OK, {fail} failed, {skip} skipped")

if __name__ == "__main__":
    main()
