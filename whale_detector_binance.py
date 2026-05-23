"""
=======================================================
  Binance Whale Accumulation Detector
  Exchange : KuCoin (Public API, no key needed)
  Deteksi  : Akumulasi whale tahap awal
  Sinyal   :
    1. Volume spike abnormal
    2. Ask wall tiba-tiba hilang
    3. Trade besar beruntun
    4. Bid wall besar muncul
    5. Spread ketat
  Notif    : Telegram
=======================================================
"""

import os
import time
import requests
from datetime import datetime
from collections import defaultdict

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "120"))

# ── Threshold deteksi ─────────────────────────────────
VOLUME_SPIKE_MULTIPLIER = 3.0      # volume > 3x rata-rata → spike
MIN_VOLUME_USDT         = 100_000  # min volume 100k USDT/hari
WALL_USDT_THRESHOLD     = 50_000   # wall besar jika > 50k USDT
BIG_TRADE_USDT          = 10_000   # transaksi besar jika > 10k USDT
BIG_TRADE_COUNT         = 3        # minimal 3 transaksi besar
SCORE_MIN               = 4        # minimal score 2 untuk alert

# ── Penyimpanan histori ───────────────────────────────
volume_history   = defaultdict(list)
ask_wall_history = defaultdict(list)
sent_cache       = {}

BASE_URL = "https://api.kucoin.com"
BLACKLIST = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDD", "USD1"}

# ── Telegram ──────────────────────────────────────────
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("[TELEGRAM] Token/Chat ID belum diset")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id"   : CHAT_ID,
            "text"      : msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if r.status_code == 200:
            print(f"[TELEGRAM] ✅ Terkirim")
        else:
            print(f"[TELEGRAM] ❌ {r.text[:100]}")
    except Exception as e:
        print(f"[TELEGRAM] ❌ {e}")

# ── Test koneksi ──────────────────────────────────────
def test_connection():
    try:
        r = requests.get(f"{BASE_URL}/api/v1/timestamp", timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("code") == "200000":
            print(f"[TEST] KuCoin API: OK (status {r.status_code})")
            return True
        else:
            print(f"[TEST] KuCoin API: GAGAL (status {r.status_code}, code {data.get('code')})")
            return False
    except Exception as e:
        print(f"[TEST] GAGAL akses KuCoin: {e}")
        return False

# ── Ambil semua pair USDT ─────────────────────────────
def get_all_pairs():
    try:
        r = requests.get(f"{BASE_URL}/api/v1/symbols", timeout=15)
        data = r.json()
        pairs = []
        for s in data.get("data", []):
            if (s["quoteCurrency"] == "USDT" and
                s["enableTrading"] and
                s["baseCurrency"] not in BLACKLIST):
                pairs.append(s["symbol"])  # format: BTC-USDT
        print(f"[PAIRS] Ditemukan {len(pairs)} pair USDT di KuCoin")
        return pairs
    except Exception as e:
        print(f"[PAIRS ERROR] {e}")
        return []

# ── Ambil ticker 24h ──────────────────────────────────
def get_ticker(symbol):
    try:
        r = requests.get(f"{BASE_URL}/api/v1/market/stats",
                         params={"symbol": symbol}, timeout=8)
        d = r.json().get("data", {})
        if not d:
            return None
        last = float(d.get("last", 0) or 0)
        vol  = float(d.get("volValue", 0) or 0)  # sudah dalam USDT
        high = float(d.get("high", 0) or 0)
        low  = float(d.get("low", 0) or 0)
        chg  = float(d.get("changeRate", 0) or 0) * 100
        return {
            "last"      : last,
            "vol_usdt"  : vol,
            "high"      : high,
            "low"       : low,
            "price_chg" : chg,
        }
    except:
        return None

# ── Ambil order book ──────────────────────────────────
def get_order_book(symbol, limit=20):
    try:
        r = requests.get(f"{BASE_URL}/api/v1/market/orderbook/level2_20",
                         params={"symbol": symbol}, timeout=8)
        data = r.json().get("data", {})
        if not data:
            return [], []
        bids = [[float(x[0]), float(x[1])] for x in data.get("bids", [])]
        asks = [[float(x[0]), float(x[1])] for x in data.get("asks", [])]
        return bids, asks
    except:
        return [], []

# ── Ambil trade history ───────────────────────────────
def get_recent_trades(symbol, limit=50):
    try:
        r = requests.get(f"{BASE_URL}/api/v1/market/histories",
                         params={"symbol": symbol}, timeout=8)
        trades = r.json().get("data", [])
        result = []
        for t in trades:
            price  = float(t.get("price", 0))
            qty    = float(t.get("size", 0))
            is_buy = t.get("side") == "buy"
            result.append({
                "type" : "buy" if is_buy else "sell",
                "price": price,
                "qty"  : qty,
                "usdt" : price * qty,
            })
        return result
    except:
        return []

# ── Deteksi sinyal whale ──────────────────────────────
def detect_whale_signals(symbol, ticker, bids, asks, trades):
    signals = []
    score   = 0
    vol_usdt = ticker["vol_usdt"]

    # ── 1. Volume Spike ───────────────────────────────
    hist = volume_history[symbol]
    hist.append(vol_usdt)
    if len(hist) > 20:
        hist.pop(0)

    if len(hist) >= 5:
        avg_vol = sum(hist[:-1]) / len(hist[:-1])
        if avg_vol > 0:
            ratio = vol_usdt / avg_vol
            if ratio >= VOLUME_SPIKE_MULTIPLIER:
                signals.append(f"🔥 Volume spike <b>{round(ratio,1)}x</b> dari rata-rata")
                score += 2
            elif ratio >= 1.5:
                signals.append(f"📈 Volume naik <b>{round(ratio,1)}x</b> dari rata-rata")
                score += 1

    # ── 2. Ask Wall Hilang ────────────────────────────
    total_ask  = sum(p * q for p, q in asks[:15])
    ask_hist   = ask_wall_history[symbol]
    ask_hist.append(total_ask)
    if len(ask_hist) > 10:
        ask_hist.pop(0)

    if len(ask_hist) >= 3:
        prev_ask_avg = sum(ask_hist[:-1]) / len(ask_hist[:-1])
        if prev_ask_avg > 0:
            ask_ratio = total_ask / prev_ask_avg
            if ask_ratio < 0.4:
                signals.append(f"🚪 Ask wall hilang <b>{round((1-ask_ratio)*100)}%</b> — penjual mundur")
                score += 2
            elif ask_ratio < 0.6:
                signals.append(f"📉 Ask wall menipis <b>{round((1-ask_ratio)*100)}%</b>")
                score += 1

    # ── 3. Bid Wall Besar Muncul ──────────────────────
    big_bids = [(p, p*q) for p, q in bids[:15] if p*q >= WALL_USDT_THRESHOLD]
    if big_bids:
        total_big_bid = sum(usdt for _, usdt in big_bids)
        signals.append(f"💚 Bid wall besar: <b>{len(big_bids)} level</b> total ${int(total_big_bid):,}")
        score += 1
        if total_big_bid >= WALL_USDT_THRESHOLD * 5:
            score += 1

    # ── 4. Transaksi Beli Besar Beruntun ──────────────
    big_buys = [t for t in trades if t["type"] == "buy" and t["usdt"] >= BIG_TRADE_USDT]
    if len(big_buys) >= BIG_TRADE_COUNT:
        total_big_buy = sum(t["usdt"] for t in big_buys)
        signals.append(f"🐋 <b>{len(big_buys)} transaksi beli besar</b> total ${int(total_big_buy):,}")
        score += 2

    # ── 5. Spread ketat ───────────────────────────────
    if bids and asks:
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        if best_ask > 0:
            spread_pct = (best_ask - best_bid) / best_ask * 100
            if spread_pct < 0.05:
                signals.append(f"⚡ Spread sangat ketat <b>{round(spread_pct,3)}%</b> — pembeli agresif")
                score += 1

    # ── 6. Harga naik signifikan 24H ─────────────────
    chg = ticker["price_chg"]
    if chg >= 10:
        signals.append(f"🚀 Harga naik <b>+{round(chg,1)}%</b> dalam 24H")
        score += 1

    return score, signals

# ── Format pesan alert ────────────────────────────────
def format_alert(symbol, ticker, score, signals, bids, asks):
    now_str    = datetime.now().strftime("%H:%M:%S")
    last_price = ticker["last"]
    coin       = symbol.replace("-USDT", "")

    support    = bids[0][0] if bids else "-"
    resistance = asks[0][0] if asks else "-"

    signal_text = "\n".join([f"   {s}" for s in signals])

    if score >= 5:
        alert_level = "🚨 AKUMULASI KUAT"
        emoji = "🚨"
    elif score >= 3:
        alert_level = "⚠️ AKUMULASI TERDETEKSI"
        emoji = "⚠️"
    else:
        alert_level = "👀 PERLU DIPERHATIKAN"
        emoji = "👀"

    msg = (
        f"{emoji} <b>WHALE DETECTOR — {coin}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Waktu       : {now_str}\n"
        f"💰 Harga       : ${last_price}\n"
        f"📈 High 24H    : ${ticker['high']}\n"
        f"📉 Low 24H     : ${ticker['low']}\n"
        f"📊 Perubahan   : {round(ticker['price_chg'], 2)}%\n"
        f"💹 Volume 24H  : ${int(ticker['vol_usdt']):,}\n"
        f"🛡️ Support     : ${support}\n"
        f"🚧 Resistance  : ${resistance}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Score: {score} | {alert_level}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Sinyal terdeteksi:</b>\n{signal_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Deteksi akumulasi awal, bukan jaminan pump!\n"
        f"💡 Selalu gunakan risk management!"
    )
    return msg

# ── Main loop ─────────────────────────────────────────
def main():
    print("=" * 55)
    print("  Binance Whale Accumulation Detector")
    print(f"  Exchange  : KuCoin")
    print(f"  Interval  : {CHECK_INTERVAL}s")
    print(f"  Min Volume: ${MIN_VOLUME_USDT:,} USDT")
    print(f"  Min Score : {SCORE_MIN}")
    print("=" * 55)

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN / CHAT_ID belum diset!")
        return

    print("[TEST] Mencoba akses KuCoin API...")
    if not test_connection():
        print("[ERROR] Tidak bisa akses KuCoin, cek koneksi!")
        return

    send_telegram(
        "🐋 <b>Whale Detector KuCoin — ONLINE!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 Exchange  : KuCoin\n"
        "🔍 Deteksi   :\n"
        "   🔥 Volume spike abnormal\n"
        "   🚪 Ask wall tiba-tiba hilang\n"
        "   💚 Bid wall besar muncul\n"
        "   🐋 Transaksi beli besar beruntun\n"
        "   ⚡ Spread ketat\n"
        "   🚀 Harga naik signifikan\n"
        f"⏱️ Interval  : setiap {CHECK_INTERVAL//60} menit\n"
        f"💰 Min Volume: ${MIN_VOLUME_USDT:,} USDT/hari\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Bot berjalan di Railway!"
    )

    # Warmup
    print("[WARMUP] Membangun histori data awal...")
    pairs = get_all_pairs()
    count = 0
    for symbol in pairs[:50]:  # warmup 50 coin dulu
        try:
            ticker = get_ticker(symbol)
            if ticker and ticker["vol_usdt"] >= MIN_VOLUME_USDT:
                volume_history[symbol].append(ticker["vol_usdt"])
                _, asks = get_order_book(symbol)
                total_ask = sum(p*q for p,q in asks[:15]) if asks else 0
                ask_wall_history[symbol].append(total_ask)
                count += 1
        except:
            continue
    print(f"[WARMUP] Selesai, {count} coin ditrack")
    time.sleep(CHECK_INTERVAL)

    while True:
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now_str}] Scanning semua pair KuCoin...")

        pairs = get_all_pairs()
        if not pairs:
            time.sleep(30)
            continue

        alerts_sent = 0

        for symbol in pairs:
            try:
                ticker = get_ticker(symbol)
                if ticker is None or ticker["vol_usdt"] < MIN_VOLUME_USDT:
                    continue

                bids, asks = get_order_book(symbol)
                if not bids or not asks:
                    continue

                trades = get_recent_trades(symbol)
                score, signals = detect_whale_signals(symbol, ticker, bids, asks, trades)

                if score > 0:
                    print(f"[{symbol}] Score: {score} | {len(signals)} sinyal")

                if score < SCORE_MIN:
                    continue

                # Cooldown 30 menit per coin
                last_sent = sent_cache.get(symbol, 0)
                if time.time() - last_sent < 1800:
                    print(f"[{symbol}] Cooldown aktif, skip")
                    continue

                msg = format_alert(symbol, ticker, score, signals, bids, asks)
                send_telegram(msg)
                sent_cache[symbol] = time.time()
                alerts_sent += 1
                print(f"[{symbol}] ✅ Alert dikirim! Score: {score}")

                time.sleep(0.5)

            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")
                continue

        print(f"\n[CYCLE DONE] {alerts_sent} alert dikirim dari {len(pairs)} pair")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
      
