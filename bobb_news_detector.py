import datetime, requests, json, os, re
import warnings
warnings.filterwarnings('ignore')

# ════════════════════════════════════════
# VERSION
# ════════════════════════════════════════
VERSION = 'v1.8'
# v1.0 — Initial standalone news detector
# v1.1 — Dual source FF scraping + MyFxBook fallback
# v1.2 — Primary: FF JSON (nfs.faireconomy.media) — lebih stabil dari scraping
#         Fallback 1: FF HTML scraping
#         Fallback 2: MyFxBook scraping
#         Format waktu WIB + UTC semua pesan
# v1.3 — Breaking News module: RSS Feed (primary) + NewsAPI (fallback)
#         Keyword filter otomatis: war, sanctions, Fed, rate, oil, gold, crypto, dll
#         Anti-duplikat via state, cooldown 4 jam per keyword group
# v1.4 — FIX: sent_breaking cleanup bug — cooldown key tidak pernah expire (zombie)
#         FIX: dedup hash diperpanjang dari 24 jam ke 7 hari — berita sama tidak re-trigger
# v1.5 — FIX: pubDate filter max 6 jam — no more stale/old news
#         FIX: false positive — 'war' whole-word match, 'crisis' butuh financial context
#         FIX: RSS feeds — hapus Bloomberg/MarketWatch (block publik), tambah FT & Guardian
#         FIX: 'sec' keyword dipindah jadi 'sec crypto' supaya tidak false positive
# v1.6 — FIX: Bot token & Chat ID pindah ke env vars (GitHub Secrets) — no more hardcode
#         FIX: sent_reminder & sent_actual cleanup — state JSON tidak lagi membengkak
#         FIX: Hapus FT RSS feed — paywall, selalu 401/403 di GitHub Actions
#         FIX: Eliminate double-fetch di run_news_detector — hemat bandwidth
#         FIX: trade war conflict — hapus dari WAR_FALSE_POSITIVES, tetap di Macro keywords
# v1.7 — NEW: Price Spike Detector — XAUUSD, BTCUSDT, 9 forex pairs
#         Source: Binance public API (BTC), Yahoo Finance (XAU + forex) — gratis, no key
#         Alert kalau harga gerak melebihi threshold dalam 5 menit terakhir
#         Cooldown 30 menit per pair — anti-spam saat volatilitas ekstrem
#         Pesan Telegram include: % move, direction, pair info, level harga
# v1.8 — NEW: Pair Direction Predictor di Actual Result
#         Setelah data rilis, bot prediksi arah tiap pair yang terdampak
#         Logic: currency strength/weakness × pair composition × safe haven behavior
#         Cover: USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD, XAU, BTC
#         Output: ↑/↓/↔ per pair dengan confidence label (Strong/Moderate/Watch)

# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════
BOT_TOKEN   = os.environ.get('BOT_TOKEN', '')    # Set via GitHub Secrets / env var
CHAT_ID     = os.environ.get('CHAT_ID', '')      # Set via GitHub Secrets / env var
NEWSAPI_KEY = os.environ.get('NEWSAPI_KEY', '')  # Opsional — newsapi.org free tier

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError('[CONFIG] BOT_TOKEN dan CHAT_ID harus diset via environment variable!')

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'bobb_news_state.json'
)

# ── Breaking News Keywords ────────────────────────────────────────────
# Dikelompokkan per tema — kalau salah satu keyword match, berita dikirim
BREAKING_KEYWORDS = {
    '⚔️ Geopolitical': [
        # 'war' pakai whole-word — hindari false positive "currency war", "star wars"
        # 'crisis' dihapus — terlalu broad, diganti multi-word spesifik
        ' war ', 'attack', 'missile', 'airstrike', 'invasion', 'conflict',
        'troops', 'nuclear', 'sanctions', 'ceasefire', 'explosion', 'coup',
        'terrorism', 'escalation', 'military strike',
        'debt crisis', 'banking crisis', 'financial crisis', 'currency crisis',
    ],
    '🏦 Central Bank': [
        'federal reserve', 'fomc', 'rate hike', 'rate cut',
        'interest rate', 'ecb', 'bank of england', 'bank of japan',
        'rba', 'monetary policy', 'quantitative easing', 'quantitative tightening',
        'powell', 'lagarde', 'inflation target', 'boj rate', 'fed rate',
    ],
    '📈 Market Moving': [
        'emergency', 'market crash', 'stock crash', 'collapse',
        'sovereign default', 'recession', 'bank failure', 'bank run',
        'circuit breaker', 'trading halt', 'bailout',
        'debt ceiling', 'credit downgrade', 'credit rating cut',
    ],
    '🥇 Gold & Oil': [
        'gold price', 'gold rally', 'gold falls', 'xauusd',
        'oil price', 'crude oil', 'opec', 'petroleum',
        'energy crisis', 'supply cut', 'oil output',
    ],
    '₿ Crypto': [
        'bitcoin', 'btc', 'ethereum', 'crypto',
        'sec crypto', 'sec bitcoin', 'etf approval', 'crypto etf',
        'exchange hack', 'stablecoin', 'cbdc', 'crypto regulation',
    ],
    '🌍 Macro': [
        'gdp', 'unemployment rate', 'nonfarm payroll', 'cpi inflation',
        'ppi', 'trade war', 'tariff', 'us dollar', 'treasury yield',
        'yield curve', 'government bond',
    ],
}

# Cooldown — jangan kirim berita dari group yang sama dalam X jam
BREAKING_COOLDOWN_HOURS = 4

# RSS Feeds — gratis, tidak perlu API key
# Bloomberg & MarketWatch dihapus — sudah block RSS publik
# FT dihapus — paywall, return 401/403 di GitHub Actions
RSS_FEEDS = [
    ('Reuters',   'https://feeds.reuters.com/reuters/businessNews'),
    ('Reuters',   'https://feeds.reuters.com/reuters/topNews'),
    ('BBC',       'https://feeds.bbci.co.uk/news/business/rss.xml'),
    ('BBC',       'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('CNBC',      'https://www.cnbc.com/id/10000664/device/rss/rss.html'),
    ('CNBC',      'https://www.cnbc.com/id/20910258/device/rss/rss.html'),  # CNBC Finance
    ('Guardian',  'https://www.theguardian.com/business/rss'),
]

# Max umur berita yang akan diproses (jam) — filter stale news dari RSS
RSS_MAX_AGE_HOURS = 6

WATCHED_CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CAD', 'CHF', 'NZD', 'XAU', 'BTC']

CURRENCY_PAIRS = {
    'USD': ['XAUUSD', 'BTCUSDT', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF', 'NZDUSD'],
    'EUR': ['EURUSD', 'EURJPY', 'GBPJPY'],
    'GBP': ['GBPUSD', 'GBPJPY'],
    'JPY': ['USDJPY', 'EURJPY', 'GBPJPY'],
    'AUD': ['AUDUSD'],
    'CAD': ['USDCAD'],
    'CHF': ['USDCHF'],
    'NZD': ['NZDUSD'],
    'XAU': ['XAUUSD'],
    'BTC': ['BTCUSDT'],
}

IMPACT_CONFIG = {
    'High':         {'emoji': '🔴', 'priority': 3, 'include': True},
    'Medium':       {'emoji': '🟡', 'priority': 2, 'include': True},
    'Low':          {'emoji': '⚪', 'priority': 1, 'include': False},
    'Non-Economic': {'emoji': '⬜', 'priority': 0, 'include': False},
}

# ════════════════════════════════════════
# PRICE SPIKE CONFIG
# ════════════════════════════════════════
# Threshold % move dalam 5 menit untuk trigger alert
SPIKE_THRESHOLDS = {
    'XAUUSD':  0.30,   # Gold: alert kalau gerak > 0.30% (~$7-8 dari $2500)
    'BTCUSDT': 1.00,   # BTC: alert kalau gerak > 1.00% (~$600 dari $60k)
    'EURUSD':  0.15,   # Major forex: 15 pip equivalent
    'GBPUSD':  0.15,
    'USDJPY':  0.15,
    'AUDUSD':  0.15,
    'USDCAD':  0.15,
    'USDCHF':  0.15,
    'NZDUSD':  0.15,
    'EURJPY':  0.20,
    'GBPJPY':  0.20,
}

# Cooldown per pair — jangan spam saat market volatile
SPIKE_COOLDOWN_MINUTES = 30

# Yahoo Finance ticker mapping
YAHOO_TICKERS = {
    'XAUUSD': 'GC=F',       # Gold futures
    'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'JPY=X',
    'AUDUSD': 'AUDUSD=X',
    'USDCAD': 'CAD=X',
    'USDCHF': 'CHF=X',
    'NZDUSD': 'NZDUSD=X',
    'EURJPY': 'EURJPY=X',
    'GBPJPY': 'GBPJPY=X',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

DIV  = '─' * 30
DIV2 = '═' * 30

# ════════════════════════════════════════
# STATE
# ════════════════════════════════════════
def load_state():
    default = {
        'sent_daily':    {},
        'sent_reminder': {},
        'sent_actual':   {},
        'sent_breaking': {},   # title_hash -> timestamp string
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        for k, v in default.items():
            if k not in state:
                state[k] = v
        return state
    except Exception as e:
        print(f'[STATE] Load error: {e}')
        return default

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f'[STATE] Save error: {e}')

# ════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════
def send_text(text):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    try:
        resp = requests.post(
            url,
            data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=30
        )
        return resp.json()
    except Exception as e:
        print(f'[TELEGRAM] Error: {e}')
        return {'ok': False}

# ════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════
def clean_val(val):
    if not val:
        return ''
    val = re.sub(r'<[^>]+>', '', str(val)).strip()
    return val if val else ''

def make_event(currency, title, impact_raw, dt_utc, forecast='', previous='', actual='', source=''):
    cfg = IMPACT_CONFIG.get(impact_raw, IMPACT_CONFIG['Low'])
    if not cfg['include']:
        return None
    if currency not in WATCHED_CURRENCIES:
        return None
    dt_wib = dt_utc + datetime.timedelta(hours=7)
    event_id = f"{currency}_{re.sub(r'[^A-Za-z0-9]','_',title)[:20]}_{dt_utc.strftime('%H%M')}"
    return {
        'id':              event_id,
        'currency':        currency,
        'title':           title,
        'impact':          impact_raw,
        'impact_emoji':    cfg['emoji'],
        'impact_priority': cfg['priority'],
        'dt_utc':          dt_utc,
        'dt_wib':          dt_wib,
        'time_wib':        dt_wib.strftime('%H:%M'),
        'time_utc':        dt_utc.strftime('%H:%M'),
        'forecast':        clean_val(forecast),
        'previous':        clean_val(previous),
        'actual':          clean_val(actual),
        'affected_pairs':  CURRENCY_PAIRS.get(currency, [currency]),
        'source':          source,
    }

# ════════════════════════════════════════
# SOURCE 1 — FF JSON (PRIMARY)
# Endpoint komunitas trader, stabil bertahun-tahun
# ════════════════════════════════════════
def fetch_ff_json(target_date):
    urls = [
        'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
        'https://nfs.faireconomy.media/ff_calendar_nextweek.json',
    ]
    raw = []
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                raw.extend(resp.json())
        except Exception as e:
            print(f'[FF_JSON] {url} error: {e}')

    if not raw:
        return []

    events = []
    for item in raw:
        try:
            currency = item.get('country', '').upper()
            impact_raw = item.get('impact', 'Low').capitalize()
            title      = item.get('title', 'N/A')
            date_raw   = item.get('date', '')

            if not date_raw:
                continue

            # Parse datetime — FF JSON pakai format ISO dengan offset EST
            try:
                dt_raw = datetime.datetime.fromisoformat(date_raw)
                # Konversi ke UTC
                if dt_raw.utcoffset() is not None:
                    dt_utc = dt_raw - dt_raw.utcoffset()
                    dt_utc = dt_utc.replace(tzinfo=None)
                else:
                    # Assume EST = UTC-5
                    dt_utc = dt_raw + datetime.timedelta(hours=5)
            except Exception:
                try:
                    dt_raw = datetime.datetime.strptime(date_raw[:19], '%Y-%m-%dT%H:%M:%S')
                    dt_utc = dt_raw + datetime.timedelta(hours=5)
                except Exception:
                    continue

            if dt_utc.date() != target_date:
                continue

            ev = make_event(
                currency   = currency,
                title      = title,
                impact_raw = impact_raw,
                dt_utc     = dt_utc,
                forecast   = item.get('forecast', ''),
                previous   = item.get('previous', ''),
                actual     = item.get('actual', ''),
                source     = 'FF-JSON',
            )
            if ev:
                events.append(ev)

        except Exception as e:
            print(f'[FF_JSON] Parse error: {e}')

    events.sort(key=lambda x: x['dt_utc'])
    print(f'[FF_JSON] {len(events)} events for {target_date}')
    return events

# ════════════════════════════════════════
# SOURCE 2 — FF HTML SCRAPING (FALLBACK 1)
# ════════════════════════════════════════
def fetch_ff_html(target_date):
    try:
        date_param = target_date.strftime('%b%d.%Y').lower()
        url = f'https://www.forexfactory.com/calendar?day={date_param}'
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f'[FF_HTML] HTTP {resp.status_code}')
            return []

        html   = resp.text
        events = _parse_ff_html(html, target_date)
        print(f'[FF_HTML] {len(events)} events')
        return events
    except Exception as e:
        print(f'[FF_HTML] Error: {e}')
        return []

def _parse_ff_html(html, target_date):
    events = []
    try:
        rows = re.findall(r'<tr[^>]*calendar__row[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
        last_dt = None

        for row in rows:
            try:
                curr_m = re.search(r'calendar__currency[^>]*>\s*([A-Z]{3})\s*<', row, re.IGNORECASE)
                if not curr_m:
                    continue
                currency = curr_m.group(1).upper()

                imp_m = re.search(r'impact--(\w+)', row, re.IGNORECASE)
                if not imp_m:
                    continue
                impact_raw = imp_m.group(1).capitalize()

                title_m = re.search(r'calendar__event-title[^>]*>\s*([^<]+)\s*<', row, re.IGNORECASE)
                title   = clean_val(title_m.group(1)) if title_m else 'N/A'

                time_m   = re.search(r'calendar__time[^>]*>\s*([^<]+)\s*<', row, re.IGNORECASE)
                time_str = clean_val(time_m.group(1)) if time_m else ''
                dt_utc   = _parse_ff_time(time_str, target_date, last_dt)
                if dt_utc:
                    last_dt = dt_utc
                else:
                    dt_utc = last_dt
                if not dt_utc:
                    continue

                fore_m = re.search(r'calendar__forecast[^>]*>\s*([^<]*)', row, re.IGNORECASE)
                act_m  = re.search(r'calendar__actual[^>]*>\s*([^<]*)',   row, re.IGNORECASE)
                prev_m = re.search(r'calendar__previous[^>]*>\s*([^<]*)', row, re.IGNORECASE)

                ev = make_event(
                    currency   = currency,
                    title      = title,
                    impact_raw = impact_raw,
                    dt_utc     = dt_utc,
                    forecast   = fore_m.group(1) if fore_m else '',
                    previous   = prev_m.group(1) if prev_m else '',
                    actual     = act_m.group(1)  if act_m  else '',
                    source     = 'FF-HTML',
                )
                if ev:
                    events.append(ev)
            except Exception:
                continue

        events.sort(key=lambda x: x['dt_utc'])
    except Exception as e:
        print(f'[FF_HTML_PARSE] Error: {e}')
    return events

def _parse_ff_time(time_str, target_date, last_dt):
    try:
        time_str = clean_val(time_str).upper()
        if not time_str or time_str in ['ALL DAY', 'TENTATIVE', '']:
            return None
        m = re.match(r'(\d{1,2}):(\d{2})(AM|PM)', time_str)
        if not m:
            return None
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == 'PM' and h != 12:
            h += 12
        elif ampm == 'AM' and h == 12:
            h = 0
        dt_est = datetime.datetime(target_date.year, target_date.month, target_date.day, h, mn)
        return dt_est + datetime.timedelta(hours=5)  # EST → UTC
    except Exception:
        return None

# ════════════════════════════════════════
# SOURCE 3 — MYFXBOOK (FALLBACK 2)
# ════════════════════════════════════════
def fetch_myfxbook(target_date):
    try:
        ds  = target_date.strftime('%Y-%m-%d')
        url = f'https://www.myfxbook.com/forex-economic-calendar/{ds}/{ds}'
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f'[MFB] HTTP {resp.status_code}')
            return []

        html   = resp.text
        events = _parse_mfb_html(html, target_date)
        print(f'[MFB] {len(events)} events')
        return events
    except Exception as e:
        print(f'[MFB] Error: {e}')
        return []

def _parse_mfb_html(html, target_date):
    events = []
    try:
        rows = re.findall(r'<tr[^>]*calRow[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
        for row in rows:
            try:
                curr_m = re.search(r'currency[^>]*>\s*([A-Z]{3})\s*<', row, re.IGNORECASE)
                if not curr_m:
                    continue
                currency = curr_m.group(1).upper()

                imp_m      = re.search(r'impact[_-](\w+)', row, re.IGNORECASE)
                impact_raw = imp_m.group(1).capitalize() if imp_m else 'Low'

                title_m = re.search(r'event[^>]*>\s*<[^>]+>\s*([^<]+)', row, re.IGNORECASE)
                title   = clean_val(title_m.group(1)) if title_m else 'N/A'

                time_m   = re.search(r'time[^>]*>\s*([^<]+)\s*<', row, re.IGNORECASE)
                time_str = clean_val(time_m.group(1)) if time_m else ''
                dt_utc   = _parse_mfb_time(time_str, target_date)
                if not dt_utc:
                    continue

                fore_m = re.search(r'forecast[^>]*>\s*([^<]*)', row, re.IGNORECASE)
                act_m  = re.search(r'actual[^>]*>\s*([^<]*)',   row, re.IGNORECASE)
                prev_m = re.search(r'previous[^>]*>\s*([^<]*)', row, re.IGNORECASE)

                ev = make_event(
                    currency   = currency,
                    title      = title,
                    impact_raw = impact_raw,
                    dt_utc     = dt_utc,
                    forecast   = fore_m.group(1) if fore_m else '',
                    previous   = prev_m.group(1) if prev_m else '',
                    actual     = act_m.group(1)  if act_m  else '',
                    source     = 'MyFxBook',
                )
                if ev:
                    events.append(ev)
            except Exception:
                continue
        events.sort(key=lambda x: x['dt_utc'])
    except Exception as e:
        print(f'[MFB_PARSE] Error: {e}')
    return events

def _parse_mfb_time(time_str, target_date):
    try:
        m = re.match(r'(\d{1,2}):(\d{2})', time_str.strip())
        if not m:
            return None
        h, mn = int(m.group(1)), int(m.group(2))
        return datetime.datetime(target_date.year, target_date.month, target_date.day, h, mn)
    except Exception:
        return None

# ════════════════════════════════════════
# FETCH — TRIPLE SOURCE
# ════════════════════════════════════════
def fetch_events(target_date):
    # Primary: FF JSON
    print('[FETCH] Trying FF-JSON (primary)...')
    events = fetch_ff_json(target_date)
    if events:
        return events, 'FF-JSON'

    # Fallback 1: FF HTML
    print('[FETCH] Trying FF-HTML (fallback 1)...')
    events = fetch_ff_html(target_date)
    if events:
        return events, 'FF-HTML'
