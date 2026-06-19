import datetime, requests, json, os, re
import warnings
warnings.filterwarnings('ignore')

# ════════════════════════════════════════
# VERSION
# ════════════════════════════════════════
VERSION = 'v1.3'
# v1.0 — Initial standalone news detector
# v1.1 — Dual source FF scraping + MyFxBook fallback
# v1.2 — Primary: FF JSON (nfs.faireconomy.media) — lebih stabil dari scraping
#         Fallback 1: FF HTML scraping
#         Fallback 2: MyFxBook scraping
#         Format waktu WIB + UTC semua pesan
# v1.3 — Breaking News module: RSS Feed (primary) + NewsAPI (fallback)
#         Keyword filter otomatis: war, sanctions, Fed, rate, oil, gold, crypto, dll
#         Anti-duplikat via state, cooldown 4 jam per keyword group

# ════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════
BOT_TOKEN   = '8722556278:AAEs5_W4RuFaZQEkhVQnXcKRMNmFGvSjq9k'  # BobbInfinityCore_bot
CHAT_ID     = '680378702'
NEWSAPI_KEY = ''   # Opsional — isi kalau punya key dari newsapi.org (free tier)

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'bobb_news_state.json'
)

# ── Breaking News Keywords ────────────────────────────────────────────
# Dikelompokkan per tema — kalau salah satu keyword match, berita dikirim
BREAKING_KEYWORDS = {
    '⚔️ Geopolitical': [
        'war', 'attack', 'missile', 'airstrike', 'invasion', 'conflict',
        'troops', 'nuclear', 'sanctions', 'ceasefire', 'explosion', 'coup',
        'terrorism', 'crisis', 'escalation', 'military',
    ],
    '🏦 Central Bank': [
        'fed', 'federal reserve', 'fomc', 'rate hike', 'rate cut',
        'interest rate', 'ecb', 'boe', 'boj', 'rba', 'monetary policy',
        'quantitative', 'powell', 'lagarde', 'inflation target',
    ],
    '📈 Market Moving': [
        'emergency', 'crash', 'collapse', 'default', 'recession',
        'bank failure', 'circuit breaker', 'halt', 'bailout',
        'debt ceiling', 'downgrade', 'credit rating',
    ],
    '🥇 Gold & Oil': [
        'gold', 'xauusd', 'oil', 'crude', 'opec', 'petroleum',
        'commodity', 'energy crisis', 'supply cut',
    ],
    '₿ Crypto': [
        'bitcoin', 'btc', 'ethereum', 'crypto', 'sec', 'etf approval',
        'exchange hack', 'stablecoin', 'cbdc',
    ],
    '🌍 Macro': [
        'gdp', 'unemployment', 'nonfarm', 'cpi', 'ppi', 'trade war',
        'tariff', 'dollar', 'treasury', 'yield curve', 'bond',
    ],
}

# Cooldown — jangan kirim berita dari group yang sama dalam X jam
BREAKING_COOLDOWN_HOURS = 4

# RSS Feeds — gratis, tidak perlu API key
RSS_FEEDS = [
    ('Reuters',   'https://feeds.reuters.com/reuters/businessNews'),
    ('Reuters',   'https://feeds.reuters.com/reuters/topNews'),
    ('BBC',       'https://feeds.bbci.co.uk/news/business/rss.xml'),
    ('Bloomberg', 'https://feeds.bloomberg.com/markets/news.rss'),
    ('CNBC',      'https://www.cnbc.com/id/10000664/device/rss/rss.html'),
    ('MarketWatch','https://feeds.marketwatch.com/marketwatch/topstories/'),
]

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
            f'<b>Bobb Market Intelligence v1.3</b>\n'
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
        f'<b>Bobb Market Intelligence v1.3</b>\n'
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
        f'<i>Bobb Market Intelligence v1.3</i>'
    )


def fmt_actual_result(event):
    pairs_str = ', '.join(event['affected_pairs'])
    actual    = event['actual']   if event['actual']   else '—'
    forecast  = event['forecast'] if event['forecast'] else '—'
    previous  = event['previous'] if event['previous'] else '—'

    sentiment       = 'Result released'
    sentiment_emoji = '📰'
    try:
        act_val  = float(re.sub(r'[^0-9.\-]', '', str(actual)))
        fore_val = float(re.sub(r'[^0-9.\-]', '', str(forecast)))
        if act_val > fore_val:
            sentiment = 'Better than forecast'
            sentiment_emoji = '🟢'
        elif act_val < fore_val:
            sentiment = 'Worse than forecast'
            sentiment_emoji = '🔴'
        else:
            sentiment = 'In line with forecast'
            sentiment_emoji = '🟡'
    except Exception:
        pass

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
        f'{sentiment_emoji} <b>{sentiment}</b>\n'
        f'💱 Affects  : <i>{pairs_str}</i>\n'
        f'{DIV}\n'
        f'<i>Bobb Market Intelligence v1.3</i>'
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
        f'<i>Bobb Market Intelligence v1.3</i>'
    )

# ════════════════════════════════════════
# BREAKING NEWS — RSS + NEWSAPI
# ════════════════════════════════════════
def _match_keywords(text):
    """
    Cek apakah text mengandung keyword dari BREAKING_KEYWORDS.
    Return: (group_name, matched_keyword) atau (None, None)
    """
    text_lower = text.lower()
    for group, keywords in BREAKING_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return group, kw
    return None, None

def _news_hash(title):
    """Buat hash pendek dari judul berita untuk dedup."""
    import hashlib
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]

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
    """Fetch berita dari semua RSS feeds."""
    all_items = []
    for source_name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                items = _parse_rss_xml(resp.text, source_name)
                all_items.extend(items)
                print(f'[RSS] {source_name}: {len(items)} items')
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

def process_breaking_news(now, state):
    """
    Main breaking news processor:
    1. Fetch dari RSS (primary) + NewsAPI (fallback)
    2. Filter by keyword
    3. Dedup via hash + cooldown
    4. Kirim ke Telegram
    """
    print('[BREAKING] Fetching breaking news...')

    # Fetch
    items = fetch_rss_breaking(now)
    if not items:
        print('[BREAKING] RSS empty, trying NewsAPI...')
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

    # Cleanup sent_breaking > 24 jam
    cutoff_ts = (now - datetime.timedelta(hours=24)).isoformat()
    state['sent_breaking'] = {
        k: v for k, v in state['sent_breaking'].items()
        if v >= cutoff_ts or k.startswith('cooldown_')
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
        f'<i>Bobb Market Intelligence v1.3</i>'
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
def run_news_detector():
    now   = datetime.datetime.utcnow()
    state = load_state()

    print(f'=== BOBB MARKET INTELLIGENCE v1.3 ===')
    print(f'Time UTC : {now.strftime("%Y-%m-%d %H:%M")}')
    print(f'Time WIB : {(now + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M")}')

    # ── Fetch events ─────────────────────────────────────────────────
    events, source = fetch_events(now.date())
    print(f'[FETCH] Source used: {source}')

    # ── MODE 1: Daily Briefing — 07:00 WIB = 00:00 UTC ──────────────
    is_briefing = (now.hour == 0 and now.minute < 15)
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
    for event in events:
        mins_until   = (event['dt_utc'] - now).total_seconds() / 60
        reminder_key = f'reminder_{event["id"]}'
        if 25 <= mins_until <= 35 and not state['sent_reminder'].get(reminder_key, False):
            print(f'[REMINDER] {event["currency"]} {event["title"]} ~{int(mins_until)}m')
            r = send_text(fmt_reminder(event, 30))
            if r.get('ok'):
                state['sent_reminder'][reminder_key] = True
                print('[REMINDER] ✅ Sent')

    # ── MODE 3: Actual Result setelah rilis ─────────────────────────
    # Re-fetch untuk actual terbaru
    events_fresh, _ = fetch_events(now.date())
    for event in events_fresh:
        mins_past  = (now - event['dt_utc']).total_seconds() / 60
        actual_key = f'actual_{event["id"]}'
        if 5 <= mins_past <= 25 and event['actual'] and not state['sent_actual'].get(actual_key, False):
            print(f'[ACTUAL] {event["currency"]} {event["title"]} → {event["actual"]}')
            r = send_text(fmt_actual_result(event))
            if r.get('ok'):
                state['sent_actual'][actual_key] = True
                print('[ACTUAL] ✅ Sent')

    # ── MODE 4: Breaking News ────────────────────────────────────────
    process_breaking_news(now, state)

    # ── Cleanup state > 7 hari ──────────────────────────────────────
    cutoff = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    state['sent_daily'] = {k: v for k, v in state['sent_daily'].items() if k >= cutoff}

    save_state(state)
    print('=== DONE ===')

if __name__ == '__main__':
    run_news_detector()
