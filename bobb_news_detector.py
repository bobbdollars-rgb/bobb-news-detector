import datetime, requests, json, os, re
import warnings
warnings.filterwarnings('ignore')

# ════════════════════════════════════════
# VERSION
# ════════════════════════════════════════
VERSION = 'v2.1'
# v1.0  — Initial standalone news detector
# v1.1  — Dual source FF scraping + MyFxBook fallback
# v1.2  — Primary FF JSON, fallback FF HTML + MyFxBook
# v1.3  — Breaking News: RSS + NewsAPI fallback
# v1.4  — FIX: breaking news dedup & cleanup bugs
# v1.5  — FIX: pubDate filter, false positive, RSS cleanup
# v1.6  — FIX: env vars, state cleanup, double-fetch
# v1.7  — NEW: Price Spike Detector (Binance + Yahoo)
# v1.8  — NEW: Pair Direction Predictor
# v1.9  — FIX: Daily Briefing window 15m → 59m
# v1.10 — FIX: Reminder 10m → 30m, Actual 20m → 40m
# v2.0  — MAJOR UPGRADE (all phases, no new API keys required):
#   [Phase 1] Beat/Miss Enhanced — deviation magnitude (STRONG BEAT/BEAT/IN-LINE/MISS/STRONG MISS)
#             Threshold dinamis per event type (CPI/NFP/GDP/Rate/PMI)
#             Confidence predictor naik otomatis kalau deviation besar
#   [Phase 2] Actual Cross-Check — investing.com sebagai fallback source actual
#             Berantai: FF JSON actual → investing.com scrape → skip
#   [Phase 3] Forex Price Upgrade — exchangerate-api.com (gratis, real-time, no key)
#             Yahoo Finance tetap sebagai fallback; BTC tetap Binance
#   [Phase 4] Breaking News Source Tambah — Reddit RSS (economics/investing/worldnews)
#             + Al Jazeera English RSS; keyword matching lebih konsisten
#   [Phase 5] Sentiment Layer — Market Sentiment Score per event (0-100)
#             Input: beat/miss magnitude + session + pair liquidity weight
#             Output: BULLISH/BEARISH/NEUTRAL bias tampil di actual result message
# v2.1  — AKURASI FIX (3 isu hasil audit):
#   [Fix 1] Forex Spike Detector: exchangerate-api.com (open.er-api.com) DIHAPUS dari
#           jalur spike — endpoint itu cuma refresh 1x/24 jam (dikonfirmasi di dokumentasi
#           resminya), bukan "real-time tiap menit" seperti asumsi v2.0. Refresh sekali
#           sehari berarti "harga 5 menit lalu" sebenarnya cuma harga run sebelumnya →
#           spike asli kelewat (false negative) hampir selalu, lalu sesekali muncul
#           false positive pas data itu akhirnya berubah (lonjakan 24 jam terbaca
#           sebagai "spike 5 menit"). FIX: semua pair forex + XAUUSD sekarang full
#           lewat Yahoo Finance (1m candle asli, granularitas 5 menit yang sebenarnya).
#   [Fix 2] Breaking News Reddit RSS: ganti ke old.reddit.com (lebih permisif utk
#           request non-browser dibanding www.reddit.com yang sering balas 403 dari
#           IP datacenter/CI). Tetap best-effort — Reddit bisa block kapan aja, tapi
#           sudah fail-safe (gak crash, cuma skip source itu kalau gagal).
#   [Fix 3] investing.com cross-check (Phase 2) DIHAPUS. Endpoint itu pakai Cloudflare
#           protection & butuh session/CSRF yang gak feasible dari GitHub Actions tanpa
#           browser — request kemungkinan besar selalu kena block. Daripada kasih ilusi
#           "ada fallback" yang silently gagal, actual result sekarang murni dari
#           FF-JSON/FF-HTML/MyFxBook; kalau actual masih kosong, event itu di-skip.

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

# Yahoo Finance ticker mapping — fallback untuk forex & XAU spike detector
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

# ── v2.1: exchangerate-api.com DIHAPUS dari spike detector ──────────────────
# Alasan: open.er-api.com (free tier) cuma refresh 1x/24 jam per dokumentasi
# resminya — bukan real-time. Gak cocok buat deteksi spike 5 menit. Semua pair
# forex sekarang full pakai Yahoo Finance (lihat YAHOO_TICKERS), yang punya
# 1-minute candle asli jadi perbandingan 5 menit beneran akurat.

# ── Phase 4: Additional RSS feeds ──────────────────────────────────────────
# v2.1: ganti ke old.reddit.com — www.reddit.com lebih agresif balas 403 ke
# request non-browser dari IP datacenter/CI (GitHub Actions). old.reddit.com
# historisnya lebih permisif, tapi tetap best-effort: Reddit bisa block kapan
# aja tanpa warning, makanya fetch_reddit_rss() sudah fail-safe per feed.
REDDIT_RSS_FEEDS = [
    ('Reddit/Economics',  'https://old.reddit.com/r/economics/new/.rss'),
    ('Reddit/Investing',  'https://old.reddit.com/r/investing/new/.rss'),
    ('Reddit/WorldNews',  'https://old.reddit.com/r/worldnews/new/.rss'),
]
EXTRA_RSS_FEEDS = [
    ('AlJazeera',   'https://www.aljazeera.com/xml/rss/all.xml'),
]

# ── Phase 5: Session config ────────────────────────────────────────────────
# Session UTC hours — dipakai untuk sentiment score weighting
SESSIONS = {
    'Asia':   (0,  8),    # 00:00–08:00 UTC = 07:00–15:00 WIB
    'London': (7,  16),   # 07:00–16:00 UTC = 14:00–23:00 WIB
    'NY':     (12, 21),   # 12:00–21:00 UTC = 19:00–04:00 WIB
}

# Pair liquidity weight — lebih liquid = dampak lebih reliable
PAIR_LIQUIDITY = {
    'EURUSD': 1.0, 'GBPUSD': 0.9, 'USDJPY': 0.9,
    'XAUUSD': 0.85, 'AUDUSD': 0.8, 'USDCAD': 0.75,
    'USDCHF': 0.75, 'NZDUSD': 0.7, 'EURJPY': 0.8,
    'GBPJPY': 0.75, 'BTCUSDT': 0.7,
}

# ── Phase 1: Beat/Miss threshold per event type ────────────────────────────
# Deviation threshold untuk STRONG BEAT/MISS (dalam unit event tsb)
EVENT_THRESHOLDS = {
    'cpi':          0.2,   # 0.2% deviation = strong
    'ppi':          0.2,
    'inflation':    0.2,
    'nonfarm':      50,    # 50k jobs deviation = strong
    'nfp':          50,
    'payroll':      50,
    'gdp':          0.3,   # 0.3% deviation = strong
    'unemployment': 0.2,   # 0.2% deviation = strong
    'rate':         0.1,   # rate decisions
    'pmi':          1.0,   # 1 point PMI = strong
    'retail':       0.3,
    'default':      0.1,   # fallback threshold
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
# v2.1: Phase 2 (investing.com cross-check) DIHAPUS.
# Alasan: investing.com pakai Cloudflare protection + butuh session/CSRF yang
# gak feasible dari GitHub Actions tanpa browser nyata — POST ke
# getCalendarFilteredData kemungkinan besar selalu kena block (403/empty).
# Daripada nyimpen kode yang kasih ilusi "ada fallback" tapi silently gagal,
# lebih jujur kalau actual result murni dari FF-JSON/FF-HTML/MyFxBook saja.
# ════════════════════════════════════════

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

    # Fallback 2: MyFxBook
    print('[FETCH] Trying MyFxBook (fallback 2)...')
    events = fetch_myfxbook(target_date)
    if events:
        return events, 'MyFxBook'

    print('[FETCH] ❌ All sources failed')
    return [], 'None'

# ════════════════════════════════════════
# FORMAT MESSAGES
# ════════════════════════════════════════
def fmt_daily_briefing(events, now, source):
    date_str = (now + datetime.timedelta(hours=7)).strftime('%A, %d %b %Y')

    if not events:
        return (
            f'📅 <b>ECONOMIC CALENDAR</b>\n'
            f'<b>Bobb Market Intelligence v2.0</b>\n'
            f'{DIV2}\n'
            f'📆 {date_str} (WIB)\n'
            f'{DIV}\n'
            f'✅ No high/medium impact news today.\n'
            f'<i>Safe to trade all sessions.</i>\n'
            f'{DIV2}\n'
            f'<i>Source: {source} | {now.strftime("%d %b %Y %H:%M")} UTC</i>'
        )

    high_ev   = [e for e in events if e['impact'] == 'High']
    medium_ev = [e for e in events if e['impact'] == 'Medium']

    lines = ''
    for e in events:
        pairs_str = ' '.join(e['affected_pairs'][:3])
        forecast  = e['forecast'] if e['forecast'] else '—'
        previous  = e['previous'] if e['previous'] else '—'
        lines += (
            f'\n{e["impact_emoji"]} <b>{e["time_wib"]} WIB</b> ({e["time_utc"]} UTC)'
            f'  [{e["currency"]}] {e["title"]}\n'
            f'   Forecast: {forecast}  |  Previous: {previous}\n'
            f'   Pairs: <i>{pairs_str}</i>\n'
        )

    return (
        f'📅 <b>ECONOMIC CALENDAR</b>\n'
        f'<b>Bobb Market Intelligence v2.0</b>\n'
        f'{DIV2}\n'
        f'📆 {date_str} (WIB)\n'
        f'{DIV}\n'
        f'🔴 High Impact  : <b>{len(high_ev)}</b> event(s)\n'
        f'🟡 Medium Impact: <b>{len(medium_ev)}</b> event(s)\n'
        f'{DIV}\n'
        f'{lines}'
        f'{DIV}\n'
        f'⚠️ Avoid new entries 30 min before & after HIGH impact!\n'
        f'{DIV2}\n'
        f'<i>Source: {source} | {now.strftime("%d %b %Y %H:%M")} UTC</i>'
    )

def fmt_reminder(event, minutes_left):
    pairs_str = ', '.join(event['affected_pairs'])
    forecast  = event['forecast'] if event['forecast'] else '—'
    previous  = event['previous'] if event['previous'] else '—'
    urgency   = '🚨' if event['impact'] == 'High' else '⚠️'

    return (
        f'{urgency} <b>NEWS REMINDER — {minutes_left} MIN</b>\n'
        f'{DIV}\n'
        f'{event["impact_emoji"]} <b>[{event["currency"]}] {event["title"]}</b>\n'
        f'{DIV}\n'
        f'🕐 Time     : <b>{event["time_wib"]} WIB</b>  ({event["time_utc"]} UTC)\n'
        f'📊 Impact   : <b>{event["impact"]}</b> {event["impact_emoji"]}\n'
        f'🎯 Forecast : {forecast}\n'
        f'📈 Previous : {previous}\n'
        f'{DIV}\n'
        f'💱 Affected : <i>{pairs_str}</i>\n'
        f'{DIV}\n'
        f'⛔ <b>Avoid new entries until news passes!</b>\n'
        f'<i>Bobb Market Intelligence v2.0</i>'
    )


# ════════════════════════════════════════
# PAIR DIRECTION PREDICTOR
# ════════════════════════════════════════
# Pair composition: BASE / QUOTE
PAIR_COMPOSITION = {
    'EURUSD':  ('EUR', 'USD'),
    'GBPUSD':  ('GBP', 'USD'),
    'USDJPY':  ('USD', 'JPY'),
    'AUDUSD':  ('AUD', 'USD'),
    'USDCAD':  ('USD', 'CAD'),
    'USDCHF':  ('USD', 'CHF'),
    'NZDUSD':  ('NZD', 'USD'),
    'EURJPY':  ('EUR', 'JPY'),
    'GBPJPY':  ('GBP', 'JPY'),
    'XAUUSD':  ('XAU', 'USD'),
    'BTCUSDT': ('BTC', 'USD'),
}

# Safe haven — naik saat risk-off
SAFE_HAVEN = {'XAU', 'JPY', 'CHF'}


def _predict_pair_directions(currency, sentiment, event_title=''):
    """
    Prediksi arah pair berdasarkan currency yang rilis & sentiment.
    sentiment: 'better' | 'worse' | 'inline'
    Returns list of dict sorted by confidence.
    """
    if sentiment == 'inline':
        return []

    is_better      = (sentiment == 'better')
    currency_strong = is_better   # better → currency menguat

    title_lower  = event_title.lower()
    is_inflation = any(k in title_lower for k in ['cpi', 'ppi', 'inflation', 'price index'])

    results = []

    for pair, (base, quote) in PAIR_COMPOSITION.items():
        if currency not in (base, quote):
            continue

        direction  = None
        confidence = 'Moderate'
        reason     = ''

        # ── Core: base/quote logic ───────────────────────────────────
        if currency == base:
            direction  = 'up' if currency_strong else 'down'
            confidence = 'Strong'
            reason     = f'{currency} {"menguat" if currency_strong else "melemah"} sbg base'
        elif currency == quote:
            direction  = 'down' if currency_strong else 'up'
            confidence = 'Strong'
            reason     = f'{currency} {"menguat" if currency_strong else "melemah"} sbg quote'

        # ── Safe haven override (non-direct) ─────────────────────────
        if base in SAFE_HAVEN and currency != base:
            direction  = 'up' if not currency_strong else 'down'
            confidence = 'Moderate'
            reason     = f'{"Risk-off" if not currency_strong else "Risk-on"} → {base} safe haven'

        if quote in SAFE_HAVEN and currency != quote:
            direction  = 'down' if not currency_strong else 'up'
            confidence = 'Moderate'
            reason     = f'{"Risk-off" if not currency_strong else "Risk-on"} → {quote} safe haven menguat'

        # ── XAUUSD special ───────────────────────────────────────────
        if pair == 'XAUUSD':
            if currency == 'USD':
                direction  = 'down' if currency_strong else 'up'
                confidence = 'Strong'
                reason     = f'USD {"menguat" if currency_strong else "melemah"} → XAUUSD inverse'
            else:
                direction  = 'up' if not currency_strong else 'down'
                confidence = 'Watch'
                reason     = f'{"Risk-off" if not currency_strong else "Risk-on"} → XAU {"naik" if not currency_strong else "koreksi"}'

        # ── BTCUSDT special ──────────────────────────────────────────
        if pair == 'BTCUSDT':
            if currency == 'USD':
                direction  = 'down' if currency_strong else 'up'
                confidence = 'Moderate'
                reason     = f'USD {"menguat" if currency_strong else "melemah"} → BTC biasanya {"turun" if currency_strong else "naik"}'
            else:
                continue   # BTC kurang sensitif ke non-USD data

        # ── Inflation boost ──────────────────────────────────────────
        if is_inflation and currency_strong and confidence == 'Strong':
            reason += ' + CPI tinggi → hawkish'

        if direction is None:
            continue

        conf_emoji = {'Strong': '🔴', 'Moderate': '🟡', 'Watch': '⚪'}.get(confidence, '⚪')
        results.append({
            'pair':       pair,
            'direction':  direction,
            'arrow':      '↑' if direction == 'up' else '↓',
            'confidence': confidence,
            'conf_emoji': conf_emoji,
            'reason':     reason,
        })

    order = {'Strong': 0, 'Moderate': 1, 'Watch': 2}
    results.sort(key=lambda x: order.get(x['confidence'], 3))
    return results


def _get_event_threshold(title):
    """Phase 1: Get deviation threshold for STRONG BEAT/MISS per event type."""
    title_lower = title.lower()
    for key, threshold in EVENT_THRESHOLDS.items():
        if key in title_lower:
            return threshold
    return EVENT_THRESHOLDS['default']


def _calc_beat_miss(actual_str, forecast_str, title):
    """
    Phase 1: Calculate beat/miss with magnitude.
    Returns: (sentiment_key, sentiment_label, sentiment_emoji, deviation, pct_deviation)
    sentiment_key: 'strong_beat' | 'beat' | 'inline' | 'miss' | 'strong_miss'
    """
    try:
        act_val  = float(re.sub(r'[^0-9.\-]', '', str(actual_str)))
        fore_val = float(re.sub(r'[^0-9.\-]', '', str(forecast_str)))
        deviation = act_val - fore_val
        threshold = _get_event_threshold(title)

        if abs(deviation) < 0.001:
            return 'inline', 'In Line with Forecast', '🟡', deviation, 0.0

        pct_dev = abs(deviation / fore_val * 100) if fore_val != 0 else 0.0
        is_better = deviation > 0

        if abs(deviation) >= threshold * 1.5:
            key   = 'strong_beat' if is_better else 'strong_miss'
            label = f'🔥 STRONG BEAT (+{deviation:.2f})' if is_better else f'💥 STRONG MISS ({deviation:.2f})' 
            emoji = '🟢' if is_better else '🔴'
        elif abs(deviation) >= threshold * 0.5:
            key   = 'beat' if is_better else 'miss'
            label = f'✅ Beat Forecast (+{deviation:.2f})' if is_better else f'❌ Miss Forecast ({deviation:.2f})'
            emoji = '🟢' if is_better else '🔴'
        else:
            return 'inline', 'Near In Line with Forecast', '🟡', deviation, pct_dev

        return key, label, emoji, deviation, pct_dev

    except Exception:
        return 'inline', 'Result Released', '📰', 0.0, 0.0


def _get_sentiment_multiplier(sentiment_key):
    """Map sentiment_key ke multiplier untuk Direction Predictor confidence."""
    return {
        'strong_beat': 1.0,
        'beat':        0.8,
        'inline':      0.0,
        'miss':        0.8,
        'strong_miss': 1.0,
    }.get(sentiment_key, 0.5)


def _calc_sentiment_score(event, sentiment_key, deviation, predictions, now):
    """
    Phase 5: Calculate Market Sentiment Score (0-100).
    Input: event, beat/miss key, deviation magnitude, predictions, current time.
    Output: (score, bias_label, bias_emoji)
    """
    if sentiment_key == 'inline':
        return 50, 'NEUTRAL', '⚖️'

    # Base score dari magnitude
    threshold  = _get_event_threshold(event['title'])
    magnitude  = min(abs(deviation) / (threshold * 2), 1.0)  # normalize 0-1
    base_score = 40 + magnitude * 40  # 40-80 range

    # Impact boost
    if event['impact'] == 'High':
        base_score += 10
    elif event['impact'] == 'Medium':
        base_score += 5

    # Session boost — NY & London lebih liquid
    hour = now.hour
    if SESSIONS['NY'][0] <= hour < SESSIONS['NY'][1]:
        base_score += 8    # NY session — paling liquid
    elif SESSIONS['London'][0] <= hour < SESSIONS['London'][1]:
        base_score += 5    # London session

    # Strong beat/miss boost
    if sentiment_key in ('strong_beat', 'strong_miss'):
        base_score += 5

    score = min(int(base_score), 100)
    is_bullish = sentiment_key in ('beat', 'strong_beat')

    if score >= 75:
        bias_label = 'STRONG BULLISH' if is_bullish else 'STRONG BEARISH'
        bias_emoji = '🟢🟢' if is_bullish else '🔴🔴'
    elif score >= 60:
        bias_label = 'BULLISH BIAS' if is_bullish else 'BEARISH BIAS'
        bias_emoji = '🟢' if is_bullish else '🔴'
    else:
        bias_label = 'MILD BULLISH' if is_bullish else 'MILD BEARISH'
        bias_emoji = '🟡'

    return score, bias_label, bias_emoji


def fmt_actual_result(event, now=None):
    if now is None:
        now = datetime.datetime.utcnow()

    pairs_str = ', '.join(event['affected_pairs'])
    actual    = event['actual']   if event['actual']   else '—'
    forecast  = event['forecast'] if event['forecast'] else '—'
    previous  = event['previous'] if event['previous'] else '—'

    # Phase 1: Enhanced beat/miss with magnitude
    sent_key, sent_label, sent_emoji, deviation, pct_dev = _calc_beat_miss(
        actual, forecast, event['title']
    )

    # Simplified sentiment key for direction predictor compatibility
    direction_sent = 'better' if sent_key in ('beat', 'strong_beat') else 'worse' if sent_key in ('miss', 'strong_miss') else 'inline'

    # Phase 1: Confidence multiplier berdasarkan magnitude
    conf_multiplier = _get_sentiment_multiplier(sent_key)

    # Direction Prediction
    direction_block = ''
    predictions = []
    if direction_sent != 'inline':
        predictions = _predict_pair_directions(
            currency    = event['currency'],
            sentiment   = direction_sent,
            event_title = event['title'],
        )
        if predictions:
            lines = []
            for p in predictions:
                # Phase 1: Strong beat/miss → upgrade confidence display
                conf_display = p['confidence']
                if conf_multiplier >= 1.0 and p['confidence'] == 'Moderate':
                    conf_display = 'Strong'
                conf_emoji_disp = {'Strong': '🔴', 'Moderate': '🟡', 'Watch': '⚪'}.get(conf_display, '⚪')
                lines.append(
                    f'  {conf_emoji_disp} {p["arrow"]} <b>{p["pair"]}</b>'
                    f'  <i>({conf_display})</i>'
                )
            direction_block = (
                f'{DIV}\n'
                f'🧭 <b>Prediksi Arah Pair:</b>\n'
                + '\n'.join(lines) + '\n'
                + f'<i>🔴 Strong  🟡 Moderate  ⚪ Watch</i>\n'
            )

    # Phase 5: Sentiment Score
    sentiment_block = ''
    if direction_sent != 'inline':
        score, bias_label, bias_emoji = _calc_sentiment_score(
            event, sent_key, deviation, predictions, now
        )
        sentiment_block = (
            f'{DIV}\n'
            f'📡 <b>Market Sentiment Score: {score}/100</b>\n'
            f'{bias_emoji} <b>{bias_label}</b>\n'
        )

    return (
        f'📰 <b>NEWS RESULT — {event["currency"]}</b>\n'
        f'{DIV}\n'
        f'{event["impact_emoji"]} <b>{event["title"]}</b>\n'
        f'{DIV}\n'
        f'🕐 Time     : {event["time_wib"]} WIB  ({event["time_utc"]} UTC)\n'
        f'📊 Impact   : {event["impact"]} {event["impact_emoji"]}\n'
        f'{DIV}\n'
        f'✅ Actual   : <b>{actual}</b>\n'
        f'🎯 Forecast : {forecast}\n'
        f'📈 Previous : {previous}\n'
        f'{DIV}\n'
        f'{sent_emoji} <b>{sent_label}</b>\n'
        f'💱 Affects  : <i>{pairs_str}</i>\n'
        f'{direction_block}'
        f'{sentiment_block}'
        f'{DIV}\n'
        f'<i>Bobb Market Intelligence v2.0</i>'
    )


def fmt_all_sources_failed(now):
    return (
        f'⚠️ <b>NEWS DETECTOR — SOURCE ERROR</b>\n'
        f'{DIV}\n'
        f'🕐 {(now + datetime.timedelta(hours=7)).strftime("%H:%M")} WIB  '
        f'({now.strftime("%H:%M")} UTC)\n'
        f'{DIV}\n'
        f'❌ Semua sumber data tidak dapat diakses:\n'
        f'   • FF-JSON\n'
        f'   • FF-HTML\n'
        f'   • MyFxBook\n'
        f'{DIV}\n'
        f'⚠️ Cek manual: forexfactory.com\n'
        f'<i>Bobb Market Intelligence v2.0</i>'
    )

# ════════════════════════════════════════
# BREAKING NEWS — RSS + NEWSAPI
# ════════════════════════════════════════
def _match_keywords(text):
    """
    Cek apakah text mengandung keyword dari BREAKING_KEYWORDS.
    - Keyword dengan spasi padding (' war ') = whole-word match
    - Keyword biasa = substring match
    - Special case: ' war ' tidak boleh didahului oleh kata konteks non-geopolitik
    Return: (group_name, matched_keyword) atau (None, None)
    """
    # Pad text dengan spasi supaya ' war ' bisa match di awal/akhir kalimat
    text_lower = ' ' + text.lower() + ' '

    # Blacklist prefix untuk keyword ' war ' — hindari false positive
    # Catatan: 'trade war' TIDAK dimasukkan di sini karena sudah jadi keyword sendiri
    # di group Macro — biarkan dia trigger sebagai 'trade war' keyword, bukan ' war '
    WAR_FALSE_POSITIVES = [
        'currency war', 'star wars', 'price war',
        'bidding war', 'talent war', 'wage war', 'turf war',
        'browser war', 'streaming war', 'at war with', 'at war over',
        'word war', 'drug war', 'format war', 'standards war',
    ]

    for group, keywords in BREAKING_KEYWORDS.items():
        for kw in keywords:
            if kw not in text_lower:
                continue

            # Special handling untuk keyword ' war '
            if kw == ' war ':
                # Cek apakah ini false positive
                is_fp = any(fp in text_lower for fp in WAR_FALSE_POSITIVES)
                if is_fp:
                    continue

            return group, kw.strip()

    return None, None

def _news_hash(title):
    """Buat hash pendek dari judul berita untuk dedup."""
    import hashlib
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]

def _parse_pubdate(date_str):
    """
    Parse pubDate dari RSS feed ke datetime UTC.
    Support format: RFC 2822 (standard RSS) dan ISO 8601.
    Return datetime atau None kalau gagal parse.
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # Format RFC 2822: "Sun, 21 Jun 2026 10:21:00 +0000" atau "Sun, 21 Jun 2026 10:21:00 GMT"
    rfc_fmts = [
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S GMT',
        '%d %b %Y %H:%M:%S %z',
        '%d %b %Y %H:%M:%S GMT',
    ]
    for fmt in rfc_fmts:
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            # Konversi ke UTC naive
            if dt.tzinfo is not None:
                dt = dt - dt.utcoffset()
                dt = dt.replace(tzinfo=None)
            return dt
        except Exception:
            continue

    # Format ISO 8601: "2026-06-21T10:21:00Z" atau "2026-06-21T10:21:00+00:00"
    try:
        dt = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt - dt.utcoffset()
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        pass

    return None


def _parse_rss_xml(xml_text, source_name):
    """Parse RSS XML dan return list of (title, description, pub_date, link)."""
    items = []
    try:
        # Extract <item> blocks
        item_blocks = re.findall(r'<item[^>]*>(.*?)</item>', xml_text, re.DOTALL | re.IGNORECASE)
        for block in item_blocks:
            title_m = re.search(r'<title[^>]*><!\[CDATA\[(.*?)\]\]>|<title[^>]*>(.*?)</title>', block, re.DOTALL | re.IGNORECASE)
            desc_m  = re.search(r'<description[^>]*><!\[CDATA\[(.*?)\]\]>|<description[^>]*>(.*?)</description>', block, re.DOTALL | re.IGNORECASE)
            date_m  = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', block, re.DOTALL | re.IGNORECASE)
            link_m  = re.search(r'<link[^>]*>(.*?)</link>|<link>(.*?)</link>', block, re.DOTALL | re.IGNORECASE)

            title = clean_val(title_m.group(1) or title_m.group(2)) if title_m else ''
            desc  = clean_val(desc_m.group(1)  or desc_m.group(2))  if desc_m  else ''
            date  = clean_val(date_m.group(1))  if date_m  else ''
            link  = clean_val(link_m.group(1)   or (link_m.group(2) if link_m and len(link_m.groups()) > 1 else '')) if link_m else ''

            if title:
                items.append({
                    'title':   title,
                    'desc':    desc,
                    'date':    date,
                    'link':    link,
                    'source':  source_name,
                })
    except Exception as e:
        print(f'[RSS_PARSE] {source_name} error: {e}')
    return items

def fetch_rss_breaking(now):
    """
    Fetch berita dari semua RSS feeds.
    Filter: hanya berita dalam RSS_MAX_AGE_HOURS terakhir.
    Berita tanpa pubDate tetap diproses (biar tidak miss), tapi ditandai.
    """
    all_items = []
    cutoff = now - datetime.timedelta(hours=RSS_MAX_AGE_HOURS)

    for source_name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                print(f'[RSS] {source_name}: HTTP {resp.status_code}')
                continue

            items = _parse_rss_xml(resp.text, source_name)
            fresh = 0
            stale = 0
            no_date = 0

            for item in items:
                pub_dt = _parse_pubdate(item.get('date', ''))
                if pub_dt is None:
                    # Tidak ada pubDate — tetap masukkan tapi tandai
                    item['pub_dt'] = None
                    all_items.append(item)
                    no_date += 1
                elif pub_dt >= cutoff:
                    item['pub_dt'] = pub_dt
                    all_items.append(item)
                    fresh += 1
                else:
                    stale += 1

            print(f'[RSS] {source_name}: {fresh} fresh, {stale} stale skipped, {no_date} no-date')

        except Exception as e:
            print(f'[RSS] {source_name} error: {e}')

    return all_items

def fetch_newsapi_breaking(now):
    """Fetch berita dari NewsAPI.org — fallback kalau RSS semua gagal."""
    if not NEWSAPI_KEY:
        print('[NEWSAPI] No key configured, skipping')
        return []
    try:
        # Query gabungan keyword terpenting
        q = 'war OR sanctions OR "federal reserve" OR "rate hike" OR gold OR bitcoin OR crash OR recession'
        url = (
            f'https://newsapi.org/v2/everything?'
            f'q={requests.utils.quote(q)}&'
            f'language=en&'
            f'sortBy=publishedAt&'
            f'pageSize=20&'
            f'apiKey={NEWSAPI_KEY}'
        )
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f'[NEWSAPI] HTTP {resp.status_code}')
            return []

        data  = resp.json()
        items = []
        for art in data.get('articles', []):
            items.append({
                'title':  art.get('title', ''),
                'desc':   art.get('description', ''),
                'date':   art.get('publishedAt', ''),
                'link':   art.get('url', ''),
                'source': art.get('source', {}).get('name', 'NewsAPI'),
            })
        print(f'[NEWSAPI] {len(items)} articles')
        return items
    except Exception as e:
        print(f'[NEWSAPI] Error: {e}')
        return []


# ════════════════════════════════════════
# PHASE 4 — REDDIT RSS + EXTRA FEEDS
# ════════════════════════════════════════
def fetch_reddit_rss(now):
    """
    Phase 4: Fetch breaking news dari Reddit RSS (economics/investing/worldnews)
    + Al Jazeera English. Gratis, no API key, update frequent.
    """
    all_items = []
    cutoff    = now - datetime.timedelta(hours=RSS_MAX_AGE_HOURS)
    all_feeds = REDDIT_RSS_FEEDS + EXTRA_RSS_FEEDS

    for source_name, url in all_feeds:
        try:
            headers_reddit = dict(HEADERS)
            # Reddit butuh custom user agent yang jelas — sebagian endpoint
            # tetap bisa 403 kapan aja, itu di luar kontrol kita (fail-safe di bawah)
            if 'reddit' in url.lower():
                headers_reddit['User-Agent'] = 'BobbMarketBot/2.1 (financial news aggregator)'
            resp = requests.get(url, headers=headers_reddit, timeout=10)
            if resp.status_code != 200:
                print(f'[REDDIT_RSS] {source_name}: HTTP {resp.status_code}')
                continue

            items = _parse_rss_xml(resp.text, source_name)
            # Reddit RSS pakai <updated> bukan <pubDate> — coba keduanya
            fresh = 0
            for item in items:
                pub_dt = _parse_pubdate(item.get('date', ''))
                if pub_dt is None or pub_dt >= cutoff:
                    item['pub_dt'] = pub_dt
                    all_items.append(item)
                    fresh += 1
            print(f'[REDDIT_RSS] {source_name}: {fresh} items')

        except Exception as e:
            print(f'[REDDIT_RSS] {source_name} error: {e}')

    return all_items


def process_breaking_news(now, state):
    """
    Main breaking news processor:
    1. Fetch dari RSS (primary) + NewsAPI (fallback)
    2. Filter pubDate — skip berita > RSS_MAX_AGE_HOURS jam
    3. Filter by keyword (whole-word aware)
    4. Dedup via hash (7 hari) + cooldown per group (4 jam)
    5. Kirim ke Telegram, max 2 per run
    """
    print('[BREAKING] Fetching breaking news...')

    # Fetch
    # Phase 4: Fetch dari semua RSS sources (core + Reddit + AlJazeera)
    items = fetch_rss_breaking(now)
    reddit_items = fetch_reddit_rss(now)
    items.extend(reddit_items)

    if not items:
        print('[BREAKING] All RSS empty, trying NewsAPI...')
        items = fetch_newsapi_breaking(now)

    if not items:
        print('[BREAKING] No items from any source')
        return

    print(f'[BREAKING] Total items to scan: {len(items)}')
    sent_count = 0

    for item in items:
        try:
            title = item.get('title', '')
            desc  = item.get('desc',  '')
            if not title:
                continue

            # Match keyword
            full_text      = f'{title} {desc}'
            group, matched = _match_keywords(full_text)
            if not group:
                continue

            # Dedup check
            h = _news_hash(title)
            if state['sent_breaking'].get(h):
                continue

            # Cooldown check per group — jangan spam satu topik
            group_key      = f'cooldown_{re.sub(r"[^a-z]","",group.lower())}'
            last_sent_str  = state['sent_breaking'].get(group_key, '')
            if last_sent_str:
                try:
                    last_sent = datetime.datetime.fromisoformat(last_sent_str)
                    hours_ago = (now - last_sent).total_seconds() / 3600
                    if hours_ago < BREAKING_COOLDOWN_HOURS:
                        print(f'[BREAKING] Cooldown active for {group} ({hours_ago:.1f}h ago)')
                        continue
                except Exception:
                    pass

            # Kirim
            msg = fmt_breaking_news(item, group, matched, now)
            r   = send_text(msg)
            if r.get('ok'):
                state['sent_breaking'][h]          = now.isoformat()
                state['sent_breaking'][group_key]  = now.isoformat()
                sent_count += 1
                print(f'[BREAKING] ✅ Sent: {title[:60]}')
                # Max 2 breaking news per run — hindari spam
                if sent_count >= 2:
                    break
            else:
                print(f'[BREAKING] ❌ Failed: {r}')

        except Exception as e:
            print(f'[BREAKING] Item error: {e}')

    # Cleanup sent_breaking:
    # - Hash berita (dedup): simpan 7 hari supaya berita sama tidak re-trigger
    # - Cooldown key: hapus kalau sudah expired (> BREAKING_COOLDOWN_HOURS)
    cutoff_7d = (now - datetime.timedelta(days=7)).isoformat()
    cutoff_cd = (now - datetime.timedelta(hours=BREAKING_COOLDOWN_HOURS)).isoformat()
    state['sent_breaking'] = {
        k: v for k, v in state['sent_breaking'].items()
        if (k.startswith('cooldown_') and v >= cutoff_cd)
        or (not k.startswith('cooldown_') and v >= cutoff_7d)
    }

    print(f'[BREAKING] Done — {sent_count} sent')

def fmt_breaking_news(item, group, matched_kw, now):
    """Format pesan breaking news ke Telegram — full detail, no truncation."""
    title  = item.get('title',  'N/A')
    desc   = item.get('desc',   '').strip()
    source = item.get('source', 'Unknown')
    link   = item.get('link',   '')

    # Bersihkan HTML tags dari desc kalau ada
    desc = re.sub(r'<[^>]+>', '', desc).strip()

    time_wib  = (now + datetime.timedelta(hours=7)).strftime('%H:%M')
    time_utc  = now.strftime('%H:%M')
    date_str  = (now + datetime.timedelta(hours=7)).strftime('%d %b %Y')

    # Tentukan dampak ke market
    market_impact = _assess_market_impact(matched_kw, group)

    desc_block = f'\n📝 <b>Detail:</b>\n{desc}\n' if desc else '\n'
    link_line  = f'\n🔗 <a href="{link}">Baca selengkapnya → {source}</a>' if link else ''

    return (
        f'🚨 <b>BREAKING NEWS</b>\n'
        f'{DIV2}\n'
        f'{group}\n'
        f'{DIV}\n'
        f'📰 <b>{title}</b>\n'
        f'{desc_block}'
        f'{DIV}\n'
        f'🕐 <b>{time_wib} WIB</b>  ({time_utc} UTC)  {date_str}\n'
        f'📡 Sumber   : {source}\n'
        f'🔍 Keyword  : <i>{matched_kw}</i>\n'
        f'{DIV}\n'
        f'📊 <b>Potensi Dampak Market:</b>\n'
        f'{market_impact}\n'
        f'{link_line}\n'
        f'{DIV2}\n'
        f'⚠️ <b>Monitor pergerakan harga dengan cermat!</b>\n'
        f'<i>Bobb Market Intelligence v2.0</i>'
    )

def _assess_market_impact(keyword, group):
    """Buat analisis singkat dampak ke market berdasarkan keyword."""
    impacts = {
        # Geopolitical
        'war':        '🔴 XAUUSD ↑ (safe haven)  |  Risk assets ↓\n   USD bisa menguat, equity sell-off',
        'attack':     '🔴 XAUUSD ↑ (safe haven)  |  Oil ↑ kemungkinan\n   Risk-off sentiment',
        'missile':    '🔴 XAUUSD ↑  |  Oil ↑  |  JPY ↑ (safe haven)\n   Equity markets volatile',
        'invasion':   '🔴 XAUUSD ↑↑  |  Energy crisis risk\n   EUR bisa melemah tergantung lokasi',
        'sanctions':  '🟡 Bergantung target negara\n   Commodity terdampak jika Russia/OPEC',
        'nuclear':    '🔴 EXTREME risk-off — XAUUSD ↑↑  |  Semua risk assets ↓↓',
        'ceasefire':  '🟢 Risk-on — Equity ↑  |  XAUUSD mungkin koreksi\n   Oil bisa turun',
        'coup':       '🔴 Currency negara terdampak ↓  |  XAUUSD ↑\n   Regional contagion risk',
        'explosion':  '🔴 Risk-off sementara  |  Monitor lokasi kejadian',
        'terrorism':  '🔴 Risk-off sementara  |  XAUUSD ↑  |  JPY ↑',
        'crisis':     '🟡 Bergantung konteks  |  Safe haven assets ↑',
        'escalation': '🔴 Risk-off  |  XAUUSD ↑  |  Oil ↑ jika Middle East',
        'military':   '🟡 Monitor perkembangan  |  Potensi risk-off',
        'troops':     '🟡 Monitor perkembangan  |  Potensi risk-off',
        # Central Bank
        'fed':            '🔴/🟢 Bergantung tone hawkish/dovish\n   USD & semua pair terdampak langsung',
        'federal reserve':'🔴/🟢 Market mover terbesar\n   XAUUSD, USD pairs, semua aset terdampak',
        'fomc':           '🔴 High impact — semua pair volatile\n   Hindari entry 30 menit sebelum & sesudah',
        'rate hike':      '🔴 USD ↑  |  XAUUSD ↓  |  Equity mixed\n   Bond yields ↑',
        'rate cut':       '🟢 USD ↓  |  XAUUSD ↑  |  Equity ↑\n   Risk-on sentiment',
        'ecb':            '🟡 EUR pairs volatile  |  EURUSD high impact',
        'boe':            '🟡 GBP pairs volatile  |  GBPUSD high impact',
        'boj':            '🟡 JPY pairs volatile  |  USDJPY high impact',
        'powell':         '🔴 High impact — semua USD pair volatile',
        'lagarde':        '🟡 EUR pairs volatile',
        'inflation target':'🟡 Monitor — implikasi ke rate decision',
        'monetary policy':'🟡 Currency terdampak sesuai bank sentral',
        'quantitative':   '🟡 Liquidity impact — equity & bond terdampak',
        # Market Moving
        'crash':       '🔴 EXTREME — semua aset volatile\n   XAUUSD ↑↑  |  BTC bisa turun atau naik',
        'collapse':    '🔴 Risk-off ekstrem  |  Safe haven ↑↑',
        'default':     '🔴 Currency negara terdampak ↓↓  |  Contagion risk',
        'recession':   '🔴 Risk-off  |  XAUUSD ↑  |  Commodity ↓\n   Safe haven currencies ↑',
        'bank failure':'🔴 Sector contagion risk  |  XAUUSD ↑\n   Monitor bank-related currency',
        'bailout':     '🟡 Short-term relief  |  Inflation concern jangka panjang',
        'downgrade':   '🔴 Currency negara terdampak ↓  |  Bond yields ↑',
        'emergency':   '🔴 Risk-off  |  Monitor konteks',
        # Gold & Oil
        'gold':   '🟡 XAUUSD langsung terdampak\n   Monitor level support/resistance key',
        'xauusd': '🟡 Direct impact  |  Watch technicals',
        'oil':    '🟡 CAD ↑/↓  |  NOK terdampak  |  Inflation concern',
        'crude':  '🟡 Energy sector & CAD terdampak',
        'opec':   '🟡 Oil price direct impact  |  CAD, energy stocks',
        # Crypto
        'bitcoin': '🟡 BTC/BTCUSDT direct impact\n   Crypto market sentiment terdampak',
        'btc':     '🟡 BTCUSDT direct impact',
        'sec':     '🟡 Crypto regulation risk  |  BTC volatile',
        'etf approval': '🟢 BTC ↑↑  |  Crypto risk-on',
        'exchange hack':'🔴 BTC ↓  |  Crypto panic sell risk',
        # Macro
        'gdp':         '🟡 Currency negara terdampak  |  Risk sentiment',
        'nonfarm':     '🔴 USD pairs volatile  |  XAUUSD terdampak\n   Major market mover',
        'cpi':         '🔴 Inflation data — USD & rate expectation\n   XAUUSD, semua USD pair terdampak',
        'trade war':   '🔴 Risk-off  |  CNY/AUD terdampak  |  Gold ↑',
        'tariff':      '🟡 Currency pair terdampak sesuai negara',
        'treasury':    '🟡 USD & bond market terdampak',
        'yield curve': '🟡 Rate expectation  |  USD & equity terdampak',
    }

    # Cari match di dict
    kw_lower = keyword.lower()
    for kw, impact in impacts.items():
        if kw in kw_lower or kw_lower in kw:
            return impact

    # Default berdasarkan group
    group_defaults = {
        '⚔️ Geopolitical': '🔴 Risk-off kemungkinan  |  XAUUSD & JPY ↑',
        '🏦 Central Bank':  '🔴 USD pairs & XAUUSD volatile',
        '📈 Market Moving': '🔴 Semua aset volatile — waspada',
        '🥇 Gold & Oil':    '🟡 XAUUSD & energy terdampak',
        '₿ Crypto':         '🟡 BTCUSDT & crypto volatile',
        '🌍 Macro':         '🟡 Currency & commodity terdampak',
    }
    return group_defaults.get(group, '🟡 Monitor pergerakan harga')


# ════════════════════════════════════════
# PRICE SPIKE DETECTOR
# ════════════════════════════════════════
def _fetch_btc_price():
    """
    Fetch BTCUSDT harga sekarang dan 5 menit lalu via Binance public API.
    Return: (price_now, price_5m_ago) atau (None, None) kalau gagal.
    """
    try:
        # Kline endpoint — 1m candle, ambil 6 candle terakhir
        url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=6'
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f'[SPIKE] Binance HTTP {resp.status_code}')
            return None, None
        klines = resp.json()
        if len(klines) < 2:
            return None, None
        # klines[-1] = candle terbaru, index [4] = close price
        price_now   = float(klines[-1][4])
        price_5m    = float(klines[0][4])   # 5-6 menit lalu
        return price_now, price_5m
    except Exception as e:
        print(f'[SPIKE] BTC fetch error: {e}')
        return None, None




def _fetch_yahoo_price(ticker):
    """
    Phase 3: Yahoo Finance — fallback untuk forex & primary untuk XAUUSD (GC=F).
    Return: (price_now, price_5m_ago) atau (None, None) kalau gagal.
    """
    try:
        url = (
            f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
            f'?interval=1m&range=30m'
        )
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f'[SPIKE] Yahoo {ticker} HTTP {resp.status_code}')
            return None, None

        data   = resp.json()
        result = data.get('chart', {}).get('result', [])
        if not result:
            return None, None

        closes = result[0].get('indicators', {}).get('quote', [{}])[0].get('close', [])
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None, None

        price_now = closes[-1]
        price_5m  = closes[-6] if len(closes) >= 6 else closes[0]
        return price_now, price_5m
    except Exception as e:
        print(f'[SPIKE] Yahoo {ticker} error: {e}')
        return None, None


def _calc_spike(price_now, price_5m):
    """Hitung % perubahan. Return float atau None."""
    try:
        if price_5m == 0:
            return None
        return ((price_now - price_5m) / price_5m) * 100
    except Exception:
        return None


def fmt_spike_alert(pair, price_now, price_5m, pct_change):
    """Format pesan spike alert ke Telegram."""
    direction  = '📈 BULLISH SPIKE' if pct_change > 0 else '📉 BEARISH SPIKE'
    arrow      = '↑' if pct_change > 0 else '↓'
    abs_pct    = abs(pct_change)
    now        = datetime.datetime.utcnow()
    time_wib   = (now + datetime.timedelta(hours=7)).strftime('%H:%M')
    time_utc   = now.strftime('%H:%M')
    date_str   = (now + datetime.timedelta(hours=7)).strftime('%d %b %Y')

    # Format harga sesuai pair
    if pair in ('BTCUSDT',):
        fmt_price = lambda p: f'${p:,.2f}'
    elif pair == 'XAUUSD':
        fmt_price = lambda p: f'${p:,.2f}'
    elif 'JPY' in pair:
        fmt_price = lambda p: f'{p:.3f}'
    else:
        fmt_price = lambda p: f'{p:.5f}'

    abs_move = abs(price_now - price_5m)
    move_str = fmt_price(abs_move) if pair not in ('EURUSD','GBPUSD','AUDUSD','USDCAD','USDCHF','NZDUSD','EURJPY','GBPJPY') \
               else f'{abs_move:.5f}'

    return (
        f'⚡ <b>PRICE SPIKE ALERT</b>\n'
        f'{DIV2}\n'
        f'{direction}\n'
        f'{DIV}\n'
        f'💱 <b>{pair}</b>  {arrow} <b>{abs_pct:.2f}%</b> dalam 5 menit\n'
        f'{DIV}\n'
        f'💰 Harga Sekarang : <b>{fmt_price(price_now)}</b>\n'
        f'📌 5 Menit Lalu   : {fmt_price(price_5m)}\n'
        f'📊 Pergerakan     : {arrow} {move_str}\n'
        f'{DIV}\n'
        f'🕐 <b>{time_wib} WIB</b>  ({time_utc} UTC)  {date_str}\n'
        f'{DIV}\n'
        f'⚠️ <b>Cek chart & konfirmasi sebelum entry!</b>\n'
        f'<i>Bobb Market Intelligence v2.0</i>'
    )


def process_price_spikes(now, state):
    """
    Cek semua pair untuk spike > threshold dalam 5 menit.
    Kirim alert ke Telegram dengan cooldown 30 menit per pair.
    """
    print('[SPIKE] Checking price spikes...')

    if 'sent_spike' not in state:
        state['sent_spike'] = {}

    spike_sent = 0

    for pair, threshold in SPIKE_THRESHOLDS.items():
        try:
            # Cooldown check
            spike_key  = f'spike_{pair}'
            last_str   = state['sent_spike'].get(spike_key, '')
            if last_str:
                try:
                    last_dt   = datetime.datetime.fromisoformat(last_str)
                    mins_ago  = (now - last_dt).total_seconds() / 60
                    if mins_ago < SPIKE_COOLDOWN_MINUTES:
                        print(f'[SPIKE] {pair}: cooldown ({mins_ago:.0f}m ago)')
                        continue
                except Exception:
                    pass

            # Fetch harga — v2.1: BTC via Binance, semua forex + XAUUSD via Yahoo
            # (exchangerate-api dihapus — cuma refresh 1x/24jam, gak cocok utk spike 5m)
            if pair == 'BTCUSDT':
                price_now, price_5m = _fetch_btc_price()
            else:
                ticker = YAHOO_TICKERS.get(pair)
                if not ticker:
                    continue
                price_now, price_5m = _fetch_yahoo_price(ticker)

            if price_now is None or price_5m is None:
                print(f'[SPIKE] {pair}: no data')
                continue

            pct = _calc_spike(price_now, price_5m)
            if pct is None:
                continue

            print(f'[SPIKE] {pair}: {pct:+.3f}% (threshold ±{threshold}%)')

            if abs(pct) >= threshold:
                msg = fmt_spike_alert(pair, price_now, price_5m, pct)
                r   = send_text(msg)
                if r.get('ok'):
                    state['sent_spike'][spike_key] = now.isoformat()
                    spike_sent += 1
                    print(f'[SPIKE] ✅ Alert sent: {pair} {pct:+.2f}%')
                else:
                    print(f'[SPIKE] ❌ Send failed: {r}')

        except Exception as e:
            print(f'[SPIKE] {pair} error: {e}')

    # Cleanup spike state > 2 jam
    cutoff_2h = (now - datetime.timedelta(hours=2)).isoformat()
    state['sent_spike'] = {
        k: v for k, v in state['sent_spike'].items()
        if v >= cutoff_2h
    }

    print(f'[SPIKE] Done — {spike_sent} alerts sent')


# ════════════════════════════════════════
def run_news_detector():
    now   = datetime.datetime.utcnow()
    state = load_state()

    print(f'=== BOBB MARKET INTELLIGENCE v2.0 ===')
    print(f'Time UTC : {now.strftime("%Y-%m-%d %H:%M")}')
    print(f'Time WIB : {(now + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")}')

    # ── Fetch events — SEKALI saja, dipakai semua mode ──────────────────
    events, source = fetch_events(now.date())
    print(f'[FETCH] Source used: {source}')

    # ── MODE 1: Daily Briefing — 07:00 WIB = 00:00 UTC ──────────────
    # Window 59 menit (00:00–00:58 UTC) — toleransi GitHub Actions scheduler delay
    # Anti-duplikat dijaga sent_daily[date_key] — aman dari double-send
    is_briefing = (now.hour == 0 and now.minute < 59)
    date_key    = now.strftime('%Y-%m-%d')

    if is_briefing and not state['sent_daily'].get(date_key, False):
        print('[DAILY] Sending morning briefing...')
        if source == 'None':
            msg = fmt_all_sources_failed(now)
        else:
            msg = fmt_daily_briefing(events, now, source)
        r = send_text(msg)
        if r.get('ok'):
            state['sent_daily'][date_key] = True
            print('[DAILY] ✅ Sent')
        else:
            print(f'[DAILY] ❌ {r}')

    # ── MODE 2: Reminder 30 menit sebelum ───────────────────────────
    # Window 15–45 menit — toleransi GitHub Actions delay hingga 15 menit
    # Anti-duplikat dijaga sent_reminder[reminder_key]
    for event in events:
        mins_until   = (event['dt_utc'] - now).total_seconds() / 60
        reminder_key = f'reminder_{event["id"]}'
        if 15 <= mins_until <= 45 and not state['sent_reminder'].get(reminder_key, False):
            print(f'[REMINDER] {event["currency"]} {event["title"]} ~{int(mins_until)}m')
            r = send_text(fmt_reminder(event, 30))
            if r.get('ok'):
                state['sent_reminder'][reminder_key] = now.isoformat()
                print('[REMINDER] ✅ Sent')

    # ── MODE 3: Actual Result setelah rilis ─────────────────────────
    # Window 5–45 menit — toleransi GitHub Actions delay hingga 20 menit
    # v2.1: cross-check investing.com dihapus (rawan blocked Cloudflare) —
    # actual murni dari FF-JSON/FF-HTML/MyFxBook, skip kalau masih kosong
    for event in events:
        mins_past  = (now - event['dt_utc']).total_seconds() / 60
        actual_key = f'actual_{event["id"]}'
        if not (5 <= mins_past <= 45):
            continue
        if state['sent_actual'].get(actual_key, False):
            continue

        if not event['actual']:
            print(f'[ACTUAL] Skip — actual masih kosong utk {event["currency"]} {event["title"]}')
            continue

        print(f'[ACTUAL] {event["currency"]} {event["title"]} → {event["actual"]}')
        r = send_text(fmt_actual_result(event, now))
        if r.get('ok'):
            state['sent_actual'][actual_key] = now.isoformat()
            print('[ACTUAL] ✅ Sent')

    # ── MODE 4: Breaking News ────────────────────────────────────────
    process_breaking_news(now, state)

    # ── MODE 5: Price Spike Detector ────────────────────────────────
    process_price_spikes(now, state)

    # ── Cleanup state ────────────────────────────────────────────────
    # sent_daily: simpan 7 hari
    cutoff_7d = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    state['sent_daily'] = {k: v for k, v in state['sent_daily'].items() if k >= cutoff_7d}

    # sent_reminder & sent_actual: simpan 2 hari — event ID mengandung tanggal implisit
    # key format: "reminder_USD_CPI_1230" — cukup 2 hari untuk safety margin
    cutoff_2d = (now - datetime.timedelta(days=2)).isoformat()
    # Simpan kalau timestamp value-nya masih dalam 2 hari (nilai True = old format, hapus saja)
    state['sent_reminder'] = {
        k: v for k, v in state['sent_reminder'].items()
        if v is not True  # hapus format lama (boolean True)
        and isinstance(v, str) and v >= cutoff_2d
    }
    state['sent_actual'] = {
        k: v for k, v in state['sent_actual'].items()
        if v is not True
        and isinstance(v, str) and v >= cutoff_2d
    }

    save_state(state)
    print('=== DONE ===')

if __name__ == '__main__':
    run_news_detector()
