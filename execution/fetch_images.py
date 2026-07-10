#!/usr/bin/env python3
"""
Stage 3b — Auto Image Fetcher
Finds product images via DuckDuckGo for items in 'Image required' status.
Downloads candidates, validates with Pillow, uploads to Zoho.

New in v2:
  - Uses enriched_title + variant values for better search queries
  - Manufacturer site-first search (bicon.com, waterpik.com, etc.)
  - Cross-variant duplicate detection: forces variant-specific queries when
    the same image hash is found twice in the same collection
  - --recheck-published: audits all live Shopify products, requeues items with
    low-res or duplicate images, then auto-fetches replacements

Validation thresholds (same as Stage 3 validate_images.py):
    Aspect ratio : 0.8 – 1.2
    Min resolution: 800 x 800px

Usage:
    python execution/fetch_images.py                    # process Image-required items
    python execution/fetch_images.py --dry-run
    python execution/fetch_images.py --sku 320-150-001
    python execution/fetch_images.py --recheck-published          # audit + fix live Shopify
    python execution/fetch_images.py --recheck-published --dry-run
"""

import os
import sys
import io
import re
import hashlib
import json
import time
import argparse
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

try:
    from PIL import Image
except ImportError:
    print('✗ Pillow not installed. Run: python3 -m pip install Pillow')
    sys.exit(1)

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        print('✗ ddgs not installed. Run: python3 -m pip install ddgs')
        sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_ORG_ID        = os.getenv('ZOHO_ORGANIZATION_ID')
ZOHO_CLIENT_ID     = os.getenv('ZOHO_CLIENT_ID')
ZOHO_CLIENT_SECRET = os.getenv('ZOHO_CLIENT_SECRET')
ZOHO_REFRESH_TOKEN = os.getenv('ZOHO_REFRESH_TOKEN')
ZOHO_API_BASE      = 'https://www.zohoapis.com/inventory/v1'
TOKEN_FILE         = '.zoho_token.json'

SHOPIFY_SHOP_URL      = os.getenv('SHOPIFY_SHOP_URL')
SHOPIFY_CLIENT_ID     = os.getenv('SHOPIFY_CLIENT_ID')
SHOPIFY_CLIENT_SECRET = os.getenv('SHOPIFY_CLIENT_SECRET')
SHOPIFY_API_VERSION   = '2025-01'

ENRICHMENT_INPUT  = 'enrichment_input.json'
ENRICHMENT_OUTPUT = 'enrichment_output.json'

ASPECT_MIN     = 0.8
ASPECT_MAX     = 1.2
MIN_WIDTH      = 800
MIN_HEIGHT     = 800
MAX_CANDIDATES = 5      # DDG results to try per query

# ── Brand safety ──────────────────────────────────────────────────────────────

COMPETITOR_DOMAINS = {
    'straumann', 'nobelbiocare', 'nobel-biocare', 'zimmer', 'biomet',
    'zimmerbiomet', 'dentsply', 'sirona', 'osstem', 'megagen',
    'astratech', 'neodent', 'bredent', 'anthogyr', 'mis-implants',
    'biohorizons', 'keystone-dental', 'hiossen', 'dentiumusa',
    'ditabrasil', 'cortex-dental', 'alfa-gate', 'trinon',
}

# Trusted manufacturer domains — searched FIRST via site: operator
BRAND_TRUSTED_DOMAINS = {
    'bicon':     ['bicon.com', 'store.bicon.com'],
    'medesy':    ['medesy.it', 'medesy.com'],
    'geistlich': ['geistlich.com', 'geistlich-pharma.com'],
    'ethoss':    ['ethoss.net'],
    'komet':     ['komet.de', 'kometdental.com'],
    'coltene':   ['coltene.com'],
    'unodent':   ['unodent.com'],
    'waterpik':  ['waterpik.com'],
    'crosstex':  ['crosstex.com'],
    'beesure':   ['beesure.com', 'be-esure.com'],
    'miltex':    ['miltex.com'],
    'roeko':     ['roeko.com', 'coltene.com'],
    # Clinical bur brands — added in Batch B (2026-03-29)
    'diatech':   ['coltene.com', 'diatechdentalusa.com'],  # Diatech is a Coltene brand
    'edenta':    ['edenta.ch', 'edenta.com'],
    'hi-di':     ['hi-di.com', 'hidi.com'],
    'oral-b':    ['oral-b.com', 'oralb.com'],
    'io':        ['oral-b.com', 'oralb.com'],
    'stages':    ['oral-b.com', 'oralb.com'],
    'tepe':      ['tepe.com'],
    'colgate':   ['colgate.com'],
    'reach':     ['reachtoothbrush.com'],
    'gum':       ['sunstargum.com', 'gumbrand.com'],
}

def is_competitor_url(url):
    url_lower = url.lower()
    return any(c in url_lower for c in COMPETITOR_DOMAINS)

def is_trusted_url(url, brand):
    domains = BRAND_TRUSTED_DOMAINS.get(brand.lower(), [])
    url_lower = url.lower()
    return any(d in url_lower for d in domains)

def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

# ── Zoho Auth ─────────────────────────────────────────────────────────────────

def get_zoho_token():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                t = json.load(f)
                if time.time() - t.get('timestamp', 0) < 3300:
                    return t['access_token']
        except Exception:
            pass
    resp = requests.post(
        'https://accounts.zoho.com/oauth/v2/token',
        params={
            'refresh_token': ZOHO_REFRESH_TOKEN,
            'client_id':     ZOHO_CLIENT_ID,
            'client_secret': ZOHO_CLIENT_SECRET,
            'grant_type':    'refresh_token',
        }, timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Zoho auth failed: {data}')
    token = data['access_token']
    with open(TOKEN_FILE, 'w') as f:
        json.dump({'access_token': token, 'timestamp': time.time()}, f)
    return token

def zoho_headers(token):
    return {'Authorization': f'Zoho-oauthtoken {token}'}

# ── Shopify Auth ──────────────────────────────────────────────────────────────

_shopify_token = None

def get_shopify_token():
    global _shopify_token
    if _shopify_token:
        return _shopify_token
    if not SHOPIFY_SHOP_URL or not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise Exception('SHOPIFY_SHOP_URL / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET not set in .env')
    resp = requests.post(
        f'https://{SHOPIFY_SHOP_URL}/admin/oauth/access_token',
        json={'client_id': SHOPIFY_CLIENT_ID, 'client_secret': SHOPIFY_CLIENT_SECRET,
              'grant_type': 'client_credentials'},
        timeout=10
    )
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Shopify auth failed: {data}')
    _shopify_token = data['access_token']
    return _shopify_token

def shopify_graphql(query, variables=None):
    url = f'https://{SHOPIFY_SHOP_URL}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    headers = {
        'X-Shopify-Access-Token': get_shopify_token(),
        'Content-Type': 'application/json',
    }
    for attempt in range(3):
        resp = requests.post(url, headers=headers,
                             json={'query': query, 'variables': variables or {}},
                             timeout=30)
        if resp.status_code == 429:
            print('  [RATE LIMIT] Waiting 10s...')
            time.sleep(10)
            continue
        return resp.json()
    raise Exception('Shopify GraphQL: too many retries')

# ── Zoho reads ────────────────────────────────────────────────────────────────

def get_item_detail(item_id, token):
    resp = requests.get(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers=zoho_headers(token),
        params={'organization_id': ZOHO_ORG_ID},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return get_item_detail(item_id, token)
    item = resp.json().get('item', {})
    cfs  = {cf['api_name']: cf.get('value') for cf in item.get('custom_fields', [])}
    return cfs.get('cf_shopify_status', ''), cfs.get('cf_source_url', '')

# ── Improved search query builder (v2) ────────────────────────────────────────

_DIMENSION_RE = re.compile(r'^\d+(\.\d+)?\s*(MM|CM|M)\b', re.IGNORECASE)

_DASH_RE  = re.compile(r'[—–]')
_NOISE_RE = re.compile(r'[#"()″′]')

def _clean_search_text(text, brand=''):
    """
    Normalize product text for search engines:
    strip em-dashes, #, parens/quotes; drop a leading brand prefix
    (the brand is added to the query separately, exactly once).
    """
    t = _DASH_RE.sub(' ', text or '')
    t = _NOISE_RE.sub(' ', t)
    t = ' '.join(t.split())
    if brand and t.lower().startswith(brand.lower()):
        t = t[len(brand):].strip(' -—–')
    return t.strip()

def build_queries_v2(name, brand, category, enriched_title='', v1_name='', v1_value='',
                     forced_specific=False):
    """
    Return a prioritised list of clean query strings.

    Priority order:
      1. site:{trusted_domain} {core}     ← manufacturer site first
      2. {brand} {core}                   ← brand exactly once, cleaned title
      3. {brand} {core} {variant value}   ← variant-specific
      4. {core} dental                    ← brandless fallback

    Rules learned the hard way (2026-07-10 audit: 33/42 wrong images):
    - no em-dashes, no '#', no quoted phrases — they kill or distort results
    - never duplicate the brand ("Cattani Cattani ...")
    - never append raw category strings ("Dental Units & Accessories")
    - no generic brand-only fallback — it fetches the wrong product entirely
    """
    brand = (brand or '').strip()

    # Clean up the raw name for fallback use
    clean = name.replace('-', ' ').replace('*', '').strip()
    dim_match = _DIMENSION_RE.match(clean)
    if dim_match:
        dim = dim_match.group(0).strip()
        rest = clean[dim_match.end():].strip()
        natural = f'{rest} {dim}'.strip()
    else:
        natural = clean

    core = _clean_search_text(enriched_title, brand) or _clean_search_text(natural.title(), brand)
    v1_clean = _clean_search_text(v1_value, '') if v1_value else ''

    queries = []

    if not forced_specific:
        # 1. Manufacturer site searches (highest precision)
        trusted_domains = BRAND_TRUSTED_DOMAINS.get(brand.lower(), []) if brand else []
        for domain in trusted_domains[:2]:
            queries.append(f'site:{domain} {core}')

        # 2. Brand + cleaned core title (brand exactly once)
        if brand:
            queries.append(f'{brand} {core}')

    # 3. Variant-specific — append only the variant tokens not already in the title
    if v1_clean:
        core_words = set(core.lower().split())
        extra = ' '.join(w for w in v1_clean.split() if w.lower() not in core_words)
        if extra:
            queries.append(f'{brand} {core} {extra}'.strip())

    # 4. Brandless fallback with a dental qualifier — still product-specific
    queries.append(f'{core} dental')

    # De-dup while preserving order
    seen = set()
    return [q for q in queries if not (q in seen or seen.add(q))]

# ── Google Custom Search (primary — free tier, 100 queries/day) ──────────────

CSE_KEY         = os.getenv('SEARCH_API_KEY')
CSE_CX          = os.getenv('GOOGLE_CX')
CSE_QUOTA_FILE  = '.cse_quota.json'
CSE_DAILY_LIMIT = 95   # stay safely under the 100/day free tier

def _cse_quota():
    """Return (used_today, limit). Resets automatically each day."""
    today = date.today().isoformat()
    try:
        with open(CSE_QUOTA_FILE) as f:
            q = json.load(f)
        if q.get('date') == today:
            return q.get('used', 0), CSE_DAILY_LIMIT
    except Exception:
        pass
    return 0, CSE_DAILY_LIMIT

def _cse_quota_bump():
    today = date.today().isoformat()
    used, _ = _cse_quota()
    with open(CSE_QUOTA_FILE, 'w') as f:
        json.dump({'date': today, 'used': used + 1}, f)

def search_images_google(query, max_results=MAX_CANDIDATES):
    """
    Google Custom Search image results. Returns a list of candidate dicts,
    or None if CSE is unavailable (no key, quota exhausted, or API error) —
    None signals the caller to fall back to DDG.
    """
    if not CSE_KEY or not CSE_CX:
        return None
    used, limit = _cse_quota()
    if used >= limit:
        return None
    try:
        # One CSE call costs 1 quota unit whether we ask for 1 or 10 results —
        # always ask for 10. 'large' pre-filters tiny thumbnails server-side.
        resp = requests.get('https://www.googleapis.com/customsearch/v1', params={
            'key': CSE_KEY, 'cx': CSE_CX, 'q': query,
            'searchType': 'image', 'num': 10, 'imgSize': 'large',
        }, timeout=15)
        _cse_quota_bump()
        data = resp.json()
        if 'error' in data:
            msg = data['error'].get('message', '')
            if 'quota' in msg.lower() or resp.status_code == 429:
                # Mark quota exhausted for the rest of the day
                with open(CSE_QUOTA_FILE, 'w') as f:
                    json.dump({'date': date.today().isoformat(), 'used': CSE_DAILY_LIMIT}, f)
            else:
                print(f'     ⚠ CSE error: {msg[:80]}')
            return None
        # Keep title/snippet/context — they carry the model/shade/size tokens
        # that candidate scoring uses to pick the RIGHT variant.
        return [{'url': it.get('link', ''),
                 'width':  int(it.get('image', {}).get('width', 0) or 0),
                 'height': int(it.get('image', {}).get('height', 0) or 0),
                 'title':   it.get('title', ''),
                 'snippet': it.get('snippet', ''),
                 'context': it.get('image', {}).get('contextLink', '')}
                for it in data.get('items', []) if it.get('link')]
    except Exception as e:
        print(f'     ⚠ CSE request failed: {e}')
        return None

def search_images(query, max_results=MAX_CANDIDATES):
    """Unified search: Google CSE first (fast, reliable, free tier), DDG fallback.
    Falls back to DDG when CSE is unavailable OR returned zero results
    (the quota unit is already spent — don't waste the query entirely)."""
    results = search_images_google(query, max_results)
    if results:
        return results, 'cse'
    return search_images_ddg(query, max_results), 'ddg'

# ── DuckDuckGo image search (fallback) ────────────────────────────────────────

def search_images_ddg(query, max_results=MAX_CANDIDATES, _retries=3):
    for attempt in range(_retries + 1):
        try:
            results = list(DDGS(timeout=12).images(query, max_results=max_results))
            time.sleep(5)  # cool-down after successful query
            return [{'url': r.get('image', ''), 'width': r.get('width', 0), 'height': r.get('height', 0)}
                    for r in results if r.get('image')]
        except Exception as e:
            msg = str(e)
            if 'Ratelimit' in msg or '429' in msg or '202' in msg:
                wait = 60 * (attempt + 1)
                print(f'     ⚠ DDG rate limited — waiting {wait}s (attempt {attempt+1}/{_retries+1})...')
                time.sleep(wait)
            else:
                print(f'     ⚠ DDG search error: {e}')
                return []
    return []

# ── SKU-level manual image URL overrides ─────────────────────────────────────
# Key = Zoho SKU, Value = direct image URL.
# Used when automated search reliably fails (niche surgical items, geo-blocked
# manufacturer sites, etc.). These are tried FIRST before any DDG queries.
# Add entries here after confirming the URL returns an image that passes
# the Pillow aspect-ratio and resolution checks.

MANUAL_OVERRIDES: dict = {
    # Example template — add confirmed URLs as they are discovered:
    # '320-150-008': 'https://store.bicon.com/product/image/large/260-101-xxx_1.jpg',
}

# ── Feedback / continuous-improvement hints ───────────────────────────────────
# Loaded from feedback/image_hints.json at startup.
# Edit that file — never edit these globals directly.

_FEEDBACK_DIR       = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'feedback')
_IMAGE_HINTS_FILE   = os.path.join(_FEEDBACK_DIR, 'image_hints.json')

MANUAL_ONLY_SKUS:        set = set()   # SKUs that must be imaged manually — skip auto-fetch
MANUAL_ONLY_COLLECTIONS: set = set()   # Collections where every item is manual-only


def load_image_hints():
    """
    Merge feedback/image_hints.json into the live config dicts at startup.

    Merges:
      brand_preferred_sources    → BRAND_TRUSTED_DOMAINS
      globally_blocked_domains   → COMPETITOR_DOMAINS
      sku_image_overrides        → MANUAL_OVERRIDES
      manual_only_skus           → MANUAL_ONLY_SKUS
      manual_only_collections    → MANUAL_ONLY_COLLECTIONS
    """
    global MANUAL_ONLY_SKUS, MANUAL_ONLY_COLLECTIONS

    if not os.path.exists(_IMAGE_HINTS_FILE):
        return

    try:
        with open(_IMAGE_HINTS_FILE) as f:
            hints = json.load(f)
    except Exception as e:
        print(f'  ⚠  Could not load image hints: {e}')
        return

    # Brand preferred sources — merge into BRAND_TRUSTED_DOMAINS
    for brand, domains in hints.get('brand_preferred_sources', {}).items():
        if brand.startswith('_'):
            continue
        key = brand.lower()
        if key not in BRAND_TRUSTED_DOMAINS:
            BRAND_TRUSTED_DOMAINS[key] = []
        for d in domains:
            if d not in BRAND_TRUSTED_DOMAINS[key]:
                BRAND_TRUSTED_DOMAINS[key].append(d)

    # Globally blocked domains — add to COMPETITOR_DOMAINS
    for domain in hints.get('globally_blocked_domains', []):
        if not domain.startswith('_'):
            COMPETITOR_DOMAINS.add(domain)

    # SKU-level manual overrides
    for sku, url in hints.get('sku_image_overrides', {}).items():
        if not sku.startswith('_'):
            MANUAL_OVERRIDES[sku] = url

    # Manual-only SKUs and collections (skip auto-fetch entirely)
    MANUAL_ONLY_SKUS = {s for s in hints.get('manual_only_skus', []) if not s.startswith('_')}
    MANUAL_ONLY_COLLECTIONS = {c for c in hints.get('manual_only_collections', []) if not c.startswith('_')}

    print(f'  ✓ image_hints.json loaded — '
          f'{len(MANUAL_ONLY_SKUS)} manual-only SKUs, '
          f'{len(MANUAL_ONLY_COLLECTIONS)} manual-only collection(s), '
          f'{len(COMPETITOR_DOMAINS)} blocked domain(s)')


# ── Image download + validation ───────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """Strip Amazon CDN resolution/quality suffixes to get the full-size image.

    e.g. https://m.media-amazon.com/images/I/61XYZ._AC_UF350,350_QL50_.jpg
      →  https://m.media-amazon.com/images/I/61XYZ.jpg
    """
    import re as _re
    if 'm.media-amazon.com' in url or 'images-na.ssl-images-amazon.com' in url:
        url = _re.sub(r'\._[A-Z0-9_,]+_(\.[a-zA-Z]+)$', r'\1', url)
    return url

def download_image(url):
    url = _normalize_url(url)
    try:
        resp = requests.get(url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        if resp.status_code != 200 or not resp.content:
            return None
        ct = resp.headers.get('Content-Type', '')
        ext = url.lower().split('?')[0]
        if 'image' not in ct and not any(ext.endswith(e) for e in ('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            return None
        return resp.content
    except Exception:
        return None

def check_image(image_bytes):
    """Returns (passed, width, height, ratio, fail_reason, pil_img)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        w, h = img.size
    except Exception as e:
        return False, 0, 0, 0.0, f'Cannot open: {e}', None

    ratio = round(w / h, 2) if h > 0 else 0.0

    if ratio < ASPECT_MIN or ratio > ASPECT_MAX:
        return False, w, h, ratio, f'Aspect ratio {ratio:.2f} (need 0.8–1.2)', None

    if w < MIN_WIDTH or h < MIN_HEIGHT:
        return False, w, h, ratio, f'Too small ({w}x{h}px, need 800x800px)', img

    return True, w, h, ratio, None, img

# ── Zoho image upload ─────────────────────────────────────────────────────────

def upload_image_to_zoho(item_id, image_bytes, filename, token):
    try:
        resp = requests.post(
            f'{ZOHO_API_BASE}/items/{item_id}/image',
            headers=zoho_headers(token),
            params={'organization_id': ZOHO_ORG_ID},
            files={'image': (filename, image_bytes, 'image/jpeg')},
            timeout=30,
        )
    except requests.RequestException as e:
        return False, str(e)

    if resp.status_code == 429:
        time.sleep(60)
        return upload_image_to_zoho(item_id, image_bytes, filename, token)

    try:
        data = resp.json()
        return data.get('code') == 0, data.get('message', f'HTTP {resp.status_code}')
    except Exception:
        return resp.status_code == 200, f'HTTP {resp.status_code}'

def write_zoho_status(item_id, new_status, sync_result, notes, source_url, token):
    custom_fields = [
        {'api_name': 'cf_shopify_status',     'value': new_status},
        {'api_name': 'cf_sync_result',        'value': sync_result},
        {'api_name': 'cf_shopify_sync_notes', 'value': notes},
    ]
    if source_url:
        custom_fields.append({'api_name': 'cf_source_url', 'value': source_url})

    resp = requests.put(
        f'{ZOHO_API_BASE}/items/{item_id}',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID},
        json={'custom_fields': custom_fields},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return write_zoho_status(item_id, new_status, sync_result, notes, source_url, token)
    data = resp.json()
    return data.get('code') == 0, data.get('message', '')

# ── Candidate filtering ───────────────────────────────────────────────────────

def _variant_tokens(value):
    """Tokenize a variant value for matching: 'Turbo Jet 2 (230V 50Hz)' → {'turbo','jet','2','230v','50hz'}."""
    return {t for t in re.split(r'[^a-z0-9]+', (value or '').lower()) if t}

def _token_hits(tokens, text):
    """Count word-boundary token matches in text (loose substrings would over-match single digits)."""
    return sum(1 for t in tokens if re.search(rf'\b{re.escape(t)}\b', text))

def try_candidates(candidates, brand='', exclude_hashes=None,
                   required_tokens=None, forbidden_tokens=None, tried_urls=None):
    """
    Try each candidate URL. Returns (image_bytes, pil_img, w, h, ratio, url, md5) or None.
    - Rejects competitor URLs.
    - Rejects images whose MD5 is in exclude_hashes (already used in same collection).
    - Scores candidates by variant-distinguishing tokens (model number, shade,
      size, colour) found in the URL + CSE title/snippet/context. A candidate
      matching a SIBLING variant's distinctive token and none of this item's is
      rejected outright — this is what prevented-class failures look like:
      Turbo Jet 1 getting Jet 2's render, shade A1 getting an A3 image.
    - Prioritises score, then trusted brand domains.
    - tried_urls (a set, mutated in place) prevents re-downloading candidates
      already attempted in an earlier pass for the same item.
    """
    exclude_hashes = exclude_hashes or set()
    required_tokens = required_tokens or set()
    forbidden_tokens = forbidden_tokens or set()
    tried_urls = tried_urls if tried_urls is not None else set()

    def cand_text(c):
        return ' '.join([c.get('url', ''), c.get('title', ''),
                         c.get('snippet', ''), c.get('context', '')]).lower()

    def score(c):
        s = 0
        text = cand_text(c)
        s += 3 * _token_hits(required_tokens, text)
        s -= 4 * _token_hits(forbidden_tokens, text)
        if brand and is_trusted_url(c.get('url', ''), brand):
            s += 1
        return s

    for cand in sorted(candidates, key=lambda c: (-score(c),)):
        url = cand.get('url', '')
        if not url or url in tried_urls:
            continue
        tried_urls.add(url)
        if is_competitor_url(url):
            print(f'     ✗ Blocked competitor URL: {url[:60]}')
            continue

        # Wrong-variant guard: matches a sibling's distinctive token but none of ours
        if forbidden_tokens and _token_hits(forbidden_tokens, cand_text(cand)) \
                and required_tokens and not _token_hits(required_tokens, cand_text(cand)):
            print(f'     ✗ Wrong-variant signal, skipping: {url[:60]}')
            continue

        meta_w = cand.get('width', 0)
        meta_h = cand.get('height', 0)
        if meta_w and meta_h and (meta_w < 200 or meta_h < 200):
            continue

        image_bytes = download_image(url)
        if not image_bytes:
            continue

        img_md5 = md5_of(image_bytes)
        if img_md5 in exclude_hashes:
            print(f'     ⚠ Skipping duplicate image (already used in this collection)')
            continue

        passed, w, h, ratio, fail_reason, img = check_image(image_bytes)
        if passed:
            return image_bytes, img, w, h, ratio, url, img_md5

        # Auto-upscale: good aspect ratio but undersized
        if img is not None and 'Too small' in (fail_reason or ''):
            from image_processing import auto_upscale
            result = auto_upscale(image_bytes)
            if result:
                upscaled_bytes, uw, uh = result
                upscaled_md5 = md5_of(upscaled_bytes)
                if upscaled_md5 not in exclude_hashes:
                    upscaled_img = Image.open(io.BytesIO(upscaled_bytes)).convert('RGB')
                    print(f'     ↑ Upscaled from {w}x{h} to {uw}x{uh}')
                    return upscaled_bytes, upscaled_img, uw, uh, round(uw/uh, 2), url, upscaled_md5

    return None

# ── Per-item processing ───────────────────────────────────────────────────────

def process_item(item, token, dry_run, today_str, output_by_sku, collection_hashes):
    """
    Process one item: search, validate, upload, write status.
    collection_hashes is mutated in-place: {collection_name: set(md5s)}
    Returns one of: 'validated', 'would_validate', 'no_image', 'skipped', 'error'
    """
    item_id  = item['item_id']
    sku      = item.get('sku', '')
    name     = item.get('name', '')
    brand    = item.get('brand', '')
    category = item.get('category', '')

    # Infer Bicon brand from SKU prefix
    if not brand and sku.startswith('320-'):
        brand = 'Bicon'

    # Pull enriched data for better queries
    enriched = output_by_sku.get(sku, {})
    enriched_title = enriched.get('enriched_title', '')
    v1_name        = enriched.get('variant_1_name', '') or ''
    v1_value       = enriched.get('variant_1_value', '') or ''
    collection     = enriched.get('shopify_collection', '') or ''

    # Check current Zoho status
    status, source_url = get_item_detail(item_id, token)
    if status != 'Image required':
        print(f'  ⬜ {sku}  {name[:45]}  [{status}]')
        return 'skipped'

    # Manual-only gate — skip auto-fetch for items/collections flagged in image_hints.json
    if sku in MANUAL_ONLY_SKUS:
        print(f'  📌 {sku}  {name[:45]}  [Manual only — skipping auto-fetch]')
        return 'skipped'
    if collection and collection in MANUAL_ONLY_COLLECTIONS:
        print(f'  📌 {sku}  {name[:45]}  [Collection "{collection}" is manual-only — skipping]')
        return 'skipped'

    print(f'\n  🔍 {sku}  {name[:45]}')
    if enriched_title:
        print(f'     Enriched title: "{enriched_title}"')
    if collection:
        already_used = len(collection_hashes.get(collection, set()))
        print(f'     Collection: "{collection}" ({already_used} image(s) already locked in)')

    # Existing collection hashes to avoid duplicates
    col_hashes = collection_hashes.get(collection, set()) if collection else set()

    # Variant-distinguishing tokens: this item's variant value vs its siblings'.
    # required = tokens unique to THIS variant; forbidden = tokens unique to siblings.
    # These drive candidate scoring so 'Turbo Jet 1' can never accept Jet 2's image.
    own_tokens = _variant_tokens(v1_value)
    sibling_tokens = set()
    if collection:
        for o in output_by_sku.values():
            if o.get('shopify_collection') == collection and o.get('sku') != sku:
                sibling_tokens |= _variant_tokens(o.get('variant_1_value', ''))
    required_tokens = own_tokens - sibling_tokens
    forbidden_tokens = sibling_tokens - own_tokens
    if required_tokens:
        print(f'     Variant tokens: need {sorted(required_tokens)}, avoid {sorted(forbidden_tokens)}')

    tried_urls = set()   # never re-download the same candidate for this item
    all_candidates = []  # accumulate across queries; scoring decides order

    def _try(cands, hashes):
        return try_candidates(cands, brand=brand, exclude_hashes=hashes,
                              required_tokens=required_tokens,
                              forbidden_tokens=forbidden_tokens,
                              tried_urls=tried_urls)

    hit = None

    # Priority 0: manual override / saved source URL — tried ALONE, before
    # any search query is spent (a confirmed URL must not lose to a search hit).
    if sku in MANUAL_OVERRIDES:
        override_url = MANUAL_OVERRIDES[sku]
        print(f'     📌 Manual override: {override_url[:80]}')
        hit = _try([{'url': override_url, 'width': 0, 'height': 0}], col_hashes)
    elif source_url:
        if is_competitor_url(source_url):
            print(f'     ⚠  Saved source URL is from a blocked domain — skipping cached URL.')
        else:
            print(f'     Trying saved source URL...')
            hit = _try([{'url': source_url, 'width': 0, 'height': 0}], col_hashes)

    if not hit:
        queries = build_queries_v2(
            name, brand, category,
            enriched_title=enriched_title,
            v1_name=v1_name,
            v1_value=v1_value,
        )
        for query in queries:
            print(f'     Searching: "{query}"')
            results, engine = search_images(query, max_results=MAX_CANDIDATES)
            if engine == 'cse' and results:
                print(f'     [CSE] {len(results)} result(s)')
            all_candidates.extend(results)

            hit = _try(all_candidates, col_hashes)
            if hit:
                break

            # DDG needs a cool-down between queries; CSE does not.
            if engine == 'ddg':
                time.sleep(15)

    if not hit and all_candidates:
        # Last resort: allow a within-collection duplicate from ALREADY-FETCHED
        # candidates (no new searches — the old triple-search here was the main
        # quota drain), but warn loudly.
        tried_urls.clear()
        hit = _try(all_candidates, set())  # no hash exclusion
        is_dupe = True if hit else False
    else:
        is_dupe = False

    if not hit:
        print(f'     ✗ No valid image found')
        return 'no_image'

    image_bytes, img, w, h, ratio, url, img_md5 = hit
    dupe_flag = ' [DUPE WARNING]' if is_dupe else ''
    print(f'     ✓ Valid{dupe_flag}: {w}x{h}px, ratio {ratio:.2f}')
    print(f'       {url[:75]}')

    if dry_run:
        return 'would_validate'

    # Upload — keep PNG for upscaled images, convert others to JPEG
    if image_bytes[:4] == b'\x89PNG':
        upload_bytes = image_bytes
        filename = f'{sku.replace("/", "-")}.png'
    else:
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=95)
        upload_bytes = out.getvalue()
        filename = f'{sku.replace("/", "-")}.jpg'

    ok_upload, msg_upload = upload_image_to_zoho(item_id, upload_bytes, filename, token)
    if not ok_upload:
        print(f'     ✗ Upload failed: {msg_upload}')
        return 'error'

    sync_result = f'Image OK ({w}x{h}px, {ratio:.2f}){dupe_flag}'
    dupe_note = '\n⚠ DUPE WARNING: Could not find a unique image for this variant.\nManually upload a variant-specific image in Zoho.' if is_dupe else ''
    notes = (
        '[IMAGE-AUTO] ({})\n'
        'PASS: Image auto-fetched and validated.\n'
        'Source: {}\n'
        'Dimensions: {}x{}px\n'
        'Aspect ratio: {:.2f} (within 0.8–1.2)\n'
        'Resolution: OK (minimum 800x800px met){}'
    ).format(today_str, url[:200], w, h, ratio, dupe_note)

    ok_write, msg_write = write_zoho_status(item_id, 'Image Validated', sync_result, notes, url, token)
    if not ok_write:
        print(f'     ✗ Status write failed: {msg_write}')
        return 'error'

    # Register this image in the collection hash tracker
    if collection and not is_dupe:
        collection_hashes.setdefault(collection, set()).add(img_md5)

    print(f'     → Written: Image Validated')
    return 'validated'

# ── --recheck-published mode ──────────────────────────────────────────────────

def fetch_all_shopify_products():
    """
    Page through all Shopify products and return list of
    {title, product_id, variants: [{sku, image_url}]}
    """
    products = []
    cursor = None

    while True:
        after = f', after: "{cursor}"' if cursor else ''
        gql = f"""
        {{
          products(first: 50{after}) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{
              node {{
                id
                title
                variants(first: 100) {{
                  edges {{
                    node {{
                      id
                      sku
                      image {{ url width height }}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        data = shopify_graphql(gql)
        edges = data.get('data', {}).get('products', {}).get('edges', [])
        page_info = data.get('data', {}).get('products', {}).get('pageInfo', {})

        for edge in edges:
            node = edge['node']
            variants = []
            for v_edge in node['variants']['edges']:
                v = v_edge['node']
                img = v.get('image') or {}
                variants.append({
                    'sku':       v.get('sku', ''),
                    'image_url': img.get('url', ''),
                    'img_w':     img.get('width', 0),
                    'img_h':     img.get('height', 0),
                })
            products.append({
                'title':    node['title'],
                'id':       node['id'],
                'variants': variants,
            })

        if not page_info.get('hasNextPage'):
            break
        cursor = page_info['endCursor']
        time.sleep(0.3)

    return products


# Products where all variants intentionally share one image (shade/colour lines with
# no variant-specific product photography). Duplicates within these are NOT flagged.
SINGLE_IMAGE_COLLECTIONS = {
    'Brilliance NG Enamel Composite',
    'Brilliance Flow Composite',
}


def zoho_lookup_by_sku(sku, token):
    """
    Look up a Zoho item by SKU via search_text (more reliable than sku= filter param).
    Verifies exact SKU match before returning. Returns None if not found.
    """
    resp = requests.get(
        f'{ZOHO_API_BASE}/items',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID, 'search_text': sku, 'per_page': 10},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return zoho_lookup_by_sku(sku, token)
    items = resp.json().get('items', [])
    for it in items:
        if it.get('sku') == sku:
            return {
                'item_id':         it.get('item_id', ''),
                'sku':             it.get('sku', ''),
                'name':            it.get('name', ''),
                'brand':           it.get('brand', ''),
                'category':        it.get('category_name', ''),
                'parent_category': it.get('parent_category_name', ''),
                'rate':            it.get('rate', 0),
            }
    return None


def zoho_lookup_by_name(shopify_title, sku_hint, token):
    """
    Search Zoho by Shopify product title keywords, then verify exact SKU match.
    Used as a fallback when zoho_lookup_by_sku() returns None.
    Only returns a result if an exact SKU match is found among the name-search results —
    this prevents associating the wrong Zoho item with a Shopify variant.
    """
    keywords = ' '.join(shopify_title.split()[:4])
    resp = requests.get(
        f'{ZOHO_API_BASE}/items',
        headers={**zoho_headers(token), 'Content-Type': 'application/json'},
        params={'organization_id': ZOHO_ORG_ID, 'search_text': keywords, 'per_page': 25},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(60)
        return zoho_lookup_by_name(shopify_title, sku_hint, token)
    items = resp.json().get('items', [])
    for it in items:
        if it.get('sku') == sku_hint:
            return {
                'item_id':         it.get('item_id', ''),
                'sku':             it.get('sku', ''),
                'name':            it.get('name', ''),
                'brand':           it.get('brand', ''),
                'category':        it.get('category_name', ''),
                'parent_category': it.get('parent_category_name', ''),
                'rate':            it.get('rate', 0),
            }
    return None


def recheck_published(dry_run, token, input_by_sku):
    """
    Audit all Shopify products. For each variant:
      - Download its image and run check_image() (same rules as Stage 3)
      - Detect duplicates within the same product (same MD5 → different SKU)
        (skipped for products in SINGLE_IMAGE_COLLECTIONS)
    For items not found in enrichment_input.json, fall back to a direct Zoho
    lookup by SKU so old-format SKUs are not silently skipped.
    Requeue failing items in Zoho to "Image required", then auto-fetch them.
    Returns list of item dicts (same structure as enrichment_input.json items).
    """
    print('\n  Fetching all Shopify products...')
    products = fetch_all_shopify_products()
    print(f'  Found {len(products)} products on Shopify.\n')

    needs_requeue = []   # list of (sku, reason, prod_title)
    zoho_cache    = {}   # sku → item dict (live Zoho lookups, cached)

    for prod in products:
        prod_title = prod['title']
        variants   = prod['variants']
        if not variants:
            continue

        # Decide whether to check for intra-product duplicate images
        skip_dupe_check = prod_title in SINGLE_IMAGE_COLLECTIONS
        product_hashes  = {}  # md5 → sku

        for v in variants:
            sku       = v['sku']
            image_url = v['image_url']
            img_w     = v['img_w']
            img_h     = v['img_h']

            if not sku:
                continue

            if not image_url:
                needs_requeue.append((sku, 'No image on Shopify', prod_title))
                continue

            # Quick pre-check via metadata before downloading
            if img_w and img_h and img_w >= MIN_WIDTH and img_h >= MIN_HEIGHT:
                if skip_dupe_check:
                    continue  # resolution OK and duplicates are acceptable
                image_bytes = download_image(image_url)
                if not image_bytes:
                    needs_requeue.append((sku, 'Image URL not downloadable', prod_title))
                    continue
                img_md5 = md5_of(image_bytes)
                if img_md5 in product_hashes:
                    other_sku = product_hashes[img_md5]
                    needs_requeue.append((sku, f'Duplicate image (same as {other_sku})', prod_title))
                    if not any(s == other_sku for s, *_ in needs_requeue):
                        needs_requeue.append((other_sku, f'Duplicate image (same as {sku})', prod_title))
                else:
                    product_hashes[img_md5] = sku
            else:
                # Metadata says too small, or no metadata — download and check
                image_bytes = download_image(image_url)
                if not image_bytes:
                    needs_requeue.append((sku, 'Image URL not downloadable', prod_title))
                    continue
                passed, w, h, ratio, fail_reason, _ = check_image(image_bytes)
                img_md5 = md5_of(image_bytes)
                if not passed:
                    needs_requeue.append((sku, fail_reason, prod_title))
                elif not skip_dupe_check:
                    if img_md5 in product_hashes:
                        other_sku = product_hashes[img_md5]
                        needs_requeue.append((sku, f'Duplicate image (same as {other_sku})', prod_title))
                        if not any(s == other_sku for s, *_ in needs_requeue):
                            needs_requeue.append((other_sku, f'Duplicate image (same as {sku})', prod_title))
                    else:
                        product_hashes[img_md5] = sku

            time.sleep(0.1)

    # Report
    print(f'  Audit complete.\n')
    if not needs_requeue:
        print('  ✓ All Shopify product images meet the standard. Nothing to requeue.')
        return []

    print(f'  ⚠  {len(needs_requeue)} variant(s) need image replacement:\n')

    requeue_items = []
    for sku, reason, prod_title in needs_requeue:
        src = input_by_sku.get(sku)

        if not src:
            # Fall back 1: live Zoho lookup by SKU via search_text
            if sku not in zoho_cache:
                zoho_cache[sku] = zoho_lookup_by_sku(sku, token)
                if not zoho_cache[sku] and prod_title:
                    # Fall back 2: search by Shopify product title keywords + verify SKU
                    time.sleep(0.2)
                    zoho_cache[sku] = zoho_lookup_by_name(prod_title, sku, token)
                time.sleep(0.2)
            src = zoho_cache[sku]

        tag = '  ✗ not found in Zoho' if not src else ''
        print(f'    {sku:<22s}  {reason}{tag}')
        if src:
            requeue_items.append(src)

    if dry_run:
        print(f'\n  Dry run — {len(requeue_items)} item(s) would be requeued.')
        return []

    print(f'\n  Resetting {len(requeue_items)} item(s) to "Image required" in Zoho...')
    reset_count = 0
    for src in requeue_items:
        item_id = src['item_id']
        resp = requests.put(
            f'{ZOHO_API_BASE}/items/{item_id}',
            headers={**zoho_headers(token), 'Content-Type': 'application/json'},
            params={'organization_id': ZOHO_ORG_ID},
            json={'custom_fields': [{'api_name': 'cf_shopify_status', 'value': 'Image required'}]},
            timeout=15,
        )
        if resp.json().get('code') == 0:
            reset_count += 1
        time.sleep(0.2)

    print(f'  Reset {reset_count} item(s). Starting auto-fetch...\n')
    return requeue_items

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Stage 3b — Auto Image Fetcher')
    parser.add_argument('--dry-run',           action='store_true', help='Preview without writing to Zoho')
    parser.add_argument('--sku',               help='Process a single SKU only')
    parser.add_argument('--recheck-published', action='store_true',
                        help='Audit all live Shopify products for bad/duplicate images, requeue, then auto-fetch')
    parser.add_argument('--brands', nargs='+', metavar='BRAND',
                        help='Only process items whose brand field matches one of these (case-insensitive)')
    args = parser.parse_args()

    print('═' * 60)
    print('  Stage 3b — Auto Image Fetcher')
    if args.dry_run:
        print('  Mode: DRY RUN')
    if args.recheck_published:
        print('  Mode: RECHECK PUBLISHED (audit Shopify + requeue + fetch)')
    if args.sku:
        print(f'  Filter: SKU = {args.sku}')
    if args.brands:
        print(f'  Filter: brands = {", ".join(args.brands)}')
    print('═' * 60)

    # Load feedback hints (merges into BRAND_TRUSTED_DOMAINS, COMPETITOR_DOMAINS,
    # MANUAL_OVERRIDES, MANUAL_ONLY_SKUS, MANUAL_ONLY_COLLECTIONS)
    load_image_hints()

    if not os.path.exists(ENRICHMENT_INPUT):
        print(f'✗ {ENRICHMENT_INPUT} not found. Run Stage 1 + Stage 2 first.')
        sys.exit(1)

    with open(ENRICHMENT_INPUT) as f:
        all_input = json.load(f)
    input_by_sku = {it['sku']: it for it in all_input if it.get('sku')}

    # Load enrichment output for richer queries and collection membership
    output_by_sku = {}
    if os.path.exists(ENRICHMENT_OUTPUT):
        with open(ENRICHMENT_OUTPUT) as f:
            output_data = json.load(f)
        output_by_sku = {it['sku']: it for it in output_data if it.get('sku')}
    else:
        print(f'  ⚠ {ENRICHMENT_OUTPUT} not found — queries will use raw names only.')

    token     = get_zoho_token()
    today_str = date.today().isoformat()

    # Shared collection hash tracker across all items in this session
    collection_hashes = {}  # {collection_name: set(md5s)}

    # ── Recheck-published mode ────────────────────────────────────────────────
    if args.recheck_published:
        requeue_items = recheck_published(
            dry_run=args.dry_run,
            token=token,
            input_by_sku=input_by_sku,
        )
        if not requeue_items:
            print('\n  Nothing to fetch.')
            sys.exit(0)
        # Fall through: process the requeued items below
        items_to_process = requeue_items
        if args.sku:
            items_to_process = [it for it in items_to_process if it.get('sku') == args.sku]
    else:
        # Normal mode: read from enrichment_input.json
        # Filter out non-item objects (e.g. the injected feedback_context block)
        items_to_process = [it for it in all_input if it.get('item_id')]
        if args.sku:
            items_to_process = [it for it in items_to_process if it.get('sku') == args.sku]
            if not items_to_process:
                print(f'✗ SKU {args.sku} not found in {ENRICHMENT_INPUT}')
                sys.exit(1)
        if args.brands:
            brand_set = {b.lower() for b in args.brands}
            items_to_process = [it for it in items_to_process
                                if (it.get('brand') or '').lower() in brand_set]
            if not items_to_process:
                print(f'✗ No items found matching brands: {", ".join(args.brands)}')
                sys.exit(1)

    print(f'\n  Processing {len(items_to_process)} item(s)...')

    counts = {'validated': 0, 'would_validate': 0, 'no_image': 0, 'skipped': 0, 'error': 0}

    for item in items_to_process:
        try:
            token = get_zoho_token()
            result = process_item(item, token, args.dry_run, today_str,
                                  output_by_sku, collection_hashes)
        except Exception as e:
            print(f'  ✗ Unhandled error for {item.get("sku", "?")}: {e}')
            result = 'error'
        counts[result] = counts.get(result, 0) + 1
        # DDG needs a 20s cool-down between items; CSE does not.
        used, limit = _cse_quota()
        time.sleep(3 if used < limit else 20)

    print()
    print('═' * 60)
    print('  Done.')
    if args.dry_run:
        print(f'  Would validate  : {counts["would_validate"]}')
    else:
        print(f'  Validated       : {counts["validated"]}')
    print(f'  No image found  : {counts["no_image"]}')
    print(f'  Skipped         : {counts["skipped"]}')
    if counts.get('error'):
        print(f'  Errors          : {counts["error"]}')

    if args.dry_run:
        print('\n  Dry run — no changes written to Zoho.')
    elif counts['validated']:
        print(f'\n  → {counts["validated"]} new item(s) Image Validated. Run Stage 4 next.')
        print(     '     python execution/validate_enrichment.py')
    if counts['no_image']:
        print(f'  → {counts["no_image"]} item(s) still need manual images in Zoho.')
    print('═' * 60)


if __name__ == '__main__':
    main()
