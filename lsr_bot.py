"""
=======================================================
  Trend Following Breakout Bot — v1.0
  Pairs    : 18 pair forex + komoditas
  Strategy : Trend Following + Breakout Entry
             1D trend → 4H struktur → 1H breakout level
             → 15M konfirmasi momentum → Entry
             + Anti-Reversal Filter (CHoCH & RSI divergence)
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
# INDIKATOR TEKNIKAL
# ─────────────────────────────────────────────
def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_adr(df, lookback=14):
    """Average Daily Range — mengukur volatilitas rata-rata."""
    return (df["high"] - df["low"]).tail(lookback).mean()

# ─────────────────────────────────────────────
# DETEKSI TREN UTAMA
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

def detect_trend(df, label=""):
    """
    Menentukan tren berdasarkan:
    1. EMA 50 vs EMA 200 — filter tren makro
    2. Struktur swing HH/HL atau LL/LH
    3. Harga di atas/bawah EMA 50

    Return: "UPTREND", "DOWNTREND", atau "SIDEWAYS"
    """
    if len(df) < 50:
        return "SIDEWAYS"

    ema50  = calc_ema(df["close"], 50)
    ema200 = calc_ema(df["close"], 200) if len(df) >= 200 else None
    price  = df["close"].iloc[-1]
    e50    = ema50.iloc[-1]

    swing_highs, swing_lows = find_swing_points(df, lookback=50)

    structure = "SIDEWAYS"
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        sh1, sh2 = swing_highs[-2][1], swing_highs[-1][1]
        sl1, sl2 = swing_lows[-2][1],  swing_lows[-1][1]
        hh = sh2 > sh1
        hl = sl2 > sl1
        ll = sl2 < sl1
        lh = sh2 < sh1
        if hh and hl:
            structure = "UPTREND"
        elif ll and lh:
            structure = "DOWNTREND"

    # Konfirmasi dengan EMA
    if structure == "UPTREND":
        if price < e50 * 0.998:          # harga jauh di bawah EMA50 → lemahkan sinyal
            structure = "SIDEWAYS"
    elif structure == "DOWNTREND":
        if price > e50 * 1.002:
            structure = "SIDEWAYS"

    # Jika ada EMA200, tren harus sejalan
    if ema200 is not None:
        e200 = ema200.iloc[-1]
        if structure == "UPTREND"   and e50 < e200:
            structure = "SIDEWAYS"
        if structure == "DOWNTREND" and e50 > e200:
            structure = "SIDEWAYS"

    if label:
        print(f"[TREND] {label}: {structure} | price={round(price,4)} EMA50={round(e50,4)}")
    return structure

# ─────────────────────────────────────────────
# DETEKSI BREAKOUT LEVEL (1H)
# ─────────────────────────────────────────────
def find_breakout_level(df, trend, lookback=30):
    """
    Cari level kunci yang kemungkinan akan di-break sesuai tren.

    UPTREND  → cari resistance terdekat (swing high / equal high)
               yang harga belum pernah melewatinya
    DOWNTREND→ cari support terdekat (swing low / equal low)

    Return: level (float) atau None
    """
    data         = df.tail(lookback).reset_index(drop=True)
    current_high = data["high"].max()
    current_low  = data["low"].min()
    current_close = df["close"].iloc[-2]

    swing_highs, swing_lows = find_swing_points(data, lookback=lookback)

    if trend == "UPTREND":
        # Kumpulkan semua swing high di atas harga saat ini
        levels = sorted(
            [s[1] for s in swing_highs if s[1] > current_close],
            reverse=False
        )
        # Cari yang paling dekat (terendah di antara yang lebih tinggi dari harga)
        if levels:
            return levels[0]
        # Fallback: pakai swing high tertinggi dalam lookback
        if swing_highs:
            return max(s[1] for s in swing_highs)

    elif trend == "DOWNTREND":
        levels = sorted(
            [s[1] for s in swing_lows if s[1] < current_close],
            reverse=True
        )
        if levels:
            return levels[0]
        if swing_lows:
            return min(s[1] for s in swing_lows)

    return None

# ─────────────────────────────────────────────
# DETEKSI BREAKOUT CANDLE (1H)
# ─────────────────────────────────────────────
def detect_breakout(df, trend, breakout_level, pair=""):
    """
    Konfirmasi breakout valid di 1H:

    UPTREND:
    - Close candle > breakout level (bullish breakout)
    - Body candle cukup besar (bukan false break)
    - Tidak ada upper wick besar (bukan rejection)

    DOWNTREND:
    - Close candle < breakout level (bearish breakout)
    - Body candle cukup besar
    - Tidak ada lower wick besar

    Tipe breakout yang diakui:
    1. Clean Break      — close melewati level dengan body kuat
    2. Momentum Break   — candle besar + volume relatif tinggi
    3. Retest Entry     — breakout + pullback ke level → pantul kembali

    Return: (valid: bool, breakout_type: str atau None)
    """
    if len(df) < 5 or breakout_level is None:
        return False, None

    curr = df.iloc[-2]    # candle confirmed terbaru
    prev = df.iloc[-3]
    atr  = calc_adr(df, 14)

    if atr <= 0:
        return False, None

    body       = abs(curr["close"] - curr["open"])
    upper_wick = curr["high"] - max(curr["open"], curr["close"])
    lower_wick = min(curr["open"], curr["close"]) - curr["low"]
    candle_range = curr["high"] - curr["low"]

    if trend == "UPTREND":
        # Harga harus sudah menembus level ke atas
        if curr["close"] <= breakout_level:
            print(f"[{pair}] Close {round(curr['close'],4)} belum tembus resistance {round(breakout_level,4)}")
            return False, None

        # Body minimal 35% ATR — bukan fake break
        if body < atr * 0.35:
            print(f"[{pair}] Body candle terlalu kecil untuk breakout valid")
            return False, None

        # Upper wick tidak lebih besar dari body (tidak ada rejection kuat)
        if upper_wick > body * 0.8:
            print(f"[{pair}] Upper wick besar → potensi false breakout atas")
            return False, None

        # Tipe 1: Clean Break — close > level, body > 50% candle range
        if body >= candle_range * 0.5 and curr["close"] > breakout_level:
            btype = "🚀 Clean Bullish Break"

        # Tipe 2: Momentum Break — candle jauh lebih besar dari prev
        elif body >= abs(prev["close"] - prev["open"]) * 1.5:
            btype = "💥 Momentum Bullish Break"

        # Tipe 3: Retest Entry — prev break, curr pullback ke level lalu close di atas
        elif (prev["close"] > breakout_level and
              curr["low"]   <= breakout_level * 1.001 and
              curr["close"] > breakout_level):
            btype = "🔄 Retest Bullish Entry"

        else:
            print(f"[{pair}] Breakout bullish ditemukan tapi tipe tidak diakui")
            return False, None

        print(f"[{pair}] ✅ {btype} di level {round(breakout_level,4)}")
        return True, btype

    elif trend == "DOWNTREND":
        if curr["close"] >= breakout_level:
            print(f"[{pair}] Close {round(curr['close'],4)} belum tembus support {round(breakout_level,4)}")
            return False, None

        if body < atr * 0.35:
            print(f"[{pair}] Body candle terlalu kecil untuk breakout valid")
            return False, None

        if lower_wick > body * 0.8:
            print(f"[{pair}] Lower wick besar → potensi false breakout bawah")
            return False, None

        if body >= candle_range * 0.5 and curr["close"] < breakout_level:
            btype = "🔻 Clean Bearish Break"

        elif body >= abs(prev["close"] - prev["open"]) * 1.5:
            btype = "💥 Momentum Bearish Break"

        elif (prev["close"] < breakout_level and
              curr["high"]  >= breakout_level * 0.999 and
              curr["close"] < breakout_level):
            btype = "🔄 Retest Bearish Entry"

        else:
            print(f"[{pair}] Breakout bearish ditemukan tapi tipe tidak diakui")
            return False, None

        print(f"[{pair}] ✅ {btype} di level {round(breakout_level,4)}")
        return True, btype

    return False, None

# ─────────────────────────────────────────────
# FILTER PEMBALIKAN TREND (ANTI-REVERSAL)
# ─────────────────────────────────────────────
def detect_trend_reversal_risk(df_4h, df_1h, trend, pair=""):
    """
    Deteksi tanda-tanda bahwa tren sedang berbalik arah.
    Jika ada tanda reversal → batalkan sinyal.

    Tanda bahaya yang dicek:
    1. Change of Character (CHoCH) di 4H:
       - UPTREND : swing low terbaru ditembus ke bawah
       - DOWNTREND: swing high terbaru ditembus ke atas
    2. RSI Divergence di 1H:
       - Bullish divergence di DOWNTREND = harga baru low tapi RSI naik
       - Bearish divergence di UPTREND  = harga baru high tapi RSI turun
    3. EMA crossover signal di 4H:
       - EMA21 menyeberangi EMA50 berlawanan arah tren

    Return: (reversal_risk: bool, reason: str)
    """
    # ── Cek 1: CHoCH di 4H ──────────────────────
    if df_4h is not None and len(df_4h) >= 20:
        swing_highs_4h, swing_lows_4h = find_swing_points(df_4h, lookback=40)
        curr_close_4h = df_4h["close"].iloc[-1]

        if trend == "UPTREND" and swing_lows_4h:
            last_sl = swing_lows_4h[-1][1]
            # Jika harga close menembus swing low terakhir → CHoCH bearish
            if curr_close_4h < last_sl:
                reason = f"⚠️ CHoCH: Harga tembus swing low 4H ({round(last_sl,4)})"
                print(f"[{pair}] REVERSAL RISK — {reason}")
                return True, reason

        if trend == "DOWNTREND" and swing_highs_4h:
            last_sh = swing_highs_4h[-1][1]
            if curr_close_4h > last_sh:
                reason = f"⚠️ CHoCH: Harga tembus swing high 4H ({round(last_sh,4)})"
                print(f"[{pair}] REVERSAL RISK — {reason}")
                return True, reason

    # ── Cek 2: RSI Divergence di 1H ─────────────
    if df_1h is not None and len(df_1h) >= 20:
        rsi_series = calc_rsi(df_1h["close"], 14)
        # Ambil 20 candle terakhir untuk deteksi divergence
        price_tail = df_1h["close"].tail(20).values
        rsi_tail   = rsi_series.tail(20).values

        # Cari dua titik ekstrim terakhir
        price_hi_idx = [i for i in range(1, 19) if price_tail[i] > price_tail[i-1] and price_tail[i] > price_tail[i+1]]
        price_lo_idx = [i for i in range(1, 19) if price_tail[i] < price_tail[i-1] and price_tail[i] < price_tail[i+1]]

        if trend == "UPTREND" and len(price_hi_idx) >= 2:
            p1, p2   = price_hi_idx[-2], price_hi_idx[-1]
            # Bearish divergence: harga higher high, RSI lower high
            if price_tail[p2] > price_tail[p1] and rsi_tail[p2] < rsi_tail[p1] - 3:
                reason = "⚠️ RSI Bearish Divergence di 1H"
                print(f"[{pair}] REVERSAL RISK — {reason}")
                return True, reason

        if trend == "DOWNTREND" and len(price_lo_idx) >= 2:
            p1, p2 = price_lo_idx[-2], price_lo_idx[-1]
            # Bullish divergence: harga lower low, RSI higher low
            if price_tail[p2] < price_tail[p1] and rsi_tail[p2] > rsi_tail[p1] + 3:
                reason = "⚠️ RSI Bullish Divergence di 1H"
                print(f"[{pair}] REVERSAL RISK — {reason}")
                return True, reason

    # ── Cek 3: EMA crossover berlawanan arah di 4H ─
    if df_4h is not None and len(df_4h) >= 50:
        ema21 = calc_ema(df_4h["close"], 21)
        ema50 = calc_ema(df_4h["close"], 50)
        e21_now  = ema21.iloc[-1]
        e50_now  = ema50.iloc[-1]
        e21_prev = ema21.iloc[-2]
        e50_prev = ema50.iloc[-2]

        # EMA21 cross below EMA50 di UPTREND → sinyal pelemahan
        if trend == "UPTREND" and e21_prev >= e50_prev and e21_now < e50_now:
            reason = "⚠️ EMA21 cross bawah EMA50 di 4H (tren melemah)"
            print(f"[{pair}] REVERSAL RISK — {reason}")
            return True, reason

        # EMA21 cross above EMA50 di DOWNTREND → sinyal pelemahan bearish
        if trend == "DOWNTREND" and e21_prev <= e50_prev and e21_now > e50_now:
            reason = "⚠️ EMA21 cross atas EMA50 di 4H (tren melemah)"
            print(f"[{pair}] REVERSAL RISK — {reason}")
            return True, reason

    return False, ""

# ─────────────────────────────────────────────
# KONFIRMASI MOMENTUM 15M
# ─────────────────────────────────────────────
def confirm_breakout_15m(df, action, pair=""):
    """
    Konfirmasi momentum di 15M setelah breakout 1H:
    - Candle searah action
    - Close melewati high/low candle sebelumnya
    - RSI mendukung arah (tidak overbought/oversold ekstrem)
    - Body minimal 25% ATR
    """
    if len(df) < 5:
        return False

    curr = df.iloc[-2]
    prev = df.iloc[-3]
    atr  = calc_adr(df, 14)
    body = abs(curr["close"] - curr["open"])
    rsi  = calc_rsi(df["close"], 14).iloc[-2]

    if body < atr * 0.25:
        print(f"[{pair}] 15M body lemah")
        return False

    if action == "BUY":
        # RSI tidak boleh overbought ekstrem (>80) → kemungkinan pullback segera
        if rsi > 80:
            print(f"[{pair}] 15M RSI overbought ({round(rsi,1)}) → skip")
            return False
        return (curr["close"] > curr["open"] and
                curr["close"] > prev["high"])

    elif action == "SELL":
        if rsi < 20:
            print(f"[{pair}] 15M RSI oversold ({round(rsi,1)}) → skip")
            return False
        return (curr["close"] < curr["open"] and
                curr["close"] < prev["low"])

    return False

# ─────────────────────────────────────────────
# HITUNG SL / TP
# ─────────────────────────────────────────────
def calc_sl_tp(df, action, breakout_level):
    """
    Entry : close terbaru (15M)
    SL    : di balik breakout_level + ATR buffer
            (jika harga kembali ke bawah/atas level → breakout gagal)
    TP    : RR 1:2.5 (lebih konservatif untuk trend following)
    """
    price  = df["close"].iloc[-2]
    atr    = calc_adr(df, 14)
    buffer = atr * 0.25

    if action == "BUY":
        sl   = round(breakout_level - buffer, 4)
        risk = price - sl
        if risk <= 0 or risk > atr * 5:
            return None, None, None, None
        tp = round(price + risk * 2.5, 4)

    elif action == "SELL":
        sl   = round(breakout_level + buffer, 4)
        risk = sl - price
        if risk <= 0 or risk > atr * 5:
            return None, None, None, None
        tp = round(price - risk * 2.5, 4)

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
def analyze_with_groq(pair, trend_1d, trend_4h, action, breakout_type, headlines, fred_data):
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

Pair      : {pair}
Sinyal    : {action}
Tren 1D   : {trend_1d}
Tren 4H   : {trend_4h}
Breakout  : {breakout_type}

{fred_text}

Berita Terkini:
{news_text}

Tugas:
1. Apakah kondisi makro mendukung sinyal {action} pada {pair}?
2. Dampak data Fed Rate, CPI, NFP terhadap {pair}?
3. Apakah tren jangka panjang 1D & 4H SEJALAN atau ada risiko pembalikan?
4. Prediksi kelanjutan tren berdasarkan fundamental.

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
# ANALISIS PER PAIR
# ─────────────────────────────────────────────
def analyze_pair(pair, symbol):
    print(f"\n[{pair}] Menganalisis...")

    df_1d  = get_data(symbol, "1d",  "120d")
    df_4h  = get_data(symbol, "4h",  "60d")
    df_1h  = get_data(symbol, "1h",  "7d")
    df_15m = get_data(symbol, "15m", "2d")

    if df_1d is None or df_4h is None or df_1h is None or df_15m is None:
        print(f"[{pair}] Data tidak tersedia")
        return None

    # ── Step 1: Deteksi tren 1D & 4H ──────────
    trend_1d = detect_trend(df_1d, label=f"{pair} 1D")
    trend_4h = detect_trend(df_4h, label=f"{pair} 4H")

    # Kedua timeframe harus searah — jika tidak, skip
    if trend_1d == "SIDEWAYS" or trend_4h == "SIDEWAYS":
        print(f"[{pair}] Salah satu TF SIDEWAYS → NO TRADE")
        return None

    if trend_1d != trend_4h:
        print(f"[{pair}] Tren 1D ({trend_1d}) ≠ 4H ({trend_4h}) → konflik → NO TRADE")
        return None

    trend  = trend_1d
    action = "BUY" if trend == "UPTREND" else "SELL"

    # ── Step 2: Cek risiko pembalikan tren ────
    reversal_risk, reversal_reason = detect_trend_reversal_risk(
        df_4h, df_1h, trend, pair=pair
    )
    if reversal_risk:
        print(f"[{pair}] Ada tanda pembalikan → batalkan sinyal")
        return None

    # ── Step 3: Cari level breakout di 1H ─────
    breakout_level = find_breakout_level(df_1h, trend, lookback=30)
    if breakout_level is None:
        print(f"[{pair}] Tidak ada level breakout yang valid")
        return None

    print(f"[{pair}] Level breakout {trend}: {round(breakout_level,4)}")

    # ── Step 4: Konfirmasi breakout di 1H ─────
    broke, breakout_type = detect_breakout(df_1h, trend, breakout_level, pair=pair)
    if not broke:
        return None

    # ── Step 5: Konfirmasi momentum di 15M ────
    confirmed = confirm_breakout_15m(df_15m, action, pair=pair)
    if not confirmed:
        print(f"[{pair}] Momentum 15M belum konfirmasi")
        return None

    # ── Step 6: Hitung SL / TP ────────────────
    entry, sl, tp, rr = calc_sl_tp(df_15m, action, breakout_level)
    if entry is None:
        print(f"[{pair}] Risk kalkulasi invalid, skip")
        return None

    print(f"[{pair}] ✅ SINYAL {action} | Entry:{entry} SL:{sl} TP:{tp} RR:1:{rr}")
    return {
        "pair"          : pair,
        "action"        : action,
        "entry"         : entry,
        "sl"            : sl,
        "tp"            : tp,
        "rr"            : rr,
        "trend_1d"      : trend_1d,
        "trend_4h"      : trend_4h,
        "breakout_level": breakout_level,
        "breakout_type" : breakout_type,
    }

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
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  Trend Following Breakout Bot  v1.0")
    print(f"  Pairs   : {len(PAIRS)} pairs aktif")
    print(f"  Interval: {CHECK_INTERVAL}s")
    print(f"  Max sinyal/cycle: {MAX_SIGNALS_PER_CYCLE}")
    print("=" * 55)

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN / CHAT_ID belum diset!")
        return

    send_telegram(
        "🤖 <b>Trend Following Breakout Bot v1.0 — ONLINE!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Pairs      : {len(PAIRS)} pair aktif\n"
        "📈 Strategy   : Trend Following + Breakout Entry\n"
        "🕯️ Timeframe  : 1D Trend → 4H Konfirmasi → 1H Breakout → 15M Entry\n"
        "🛡️ Anti-Rev   : CHoCH + RSI Divergence + EMA Cross Filter\n"
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

                # Anti-spam: pakai breakout_level sebagai identitas setup
                sig_key   = f"{pair}_{result['action']}_{result['breakout_level']}"
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
                    pair,
                    result["trend_1d"],
                    result["trend_4h"],
                    result["action"],
                    result["breakout_type"],
                    headlines,
                    fred_data
                )

                emj       = "🟢" if result["action"] == "BUY" else "🔴"
                trend_emj = "📈" if result["trend_1d"] == "UPTREND" else "📉"
                news_text = "\n".join(
                    [f"   • {h[:55]}..." for h in headlines]
                ) if headlines else "   • Tidak tersedia"
                fred_text = format_fred_data(fred_data) if fred_data else "   • Tidak tersedia"

                msg = (
                    f"{emj} <b>SINYAL {result['action']} — {pair}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱️ Waktu          : {now_str}\n"
                    f"💰 Entry          : {result['entry']}\n"
                    f"🛑 Stop Loss      : {result['sl']}\n"
                    f"🎯 Take Profit    : {result['tp']}\n"
                    f"⚖️ R:R Ratio      : 1:{result['rr']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{trend_emj} <b>Tren 1D  :</b> {result['trend_1d']}\n"
                    f"📊 <b>Tren 4H  :</b> {result['trend_4h']}\n"
                    f"🔑 Break Level    : {result['breakout_level']}\n"
                    f"💥 Tipe Breakout  : {result['breakout_type']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 <b>Data Ekonomi Makro:</b>\n{fred_text}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📰 <b>Berita Terkini:</b>\n{news_text}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🧠 <b>Analisis AI Makro:</b>\n{ai_analysis}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ <b>BREAKOUT TREND TERKONFIRMASI!</b>\n"
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
