"""
=======================================================
  SMC Liquidity Sweep Reversal Bot — v1.0
  Pairs    : 18 pair forex + komoditas
  Strategy : Liquidity Sweep Reversal
             4H trend → 1H sweep level → 1H reversal candle
             → 15M konfirmasi → Entry
  AI       : Groq Llama3 (analisis makro ekonomi)
  Data     : yfinance + FRED API + NewsAPI
  Notif    : Telegram
=======================================================
"""

import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
NEWS_API_KEY   = os.environ.get("NEWS_API_KEY", "")
FRED_API_KEY   = os.environ.get("FRED_API_KEY", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))

MAX_SIGNALS_PER_CYCLE = 3
SIGNAL_EXPIRE_SECS    = 4 * 3600

PAIRS = {
    "XAUUSD" : "GC=F",
    "USDJPY" : "JPY=X",
    "AUDCAD" : "AUDCAD=X",
    "EURJPY" : "EURJPY=X",
    "EURUSD" : "EURUSD=X",
    "GBPUSD" : "GBPUSD=X",
    "USDCHF" : "USDCHF=X",
    "USDCAD" : "USDCAD=X",
    "AUDUSD" : "AUDUSD=X",
    "NZDUSD" : "NZDUSD=X",
    "GBPJPY" : "GBPJPY=X",
    "CADJPY" : "CADJPY=X",
    "CHFJPY" : "CHFJPY=X",
    "EURGBP" : "EURGBP=X",
    "EURAUD" : "EURAUD=X",
    "GBPAUD" : "GBPAUD=X",
    "XAGUSD" : "SI=F",
    "USOIL"  : "CL=F",
}

# ─────────────────────────────────────────────
# SESSION FILTER
# ─────────────────────────────────────────────
def is_valid_session():
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour
    in_london  = 7  <= hour < 16
    in_newyork = 12 <= hour < 21
    in_asia    = 0  <= hour < 7
    return in_london or in_newyork or in_asia

# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def get_data(symbol, interval, period):
    try:
        ticker = yf.Ticker(symbol)
        df     = ticker.history(period=period, interval=interval)
        if (df is None or len(df) < 10) and interval == "4h":
            print(f"[DATA] {symbol} tidak support 4h, fallback ke 1h")
            df = ticker.history(period="30d", interval="1h")
        if df is None or len(df) < 10:
            return None
        df         = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df         = df.rename(columns={"datetime": "time", "date": "time"})
        col        = pd.to_datetime(df["time"])
        if col.dt.tz is not None:
            col = col.dt.tz_convert(None)
        else:
            col = col.dt.tz_localize(None)
        df["time"] = col
        df         = df[["time", "open", "high", "low", "close", "volume"]]
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"[DATA ERROR] {symbol} {interval}: {e}")
        return None

# ─────────────────────────────────────────────
# STRUKTUR MARKET
# ─────────────────────────────────────────────
def find_swing_points(df, lookback=30):
    highs = []
    lows  = []
    data  = df.tail(lookback).reset_index(drop=True)
    for i in range(1, len(data) - 1):
        if data["high"].iloc[i] > data["high"].iloc[i-1] and \
           data["high"].iloc[i] > data["high"].iloc[i+1]:
            highs.append((i, data["high"].iloc[i]))
        if data["low"].iloc[i] < data["low"].iloc[i-1] and \
           data["low"].iloc[i] < data["low"].iloc[i+1]:
            lows.append((i, data["low"].iloc[i]))
    return highs, lows

def detect_structure(df):
    if len(df) < 15:
        return "SIDEWAYS"
    swing_highs, swing_lows = find_swing_points(df, lookback=40)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "SIDEWAYS"
    sh1 = swing_highs[-2][1]
    sh2 = swing_highs[-1][1]
    sl1 = swing_lows[-2][1]
    sl2 = swing_lows[-1][1]
    hh  = sh2 > sh1
    hl  = sl2 > sl1
    ll  = sl2 < sl1
    lh  = sh2 < sh1
    if hh and hl:
        return "UPTREND"
    elif ll and lh:
        return "DOWNTREND"
    return "SIDEWAYS"

# ─────────────────────────────────────────────
# EQUAL HIGHS / LOWS (liquidity magnet)
# ─────────────────────────────────────────────
def detect_equal_levels(df, lookback=20, tolerance=0.0015):
    """
    Cari equal highs dan equal lows — zona di mana
    stop loss banyak trader menumpuk (liquidity pool).
    Tolerance default 0.15% dari harga.
    """
    data   = df.tail(lookback)
    eq_high = None
    eq_low  = None
    highs  = data["high"].values
    lows   = data["low"].values

    # Cari pasangan high yang hampir sama
    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if highs[i] > 0 and abs(highs[i] - highs[j]) / highs[i] < tolerance:
                eq_high = max(highs[i], highs[j])
                break
        if eq_high:
            break

    # Cari pasangan low yang hampir sama
    for i in range(len(lows) - 1):
        for j in range(i + 1, len(lows)):
            if lows[i] > 0 and abs(lows[i] - lows[j]) / lows[i] < tolerance:
                eq_low = min(lows[i], lows[j])
                break
        if eq_low:
            break

    return eq_high, eq_low

# ─────────────────────────────────────────────
# DETEKSI SWEEP + REVERSAL CANDLE
# ─────────────────────────────────────────────
def detect_sweep_reversal(df, structure, pair=""):
    """
    Cari liquidity sweep diikuti reversal candle di 1H.

    Logika:
    UPTREND  → cari sweep LOW (stop hunt bawah) → reversal naik → BUY
    DOWNTREND→ cari sweep HIGH (stop hunt atas) → reversal turun → SELL

    Tipe reversal yang diakui:
    1. Pin Bar   — ekor panjang, body kecil
    2. Engulfing — candle besar menelan candle sebelumnya
    3. Strong Close — close kuat melewati high/low candle sebelumnya
    """
    if len(df) < 15:
        return False, None, None, None

    # Ambil swing points dari candle yang sudah confirmed (exclude 3 terbaru)
    swing_highs, swing_lows = find_swing_points(df.iloc[:-2], lookback=30)
    eq_high, eq_low         = detect_equal_levels(df.iloc[:-2], lookback=20)

    curr = df.iloc[-2]   # candle confirmed terbaru
    prev = df.iloc[-3]   # candle sebelumnya
    atr  = (df["high"] - df["low"]).tail(14).mean()

    if atr <= 0:
        return False, None, None, None

    # ── BUY setup: sweep LOW ──────────────────
    if structure == "UPTREND":
        # Kumpulkan level liquidity bawah
        levels = []
        if swing_lows:
            levels += [s[1] for s in swing_lows[-3:]]
        if eq_low:
            levels.append(eq_low)
        if not levels:
            print(f"[{pair}] Tidak ada level liquidity bawah")
            return False, None, None, None

        sweep_level = min(levels)   # level paling rendah = liquidity utama

        # Cek apakah candle current atau previous sweep lalu close di atas
        curr_swept = curr["low"] < sweep_level and curr["close"] > sweep_level
        prev_swept = prev["low"] < sweep_level and prev["close"] > sweep_level

        if not (curr_swept or prev_swept):
            print(f"[{pair}] Tidak ada sweep low di {round(sweep_level, 4)}")
            return False, None, None, None

        # Tentukan candle reversal
        rev_candle   = curr if curr_swept else prev
        candle_range = rev_candle["high"] - rev_candle["low"]
        if candle_range <= 0:
            return False, None, None, None

        body        = abs(rev_candle["close"] - rev_candle["open"])
        lower_wick  = min(rev_candle["open"], rev_candle["close"]) - rev_candle["low"]
        upper_wick  = rev_candle["high"] - max(rev_candle["open"], rev_candle["close"])
        reversal    = None

        # Pin Bar Bullish — ekor bawah panjang
        if (lower_wick >= body * 1.5 and
                lower_wick > upper_wick and
                rev_candle["close"] > rev_candle["low"] + candle_range * 0.5):
            reversal = "📍 Pin Bar Bullish"

        # Engulfing Bullish
        elif (rev_candle["close"] > rev_candle["open"] and
              rev_candle["close"] >= prev["high"] and
              rev_candle["open"]  <= prev["close"]):
            reversal = "🔥 Engulfing Bullish"

        # Strong Close Bullish
        elif (rev_candle["close"] > rev_candle["open"] and
              body >= atr * 0.3 and
              rev_candle["close"] > sweep_level + atr * 0.1):
            reversal = "💪 Strong Close Bullish"

        if reversal:
            print(f"[{pair}] ✅ Sweep low {round(sweep_level,4)} → {reversal}")
            return True, sweep_level, reversal, "BUY"
        else:
            print(f"[{pair}] Sweep ditemukan tapi reversal candle lemah")
            return False, None, None, None

    # ── SELL setup: sweep HIGH ────────────────
    elif structure == "DOWNTREND":
        # Kumpulkan level liquidity atas
        levels = []
        if swing_highs:
            levels += [s[1] for s in swing_highs[-3:]]
        if eq_high:
            levels.append(eq_high)
        if not levels:
            print(f"[{pair}] Tidak ada level liquidity atas")
            return False, None, None, None

        sweep_level = max(levels)

        curr_swept = curr["high"] > sweep_level and curr["close"] < sweep_level
        prev_swept = prev["high"] > sweep_level and prev["close"] < sweep_level

        if not (curr_swept or prev_swept):
            print(f"[{pair}] Tidak ada sweep high di {round(sweep_level, 4)}")
            return False, None, None, None

        rev_candle   = curr if curr_swept else prev
        candle_range = rev_candle["high"] - rev_candle["low"]
        if candle_range <= 0:
            return False, None, None, None

        body       = abs(rev_candle["close"] - rev_candle["open"])
        lower_wick = min(rev_candle["open"], rev_candle["close"]) - rev_candle["low"]
        upper_wick = rev_candle["high"] - max(rev_candle["open"], rev_candle["close"])
        reversal   = None

        # Pin Bar Bearish — ekor atas panjang
        if (upper_wick >= body * 1.5 and
                upper_wick > lower_wick and
                rev_candle["close"] < rev_candle["high"] - candle_range * 0.5):
            reversal = "📍 Pin Bar Bearish"

        # Engulfing Bearish
        elif (rev_candle["close"] < rev_candle["open"] and
              rev_candle["close"] <= prev["low"] and
              rev_candle["open"]  >= prev["close"]):
            reversal = "🔥 Engulfing Bearish"

        # Strong Close Bearish
        elif (rev_candle["close"] < rev_candle["open"] and
              body >= atr * 0.3 and
              rev_candle["close"] < sweep_level - atr * 0.1):
            reversal = "💪 Strong Close Bearish"

        if reversal:
            print(f"[{pair}] ✅ Sweep high {round(sweep_level,4)} → {reversal}")
            return True, sweep_level, reversal, "SELL"
        else:
            print(f"[{pair}] Sweep ditemukan tapi reversal candle lemah")
            return False, None, None, None

    return False, None, None, None

# ─────────────────────────────────────────────
# KONFIRMASI 15M
# ─────────────────────────────────────────────
def confirm_reversal_15m(df, action):
    """
    Konfirmasi momentum di 15M:
    - Candle harus searah dengan action
    - Close melewati high/low candle sebelumnya
    - Body minimal 30% ATR (tidak terlalu lemah)
    """
    if len(df) < 3:
        return False
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    atr  = (df["high"] - df["low"]).tail(14).mean()
    body = abs(curr["close"] - curr["open"])

    if body < atr * 0.1:
        return False

    if action == "BUY":
        return (curr["close"] > curr["open"] and
                curr["close"] > prev["high"])
    elif action == "SELL":
        return (curr["close"] < curr["open"] and
                curr["close"] < prev["low"])
    return False

# ─────────────────────────────────────────────
# HITUNG SL / TP
# ─────────────────────────────────────────────
def calc_sl_tp(df, action, sweep_level):
    """
    SL: di balik sweep level + ATR buffer
    TP: RR 1:3
    """
    price  = df["close"].iloc[-2]
    atr    = (df["high"] - df["low"]).tail(14).mean()
    buffer = atr * 0.3

    if action == "BUY":
        sl   = round(sweep_level - buffer, 4)
        risk = price - sl
        if risk <= 0 or risk > atr * 4:
            return None, None, None, None
        tp = round(price + risk * 3, 4)

    elif action == "SELL":
        sl   = round(sweep_level + buffer, 4)
        risk = sl - price
        if risk <= 0 or risk > atr * 4:
            return None, None, None, None
        tp = round(price - risk * 3, 4)

    else:
        return None, None, None, None

    rr = round(abs(tp - price) / abs(price - sl), 2)
    return round(price, 4), round(sl, 4), round(tp, 4), rr

# ─────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────
def get_news(pair):
    keywords = {
        "XAUUSD": "gold XAU USD Federal Reserve",
        "USDJPY": "USD JPY Bank of Japan Fed",
        "AUDCAD": "AUD CAD Australia Canada oil",
        "EURJPY": "EUR JPY Euro Japan ECB",
        "EURUSD": "EUR USD Euro ECB Federal Reserve",
        "GBPUSD": "GBP USD Bank of England Fed",
        "USDCHF": "USD CHF Swiss National Bank",
        "USDCAD": "USD CAD Canada oil Bank of Canada",
        "AUDUSD": "AUD USD Australia RBA",
        "NZDUSD": "NZD USD New Zealand RBNZ",
        "GBPJPY": "GBP JPY Bank of England Japan",
        "CADJPY": "CAD JPY Canada Japan oil",
        "CHFJPY": "CHF JPY Swiss Japan",
        "EURGBP": "EUR GBP ECB Bank of England",
        "EURAUD": "EUR AUD ECB Australia",
        "GBPAUD": "GBP AUD Britain Australia",
        "XAGUSD": "silver XAG USD commodities",
        "USOIL" : "crude oil WTI OPEC",
    }
    kw = keywords.get(pair, "forex")
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q"       : kw,
                "language": "en",
                "sortBy"  : "publishedAt",
                "pageSize": 3,
                "apiKey"  : NEWS_API_KEY,
            },
            timeout=10
        )
        if r.status_code == 429:
            print(f"[NEWS] Rate limit (429), skip {pair}")
            return []
        if r.status_code != 200:
            print(f"[NEWS] HTTP {r.status_code} untuk {pair}")
            return []
        articles = r.json().get("articles", [])
        return [a["title"] for a in articles[:3]]
    except Exception as e:
        print(f"[NEWS ERROR] {e}")
        return []

# ─────────────────────────────────────────────
# FRED DATA
# ─────────────────────────────────────────────
def get_fred_data():
    indicators = {
        "Fed Rate"    : "FEDFUNDS",
        "CPI US"      : "CPIAUCSL",
        "NFP"         : "PAYEMS",
        "GDP US"      : "GDP",
        "DXY"         : "DTWEXBGS",
        "Unemployment": "UNRATE",
    }
    result = {}
    for name, series_id in indicators.items():
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id" : series_id,
                    "api_key"   : FRED_API_KEY,
                    "file_type" : "json",
                    "sort_order": "desc",
                    "limit"     : 2,
                },
                timeout=10
            )
            if r.status_code != 200:
                continue
            obs = r.json().get("observations", [])
            if len(obs) >= 2:
                latest = obs[0]["value"]
                prev   = obs[1]["value"]
                if latest == "." or prev == ".":
                    continue
                result[name] = {
                    "latest": latest,
                    "prev"  : prev,
                    "change": "NAIK"  if float(latest) > float(prev)
                              else "TURUN" if float(latest) < float(prev)
                              else "SAMA",
                }
        except Exception as e:
            print(f"[FRED ERROR] {name}: {e}")
    return result

def format_fred_data(fred_data):
    if not fred_data:
        return "   • Data ekonomi tidak tersedia"
    arrows = {"NAIK": "⬆️", "TURUN": "⬇️", "SAMA": "➡️"}
    lines  = []
    for name, data in fred_data.items():
        arrow = arrows.get(data["change"], "")
        lines.append(f"   • {name}: {data['latest']} {arrow} (prev: {data['prev']})")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# GROQ AI ANALYSIS
# ─────────────────────────────────────────────
def analyze_with_groq(pair, structure, action, reversal_type, headlines, fred_data):
    if not GROQ_API_KEY:
        return "Analisis AI tidak tersedia."
    try:
        news_text = "\n".join([f"- {h}" for h in headlines]) if headlines else "Tidak ada berita terkini."
        fred_text = ""
        if fred_data:
            fred_text = f"""
Data Ekonomi Makro Terkini:
- Fed Rate    : {fred_data.get('Fed Rate',     {}).get('latest','N/A')}% ({fred_data.get('Fed Rate',     {}).get('change','N/A')})
- CPI US      : {fred_data.get('CPI US',       {}).get('latest','N/A')} ({fred_data.get('CPI US',       {}).get('change','N/A')})
- NFP         : {fred_data.get('NFP',          {}).get('latest','N/A')}K ({fred_data.get('NFP',         {}).get('change','N/A')})
- GDP US      : {fred_data.get('GDP US',       {}).get('latest','N/A')} ({fred_data.get('GDP US',       {}).get('change','N/A')})
- Unemployment: {fred_data.get('Unemployment', {}).get('latest','N/A')}% ({fred_data.get('Unemployment',{}).get('change','N/A')})
- DXY         : {fred_data.get('DXY',          {}).get('latest','N/A')} ({fred_data.get('DXY',          {}).get('change','N/A')})"""

        prompt = f"""Kamu adalah analis trading forex dan ekonomi makro profesional.

Pair    : {pair}
Sinyal  : {action}
Struktur: {structure}
Pola    : {reversal_type} setelah liquidity sweep

{fred_text}

Berita Terkini:
{news_text}

Tugas:
1. Apakah kondisi makro mendukung sinyal {action} pada {pair}?
2. Dampak data Fed Rate, CPI, NFP terhadap {pair}?
3. Fundamental SEJALAN atau BERLAWANAN dengan sinyal teknikal?
4. Prediksi arah harga berdasarkan fundamental.

Jawab Bahasa Indonesia, maksimal 5 kalimat, langsung ke poin."""

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type" : "application/json",
            },
            json={
                "model"     : "llama-3.1-8b-instant",
                "messages"  : [{"role": "user", "content": prompt}],
                "max_tokens": 300,
            },
            timeout=15
        )
        if r.status_code != 200:
            return "Analisis AI tidak tersedia saat ini."
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[GROQ ERROR] {e}")
        return "Analisis AI tidak tersedia saat ini."

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if r.status_code == 200:
            print("[TELEGRAM] ✅ Terkirim!")
        else:
            print(f"[TELEGRAM] ❌ {r.text}")
    except Exception as e:
        print(f"[TELEGRAM] ❌ {e}")

# ─────────────────────────────────────────────
# ANALISIS PER PAIR
# ─────────────────────────────────────────────
def analyze_pair(pair, symbol):
    print(f"\n[{pair}] Menganalisis...")

    df_4h  = get_data(symbol, "4h",  "60d")
    df_1h  = get_data(symbol, "1h",  "7d")
    df_15m = get_data(symbol, "15m", "2d")

    if df_4h is None or df_1h is None or df_15m is None:
        print(f"[{pair}] Data tidak tersedia")
        return None

    structure_4h = detect_structure(df_4h)
    structure_1h = detect_structure(df_1h)

    print(f"[{pair}] Struktur 4H: {structure_4h} | 1H: {structure_1h}")

    if structure_4h == "SIDEWAYS":
        print(f"[{pair}] 4H SIDEWAYS → NO TRADE")
        return None

    structure = structure_4h

    # Step 1: Cari sweep + reversal candle di 1H
    swept, sweep_level, reversal_type, action = detect_sweep_reversal(
        df_1h, structure, pair=pair
    )
    if not swept:
        return None

    # Step 2: Konfirmasi momentum di 15M
    confirmed = confirm_reversal_15m(df_15m, action)
    if not confirmed:
        print(f"[{pair}] Reversal belum terkonfirmasi di 15M")
        return None

    # Step 3: Hitung SL / TP
    entry, sl, tp, rr = calc_sl_tp(df_15m, action, sweep_level)
    if entry is None:
        print(f"[{pair}] Risk kalkulasi invalid, skip")
        return None

    print(f"[{pair}] ✅ SINYAL {action} | Entry:{entry} SL:{sl} TP:{tp} RR:1:{rr}")
    return {
        "pair"         : pair,
        "action"       : action,
        "entry"        : entry,
        "sl"           : sl,
        "tp"           : tp,
        "rr"           : rr,
        "structure"    : structure,
        "structure_1h" : structure_1h,
        "sweep_level"  : sweep_level,
        "reversal_type": reversal_type,
    }

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  SMC Liquidity Sweep Reversal Bot  v1.0")
    print(f"  Pairs   : {len(PAIRS)} pairs aktif")
    print(f"  Interval: {CHECK_INTERVAL}s")
    print(f"  Max sinyal/cycle: {MAX_SIGNALS_PER_CYCLE}")
    print("=" * 55)

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN / CHAT_ID belum diset!")
        return

    send_telegram(
        "🤖 <b>SMC Liquidity Sweep Reversal Bot v1.0 — ONLINE!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Pairs      : {len(PAIRS)} pair aktif\n"
        "📈 Strategy   : Liquidity Sweep Reversal\n"
        "🕯️ Timeframe  : 4H Trend → 1H Sweep → 15M Konfirmasi\n"
        "🧠 AI         : Groq Llama3 (Makro Ekonomi)\n"
        "📰 News       : NewsAPI\n"
        "📊 Ekonomi    : FRED API\n"
        "🕐 Session    : London, New York & Asia\n"
        f"🔢 Max Sinyal : {MAX_SIGNALS_PER_CYCLE} per siklus\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Bot berjalan 24 jam di Railway!"
    )

    sent_signals      = {}
    sent_signals_time = {}
    fred_data         = {}
    fred_timer        = 0

    while True:
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now_str}] Scanning {len(PAIRS)} pairs...")

        if not is_valid_session():
            now_utc = datetime.now(timezone.utc)
            print(f"[SESSION] Di luar sesi ({now_utc.strftime('%H:%M')} UTC) → skip")
            time.sleep(CHECK_INTERVAL)
            continue

        if time.time() - fred_timer > 3600:
            print("[FRED] Update data ekonomi...")
            fred_data  = get_fred_data()
            fred_timer = time.time()
            print(f"[FRED] {len(fred_data)} indikator diambil")

        signals_this_cycle = 0

        for pair, symbol in PAIRS.items():
            if signals_this_cycle >= MAX_SIGNALS_PER_CYCLE:
                print(f"[LIMIT] Max {MAX_SIGNALS_PER_CYCLE} sinyal tercapai, skip sisa")
                break

            try:
                result = analyze_pair(pair, symbol)
                if result is None:
                    continue

                # Anti-spam: pakai sweep_level sebagai identitas setup
                sig_key   = f"{pair}_{result['action']}_{result['sweep_level']}"
                now_ts    = time.time()
                last_key  = sent_signals.get(pair)
                last_time = sent_signals_time.get(pair, 0)
                stale     = (now_ts - last_time) > SIGNAL_EXPIRE_SECS

                if last_key == sig_key and not stale:
                    print(f"[{pair}] Sinyal sama & belum expire, skip.")
                    continue

                sent_signals[pair]      = sig_key
                sent_signals_time[pair] = now_ts

                headlines   = get_news(pair)
                ai_analysis = analyze_with_groq(
                    pair, result["structure"], result["action"],
                    result["reversal_type"], headlines, fred_data
                )

                emj       = "🟢" if result["action"] == "BUY" else "🔴"
                trend_emj = "📈" if result["structure"] == "UPTREND" else "📉"
                news_text = "\n".join(
                    [f"   • {h[:55]}..." for h in headlines]
                ) if headlines else "   • Tidak tersedia"
                fred_text = format_fred_data(fred_data) if fred_data else "   • Tidak tersedia"

                msg = (
                    f"{emj} <b>SINYAL {result['action']} — {pair}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱️ Waktu         : {now_str}\n"
                    f"💰 Entry         : {result['entry']}\n"
                    f"🛑 Stop Loss     : {result['sl']}\n"
                    f"🎯 Take Profit   : {result['tp']}\n"
                    f"⚖️ R:R Ratio     : 1:{result['rr']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{trend_emj} <b>Struktur 4H :</b> {result['structure']}\n"
                    f"📊 <b>Struktur 1H :</b> {result['structure_1h']}\n"
                    f"💧 Sweep Level   : {result['sweep_level']}\n"
                    f"🕯️ Pola Reversal : {result['reversal_type']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Data Ekonomi Makro:</b>\n{fred_text}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📰 <b>Berita Terkini:</b>\n{news_text}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🧠 <b>Analisis AI Makro:</b>\n{ai_analysis}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ <b>SETUP SWEEP REVERSAL TERKONFIRMASI!</b>\n"
                    f"⚠️ Risiko maks 1-2% per trade!"
                )
                send_telegram(msg)
                signals_this_cycle += 1

            except Exception as e:
                print(f"[ERROR] {pair}: {e}")

        print(f"[CYCLE DONE] {signals_this_cycle} sinyal dikirim cycle ini")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
