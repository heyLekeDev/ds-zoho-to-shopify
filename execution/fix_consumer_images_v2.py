#!/usr/bin/env python3
"""
fix_consumer_images_v2.py — Accurate product image sourcing via product page extraction.

Strategy (per product):
  Tier 1 — Brand manufacturer page
    GUM → sunstargum.com
    TePe → tepe.com
    Oral-B → oral-b.co.uk (via Chrome if needed, else Amazon.com)
  Tier 2 — Amazon.com product page (requests works; .co.uk blocks bots)

Both tiers:
  1. DDG text search (no quota) → product page URL
  2. Fetch page with requests
  3. Verify H1/title contains brand + distinctive terms  (catches wrong product)
  4. Extract main product image URL
  5. Extract EAN/barcode if present

Post-find:
  - Upload image to Shopify
  - Write EAN back to Zoho item if found

Usage:
    python execution/fix_consumer_images_v2.py
    python execution/fix_consumer_images_v2.py --dry-run
    python execution/fix_consumer_images_v2.py --handle reach-kids-barbie-manual-toothbrush
"""

import os, sys, io, json, re, time, base64, argparse, requests
from urllib.parse import urlparse, unquote, parse_qs
from datetime import date
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(dotenv_path='.env')

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed: pip install Pillow'); sys.exit(1)

try:
    from ddgs import DDGS
except ImportError:
    print('✗ ddgs not installed: pip install ddgs'); sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

SHOPIFY_SHOP_URL   = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID  = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SEC = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VER    = '2025-01'

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SEC    = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')

MIN_WIDTH  = 600
MIN_HEIGHT = 600
ASPECT_MIN = 0.35
ASPECT_MAX = 2.5

REQUESTS_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# ── Consumer Oral Care batch ──────────────────────────────────────────────────

BATCH = [
    {"handle": "colgate-kids-battery-toothbrush",
     "title": "Colgate Kids Battery Toothbrush", "vendor": "colgate"},
    {"handle": "oral-b-kids-character-toothbrush",
     "title": "Oral-B Kids Character Toothbrush", "vendor": "oral-b"},
    {"handle": "oral-b-crossaction-replacement-heads",
     "title": "Oral-B CrossAction Replacement Heads", "vendor": "oral-b",
     "search_query": "Oral-B CrossAction replacement brush heads electric toothbrush"},
    {"handle": "oral-b-io-ultimate-clean-replacement-heads",
     "title": "Oral-B iO Ultimate Clean Replacement Heads", "vendor": "oral-b",
     "search_query": "Oral-B iO Ultimate Clean replacement brush heads refills"},
    {"handle": "oral-b-io-series-2-electric-toothbrush",
     "title": "Oral-B iO Series 2 Electric Toothbrush", "vendor": "oral-b"},
    {"handle": "tepe-interdental-brush-single-pack",
     "title": "TePe Interdental Brush Single Pack", "vendor": "tepe"},
    {"handle": "tepe-interdental-brush-bulk-pack",
     "title": "TePe Interdental Brush Bulk Pack", "vendor": "tepe"},
    {"handle": "dr-fresh-angry-birds-turbo-power-battery-toothbrush",
     "title": "Dr. Fresh Angry Birds Turbo Power Battery Toothbrush", "vendor": "dr. fresh",
     "search_query": "Dr Fresh Angry Birds turbo power battery toothbrush children"},
    {"handle": "dr-fresh-batman-turbo-power-battery-toothbrush",
     "title": "Dr. Fresh Batman Turbo Power Battery Toothbrush", "vendor": "dr. fresh",
     "search_query": "Dr Fresh Batman turbo power battery toothbrush children"},
    {"handle": "colgate-microsinic-toothbrush",
     "title": "Colgate Microsinic Toothbrush", "vendor": "colgate",
     "search_query": "Colgate MicroSonic electric toothbrush"},
    {"handle": "colgate-triple-action-toothbrush-medium",
     "title": "Colgate Triple Action Toothbrush Medium", "vendor": "colgate"},
    {"handle": "colgate-ultra-soft-toothbrush",
     "title": "Colgate Ultra Soft Toothbrush", "vendor": "colgate"},
    {"handle": "gum-crayola-kids-power-battery-toothbrush",
     "title": "GUM Crayola Kids Power Battery Toothbrush", "vendor": "gum"},
    {"handle": "gum-crayola-pip-squeaks-kids-manual-toothbrush",
     "title": "GUM Crayola Pip-SQUEAKs Kids Manual Toothbrush", "vendor": "gum"},
    {"handle": "gum-barbie-kids-manual-toothbrush-with-suction-cup",
     "title": "GUM Barbie Kids Manual Toothbrush with Suction Cup", "vendor": "gum"},
    {"handle": "gum-barbie-kids-power-battery-toothbrush",
     "title": "GUM Barbie Kids Power Battery Toothbrush", "vendor": "gum"},
    {"handle": "disney-lion-king-turbo-power-battery-toothbrush",
     "title": "Disney Lion King Turbo Power Battery Toothbrush", "vendor": "disney",
     "search_query": "Disney Lion King turbo battery toothbrush children Dr Fresh"},
    {"handle": "gum-crayola-kids-timer-light-toothbrush",
     "title": "GUM Crayola Kids Timer Light Toothbrush", "vendor": "gum"},
    {"handle": "oral-b-precision-clean-replacement-toothbrush-heads-4-pack",
     "title": "Oral-B Precision Clean Replacement Toothbrush Heads 4 Pack", "vendor": "oral-b",
     "search_query": "Oral-B Precision Clean replacement brush heads 4 pack refills"},
    {"handle": "oral-b-sensi-ultrathin-replacement-toothbrush-heads-2-pack",
     "title": "Oral-B Sensi UltraThin Replacement Toothbrush Heads 2 Pack", "vendor": "oral-b"},
    {"handle": "oral-b-super-floss-pre-cut-strands-50pk",
     "title": "Oral-B Super Floss Pre-Cut Strands 50 Pack", "vendor": "oral-b"},
    {"handle": "oral-b-kids-stages-2-manual-toothbrush-disney-frozen",
     "title": "Oral-B Kids Stages 2 Manual Toothbrush Disney Frozen", "vendor": "oral-b",
     "search_query": "Oral-B Kids Stages 2 manual toothbrush Disney Frozen soft bristles"},
    {"handle": "oral-b-kids-stages-2-manual-toothbrush-disney-pixar",
     "title": "Oral-B Kids Stages 2 Manual Toothbrush Disney Pixar", "vendor": "oral-b"},
    {"handle": "oral-b-stages-1-baby-manual-toothbrush-winnie-the-pooh",
     "title": "Oral-B Stages 1 Baby Manual Toothbrush Winnie the Pooh", "vendor": "oral-b",
     "search_query": "Oral-B Stages 1 Winnie Pooh baby manual toothbrush 0-2"},
    {"handle": "oral-b-stages-4-junior-manual-toothbrush-for-me",
     "title": "Oral-B Stages 4 Junior Manual Toothbrush", "vendor": "oral-b",
     "search_query": "Oral-B Stages 4 junior manual toothbrush kids 8+ years"},
    {"handle": "oral-b-precision-clean-toothbrush-heads-white-2-pack",
     "title": "Oral-B Precision Clean Toothbrush Heads White 2 Pack", "vendor": "oral-b",
     "search_query": "Oral-B Precision Clean replacement brush heads white 2 count"},
    {"handle": "oral-b-pro-health-stages-3-kids-manual-toothbrush-spiderman",
     "title": "Oral-B Stages 3 Kids Manual Toothbrush Spiderman", "vendor": "oral-b",
     "search_query": "Oral-B Stages 3 kids Spiderman manual toothbrush"},
    {"handle": "reach-advance-design-manual-toothbrush-soft-compact",
     "title": "Reach Advance Design Manual Toothbrush Soft Compact", "vendor": "reach",
     "search_query": "Reach Advanced Design toothbrush soft compact"},
    {"handle": "reach-kids-barbie-manual-toothbrush",
     "title": "Reach Kids Barbie Manual Toothbrush", "vendor": "reach",
     "search_query": "Reach Barbie kids toothbrush manual"},
    {"handle": "reach-total-care-manual-toothbrush-soft",
     "title": "Reach Total Care Manual Toothbrush Soft", "vendor": "reach",
     "search_query": "Reach Total Care toothbrush soft"},
    {"handle": "tepe-interdental-brush-mixed-pack-multi-size-kit",
     "title": "TePe Interdental Brush Mixed Pack", "vendor": "tepe"},
    {"handle": "tepe-interdental-brush-original-orange-045mm",
     "title": "TePe Interdental Brush Original Orange", "vendor": "tepe",
     "search_query": "TePe interdental brush original orange 0.45mm"},
    {"handle": "tepe-mini-flosser-pre-loaded-holders-36pk",
     "title": "TePe Mini Flosser Pre-Loaded Holders", "vendor": "tepe",
     "search_query": "TePe mini flosser dental floss holders"},
    {"handle": "tepe-professional-dental-study-model",
     "title": "TePe Professional Dental Study Model", "vendor": "tepe",
     "search_query": "TePe dental study model demo product"},
    {"handle": "tepe-bio-based-triple-action-tongue-cleaner",
     "title": "TePe Bio-Based Triple Action Tongue Cleaner", "vendor": "tepe",
     "search_query": "TePe tongue cleaner triple action"},

    # ── Extra zero-stock products (wrong images from original sync) ───────────
    {"handle": "oral-b-kids-manual-toothbrush-stages-4-galaxy",
     "title": "Oral-B Kids Manual Toothbrush Stages 4 Galaxy", "vendor": "oral-b",
     "search_query": "Oral-B Stages 4 Galaxy kids manual toothbrush"},
    {"handle": "oral-b-pro-expert-deep-clean-flossing-tape-50m",
     "title": "Oral-B Pro-Expert Deep Clean Flossing Tape 50m", "vendor": "oral-b",
     "search_query": "Oral-B Pro-Expert deep clean flossing tape 50m"},
    {"handle": "oral-b-pro-health-stages-1-baby-manual-toothbrush-pooh",
     "title": "Oral-B Pro-Health Stages 1 Baby Manual Toothbrush Pooh", "vendor": "oral-b",
     "search_query": "Oral-B Stages 1 baby manual toothbrush Winnie Pooh"},
    {"handle": "oral-b-pro-health-stages-3-kids-manual-toothbrush-princesses",
     "title": "Oral-B Pro-Health Stages 3 Kids Manual Toothbrush Princesses", "vendor": "oral-b",
     "search_query": "Oral-B Stages 3 kids Disney Princess manual toothbrush"},
]

# Tier 1 brand domains per vendor
TIER1_DOMAINS = {
    'gum':      ['sunstargum.com'],
    'tepe':     ['tepe.com'],
    'oral-b':   [],            # oral-b.co.uk needs JS; Amazon.com is more reliable
    'colgate':  [],
    'reach':    ['walgreens.com'],
    'dr. fresh': [],
    'disney':   [],
}

# ── Title match verification ──────────────────────────────────────────────────

STOPWORDS = {
    'the', 'and', 'for', 'with', 'pack', 'baby', 'ages',
    'count', 'piece', 'item', 'new', 'brush', 'size',
    'toothbrush', 'toothbrushes',  # too common across all products to discriminate
}

def _significant_words(title):
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    return [w for w in words if len(w) >= 4 and w not in STOPWORDS]

def title_matches(our_title, page_title, vendor=None, threshold=0.65):
    """
    True if the page title plausibly refers to the same product.
    Requires brand word AND ≥65% of significant words to be present.
    Also enforces vendor name even when it's too short to appear in sig (e.g. 'gum').
    """
    if not page_title:
        return False
    sig = _significant_words(our_title)
    if not sig:
        return False
    page_lower = page_title.lower()
    matched = [w for w in sig if w in page_lower]
    ratio = len(matched) / len(sig)
    # Hard rule: first significant word (usually brand) must be present
    if sig[0] not in page_lower:
        return False
    # Explicit vendor check for brand names that are short (< 4 chars, so excluded from sig)
    # e.g. vendor='gum' (3 chars) would not appear in sig but MUST appear in page title
    if vendor:
        vendor_words = [w for w in re.findall(r'[a-z0-9]+', vendor.lower()) if len(w) >= 3]
        if vendor_words and vendor_words[0] not in page_lower:
            return False
    return ratio >= threshold

# ── DDG text search ───────────────────────────────────────────────────────────

def ddg_text_search(query, num=5):
    """DDG text search — no quota. Returns list of {href, title, body}."""
    for attempt in range(3):
        try:
            results = list(DDGS(timeout=15).text(query, max_results=num))
            time.sleep(2)
            return results
        except Exception as e:
            if attempt < 2:
                time.sleep(10)
            else:
                print(f'     ⚠ DDG error: {e}')
    return []

# ── Page fetchers and parsers ─────────────────────────────────────────────────

def _fetch_html(url, timeout=20):
    try:
        r = requests.get(url, timeout=timeout, headers={
            'User-Agent': REQUESTS_UA,
            'Accept-Language': 'en-GB,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        })
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None

def _normalize_amazon_url(url):
    """Strip all Amazon CDN suffixes to get full-resolution image."""
    if url and ('media-amazon.com' in url or 'ssl-images-amazon.com' in url):
        # Extract base image ID and reconstruct clean URL
        # e.g. .../I/81XYZ._AC_SX679_PIbundle....jpg → .../I/81XYZ.jpg
        m = re.search(r'(/images/I/[A-Za-z0-9+]+)\.', url)
        if m:
            # Determine extension from original URL
            ext_match = re.search(r'\.(jpg|jpeg|png|webp)(?:[^a-z]|$)', url, re.IGNORECASE)
            ext = ext_match.group(1).lower() if ext_match else 'jpg'
            base = url.split('/images/I/')[0]
            img_id = m.group(1).split('/')[-1]
            url = f'https://m.media-amazon.com/images/I/{img_id}.{ext}'
    return url

def _extract_ean(text):
    m = re.search(r'(?:EAN|Barcode|GTIN|UPC)[^0-9]{0,20}(\d{8}|\d{12,14})', text, re.IGNORECASE)
    if m:
        n = m.group(1)
        if len(n) in (8, 12, 13, 14):
            return n
    return None

def parse_amazon(html, url):
    """Amazon.com product page → (title, image_url, ean)."""
    soup = BeautifulSoup(html, 'html.parser')
    title_el = soup.find(id='productTitle')
    page_title = title_el.get_text(strip=True) if title_el else ''

    image_url = None
    img_el = soup.find(id='landingImage') or soup.find(id='imgBlkFront')
    if img_el:
        dyn = img_el.get('data-a-dynamic-image', '')
        if dyn:
            try:
                img_map = json.loads(dyn)
                image_url = max(img_map, key=lambda u: img_map[u][0] * img_map[u][1])
            except Exception:
                pass
        if not image_url:
            image_url = img_el.get('src')
    if not image_url:
        m = re.search(r'"hiRes"\s*:\s*"(https://[^"]+)"', html)
        if m:
            image_url = m.group(1)

    if image_url:
        image_url = _normalize_amazon_url(image_url)

    ean = _extract_ean(soup.get_text())
    return page_title, image_url, ean

def parse_tepe(html, url):
    """tepe.com product page → (title, image_url, ean)."""
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    page_title = h1.get_text(strip=True) if h1 else ''

    image_url = None

    # TePe uses Next.js: <img src="/_next/image?url=https%3A%2F%2Fprod.tepe.com%2F...">
    for img in soup.find_all('img', src=re.compile(r'_next/image\?url=')):
        raw_src = img.get('src', '')
        qs = parse_qs(urlparse(raw_src).query)
        decoded = unquote(qs.get('url', [''])[0])
        if decoded and 'prod.tepe.com' in decoded:
            # Prefer product/globalassets images over icons/logos
            if any(k in decoded for k in ('globalassets', 'product', 'contentassets')):
                image_url = decoded
                break

    if not image_url:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict) and 'image' in data:
                    imgs = data['image']
                    image_url = imgs[0] if isinstance(imgs, list) else imgs
                    break
            except Exception:
                pass

    ean = _extract_ean(soup.get_text())
    return page_title, image_url, ean

def parse_sunstargum(html, url):
    """sunstargum.com product page → (title, image_url, ean)."""
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    page_title = h1.get_text(strip=True) if h1 else ''

    image_url = None

    # Sunstar uses Adobe Dynamic Media deliver URLs (may be relative paths)
    base_url = 'https://www.sunstargum.com'
    for img in soup.find_all('img'):
        src = img.get('src') or img.get('data-src', '')
        if 'dynamicmedia' in src or 'dam' in src:
            if src.startswith('/'):
                image_url = base_url + src
            else:
                image_url = src
            break

    # Fallback: og:image
    if not image_url:
        og = soup.find('meta', property='og:image')
        if og:
            image_url = og.get('content')

    # Fallback: JSON-LD
    if not image_url:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict) and 'image' in data:
                    imgs = data['image']
                    image_url = imgs[0] if isinstance(imgs, list) else imgs
                    break
            except Exception:
                pass

    ean = _extract_ean(soup.get_text())
    return page_title, image_url, ean

def parse_walgreens(html, url):
    """walgreens.com product page → (title, image_url, ean)."""
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    page_title = h1.get_text(strip=True) if h1 else ''

    image_url = None
    # Walgreens uses structured data and og:image
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        image_url = og['content']

    if not image_url:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict) and 'image' in data:
                    imgs = data['image']
                    image_url = imgs[0] if isinstance(imgs, list) else imgs
                    break
            except Exception:
                pass

    ean = _extract_ean(soup.get_text())
    return page_title, image_url, ean

def parse_generic(html, url):
    """Fallback parser."""
    soup = BeautifulSoup(html, 'html.parser')
    h1 = soup.find('h1')
    page_title = h1.get_text(strip=True) if h1 else ''
    og = soup.find('meta', property='og:image')
    image_url = og.get('content') if og else None
    ean = _extract_ean(soup.get_text())
    return page_title, image_url, ean

DOMAIN_PARSERS = {
    'amazon.com':     parse_amazon,
    'amazon.co.uk':   parse_amazon,
    'sunstargum.com': parse_sunstargum,
    'tepe.com':       parse_tepe,
    'walgreens.com':  parse_walgreens,
}

def _parser_for(url):
    for domain, fn in DOMAIN_PARSERS.items():
        if domain in url:
            return fn
    return parse_generic

TARGET_SIZE = 800   # final upload dimensions (square)

# ── Image download, validation, and normalization ────────────────────────────

def download_image(url):
    url = _normalize_amazon_url(url)
    try:
        r = requests.get(url, timeout=20, headers={'User-Agent': REQUESTS_UA})
        if r.status_code != 200 or not r.content:
            return None
        ct = r.headers.get('Content-Type', '')
        ext = url.lower().split('?')[0]
        if 'image' not in ct and not any(ext.endswith(e) for e in ('.jpg','.jpeg','.png','.webp')):
            return None
        return r.content
    except Exception:
        return None

def check_image(data):
    """Returns (ok, w, h, reason)."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
    except Exception as e:
        return False, 0, 0, f'Cannot open: {e}'
    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return False, w, h, f'Too small {w}x{h}'
    ratio = w / h
    if not (ASPECT_MIN <= ratio <= ASPECT_MAX):
        return False, w, h, f'Bad aspect {ratio:.2f}'
    return True, w, h, None

def normalize_to_square(img_bytes, size=TARGET_SIZE):
    """
    Fit image inside size×size on a white background (letterbox/pillarbox).
    Returns JPEG bytes at TARGET_SIZE × TARGET_SIZE.
    """
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')

    # Scale to fit within the target square, preserving aspect ratio
    ratio = min(size / img.width, size / img.height)
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Paste centred onto white canvas
    canvas = Image.new('RGB', (size, size), (255, 255, 255))
    offset = ((size - new_w) // 2, (size - new_h) // 2)
    canvas.paste(img, offset)

    buf = io.BytesIO()
    canvas.save(buf, format='JPEG', quality=92)
    return buf.getvalue()

# ── Core: try one product page ────────────────────────────────────────────────

def try_product_page(our_title, page_url, vendor=None, threshold=None):
    """
    Fetch a product page, verify title matches, extract image + EAN.
    Returns (img_bytes, img_url, ean, page_title) or None.
    """
    html = _fetch_html(page_url)
    if not html:
        print(f'     ✗ Could not fetch page')
        return None

    parser = _parser_for(page_url)
    page_title, image_url, ean = parser(html, page_url)

    short_title = page_title[:70] if page_title else '(no title)'
    kwargs = {'vendor': vendor}
    if threshold is not None:
        kwargs['threshold'] = threshold
    if not title_matches(our_title, page_title, **kwargs):
        print(f'     ✗ Title mismatch: "{short_title}"')
        return None

    if not image_url:
        print(f'     ✗ No image on page: "{short_title}"')
        return None

    img_bytes = download_image(image_url)
    if not img_bytes:
        print(f'     ✗ Image download failed: {image_url[:60]}')
        return None

    ok, w, h, reason = check_image(img_bytes)
    if not ok:
        print(f'     ✗ Image check failed ({reason}): "{short_title}"')
        return None

    print(f'     ✓ "{short_title}"')
    return img_bytes, image_url, ean, page_title

# ── Main image finder ─────────────────────────────────────────────────────────

def find_image(product):
    """
    Try Tier 1 (brand site) then Tier 2 (Amazon.com).
    Returns (img_bytes, img_url, ean) or None.
    """
    title        = product['title']
    vendor       = product['vendor']
    search_query = product.get('search_query', title)   # allow per-product query override

    # ── Tier 1 — brand site (trusted source: lower threshold) ─────────────────
    for domain in TIER1_DOMAINS.get(vendor, []):
        print(f'   [Tier 1] site:{domain}')
        results = ddg_text_search(f'site:{domain} {search_query}', num=3)
        for r in results:
            url = r.get('href', '')
            if domain not in url:           # skip ad redirects / off-domain results
                continue
            print(f'     → {url[:80]}')
            result = try_product_page(title, url, vendor=vendor, threshold=0.50)
            if result:
                return result[:3]

    # ── Tier 2: Amazon.com (less trusted: stricter threshold) ─────────────────
    print(f'   [Tier 2] Amazon.com')
    results = ddg_text_search(f'site:amazon.com {search_query}', num=6)
    for r in results:
        url = r.get('href', '')
        if '/dp/' not in url:
            continue
        print(f'     → {url[:80]}')
        result = try_product_page(title, url, vendor=vendor, threshold=0.65)
        if result:
            return result[:3]

    return None

# ── Shopify helpers ───────────────────────────────────────────────────────────

_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token: return _shopify_token
    r = requests.post(f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token', json={
        'client_id': SHOPIFY_CLIENT_ID,
        'client_secret': SHOPIFY_CLIENT_SEC,
        'grant_type': 'client_credentials',
    }, timeout=10)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_token = data['access_token']
    return _shopify_token

def shopify_headers():
    return {'X-Shopify-Access-Token': get_shopify_token(), 'Content-Type': 'application/json'}

def shopify_get(path):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.get(url, headers=shopify_headers(), timeout=20)
    if r.status_code == 429: time.sleep(10); return shopify_get(path)
    return r.json()

def shopify_post(path, payload):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VER}/{path}'
    r = requests.post(url, headers=shopify_headers(), json=payload, timeout=30)
    if r.status_code == 429: time.sleep(10); return shopify_post(path, payload)
    return r.json()

def get_product_id(handle):
    data = shopify_get(f'products.json?handle={handle}&fields=id,title')
    products = data.get('products', [])
    return (products[0]['id'], products[0]['title']) if products else (None, None)

def upload_image(product_id, img_bytes, filename):
    # Always normalize to TARGET_SIZE × TARGET_SIZE on white background
    img_bytes = normalize_to_square(img_bytes)
    encoded = base64.b64encode(img_bytes).decode()
    data = shopify_post(f'products/{product_id}/images.json', {
        'image': {'attachment': encoded, 'filename': filename}
    })
    img = data.get('image', {})
    return img.get('src'), data.get('errors')

# ── Zoho helpers ──────────────────────────────────────────────────────────────

_zoho_token = None

def get_zoho_token():
    global _zoho_token
    if _zoho_token: return _zoho_token
    r = requests.post('https://accounts.zoho.com/oauth/v2/token', params={
        'refresh_token': ZOHO_REFRESH_TOKEN,
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SEC,
        'grant_type': 'refresh_token',
    }, timeout=15)
    data = r.json()
    if 'access_token' not in data:
        raise Exception(f'Zoho auth failed: {data}')
    _zoho_token = data['access_token']
    return _zoho_token

def zoho_headers():
    return {'Authorization': f'Zoho-oauthtoken {get_zoho_token()}'}

def find_zoho_item(name):
    r = requests.get('https://inventory.zoho.com/api/v1/items', params={
        'organization_id': ZOHO_ORG_ID, 'search_text': name, 'per_page': 5,
    }, headers=zoho_headers(), timeout=15)
    items = r.json().get('items', [])
    for item in items:
        if item.get('name', '').lower() == name.lower():
            return item.get('item_id')
    return items[0].get('item_id') if items else None

def update_zoho_ean(item_id, ean):
    r = requests.put(
        f'https://inventory.zoho.com/api/v1/items/{item_id}',
        params={'organization_id': ZOHO_ORG_ID},
        json={'ean': ean},
        headers={**zoho_headers(), 'Content-Type': 'application/json'},
        timeout=15,
    )
    return r.json().get('code') == 0

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--handle', metavar='HANDLE')
    args = parser.parse_args()

    today = date.today().isoformat()
    batch = [p for p in BATCH if not args.handle or p['handle'] == args.handle]

    print(f'\n🦷 Consumer Image Fix v2 — {today}')
    print(f'   Products: {len(batch)}  |  Dry run: {args.dry_run}')
    print(f'   Store: {SHOPIFY_SHOP_URL}\n')

    results = []
    fixed, no_image, errors = [], [], []

    for i, product in enumerate(batch, 1):
        handle = product['handle']
        title  = product['title']
        vendor = product['vendor']

        print(f'[{i:02}/{len(batch)}] {title}  [{vendor}]')

        product_id, shopify_title = get_product_id(handle)
        if not product_id:
            print(f'  ✗ Not found on Shopify\n')
            errors.append({'handle': handle, 'reason': 'Not found on Shopify'})
            results.append({'handle': handle, 'status': 'error', 'reason': 'not found'})
            continue

        result = find_image(product)

        if not result:
            print(f'  ✗ No suitable image found\n')
            no_image.append(title)
            results.append({'handle': handle, 'status': 'no_image'})
            continue

        img_bytes, img_url, ean = result
        w, h = Image.open(io.BytesIO(img_bytes)).size
        print(f'  ✓ {w}x{h}px | {img_url[:80]}')
        if ean:
            print(f'  EAN: {ean}')

        if args.dry_run:
            print(f'  ↷ DRY RUN\n')
            fixed.append(title)
            results.append({'handle': handle, 'status': 'dry_run', 'image_url': img_url, 'ean': ean})
            continue

        src, upload_errors = upload_image(product_id, img_bytes, f'{handle}.jpg')
        if upload_errors or not src:
            print(f'  ✗ Upload failed: {upload_errors}\n')
            errors.append({'handle': handle, 'reason': f'upload failed'})
            results.append({'handle': handle, 'status': 'error', 'reason': 'upload failed'})
            continue

        print(f'  ✅ Uploaded → {src[:75]}')

        if ean:
            try:
                item_id = find_zoho_item(shopify_title)
                if item_id:
                    ok = update_zoho_ean(item_id, ean)
                    print(f'  {"✓" if ok else "⚠"} Zoho EAN {"saved" if ok else "write failed"}: {ean}')
                else:
                    print(f'  ⚠ Zoho item not found — EAN {ean} not saved')
            except Exception as e:
                print(f'  ⚠ Zoho EAN error: {e}')

        fixed.append(title)
        results.append({'handle': handle, 'status': 'fixed', 'image_url': src, 'ean': ean})
        print()
        time.sleep(1)

    print('=' * 60)
    print(f'SUMMARY ({today}):')
    print(f'  ✅ Fixed:     {len(fixed)}')
    print(f'  ✗  No image: {len(no_image)}')
    print(f'  ✗  Errors:   {len(errors)}')
    if no_image:
        print(f'\nNeeds manual image sourcing:')
        for t in no_image: print(f'  - {t}')
    if errors:
        print(f'\nErrors:')
        for e in errors: print(f'  - {e["handle"]}: {e.get("reason","")}')

    log_path = f'fix_consumer_images_v2_{today}.json'
    with open(log_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'\nLog saved → {log_path}')

if __name__ == '__main__':
    main()
