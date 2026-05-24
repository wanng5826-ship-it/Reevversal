"""
=======================================================
  KuCoin Whale Accumulation & Distribution Detector
  Exchange : KuCoin (Public API, no key needed)
  Deteksi  : Akumulasi (BELI) & Distribusi (JUAL)
  Sinyal Beli  :
    1. Volume spike abnormal
    2. Ask wall tiba-tiba hilang
    3. Trade beli besar beruntun
    4. Bid wall besar muncul
    5. Spread ketat
    6. Harga naik signifikan
  Sinyal Jual  :
    1. Bid wall hilang tiba-tiba (pembeli mundur)
    2. Ask wall besar muncul (penjual masuk)
    3. Transaksi jual besar beruntun
    4. Volume spike tapi harga turun (distribusi)
    5. Harga turun dari high 24H signifikan
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
SCORE_MIN               = 2        # minimal score 2 untuk alert

# ── Penyimpanan histori ───────────────────────────────
volume_history   = defaultdict(list)
ask_wall_history = defaultdict(list)
bid_wall_history = defaultdict(list)
price_history    = defaultdict(list)
sent_cache       = {}
sell_sent_cache  = {}

# ── GANTI ke KuCoin ──────────────────────────────────
BASE_URL = "https://api.kucoin.com"

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
        print(f"[TEST] KuCoin API: OK (status {r.status_code})")
        return True
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
            if (s.get("quoteCurrency") == "USDT" and
                s.get("enableTrading") == True):
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
        # changeRate di KuCoin bentuknya decimal (0.05 = 5%), kali 100
        change_rate = float(d.get("changeRate", 0) or 0) * 100
        return {
            "last"      : float(d.get("last", 0) or 0),
            "vol_usdt"  : float(d.get("volValue", 0) or 0),  # volume dalam USDT
            "high"      : float(d.get("high", 0) or 0),
            "low"       : float(d.get("low", 0) or 0),
            "price_chg" : change_rate,
        }
    except:
        return None

# ── Ambil order book ──────────────────────────────────
def get_order_book(symbol, limit=20):
    try:
        r = requests.get(f"{BASE_URL}/api/v1/market/orderbook/level2_20",
                         params={"symbol": symbol}, timeout=8)
        data = r.json().get("data", {})
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
        for t in trades[:limit]:
            price  = float(t.get("price", 0))
            qty    = float(t.get("size", 0))   # KuCoin pakai "size" bukan "qty"
            is_buy = t.get("side", "") == "buy" # KuCoin langsung ada "side": "buy"/"sell"
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

# ── Deteksi sinyal distribusi (JUAL) ─────────────────
def detect_distribution_signals(symbol, ticker, bids, asks, trades):
    signals  = []
    score    = 0
    vol_usdt   = ticker["vol_usdt"]
    last_price = ticker["last"]

    # ── Cek sinyal 2: Ask Wall Besar Muncul ───────────
    has_ask_wall = False
    total_ask = sum(p * q for p, q in asks[:15])
    ask_hist  = ask_wall_history[symbol]
    if len(ask_hist) >= 3:
        prev_ask_avg = sum(ask_hist[:-1]) / len(ask_hist[:-1])
        if prev_ask_avg > 0:
            ask_ratio = total_ask / prev_ask_avg
            if ask_ratio > 2.5:
                has_ask_wall = True
                signals.append(f"🧱 Ask wall meledak <b>{round(ask_ratio,1)}x</b> — penjual masuk besar!")
                score += 2
            elif ask_ratio > 1.7:
                has_ask_wall = True
                signals.append(f"📦 Ask wall naik <b>{round(ask_ratio,1)}x</b> — penjual mulai masuk")
                score += 1

    # ── Cek sinyal 3: Transaksi Jual Besar Beruntun ───
    has_big_sells = False
    big_sells = [t for t in trades if t["type"] == "sell" and t["usdt"] >= BIG_TRADE_USDT]
    if len(big_sells) >= BIG_TRADE_COUNT:
        has_big_sells = True
        total_big_sell = sum(t["usdt"] for t in big_sells)
        signals.append(f"🐳 <b>{len(big_sells)} transaksi jual besar</b> total ${int(total_big_sell):,}")
        score += 2

    # ── Wajib: sinyal 2 DAN 3 harus aktif bersamaan ──
    if not (has_ask_wall and has_big_sells):
        return 0, []

    # ── 1. Bid Wall Hilang (pembeli mundur) ───────────
    total_bid = sum(p * q for p, q in bids[:15])
    bid_hist  = bid_wall_history[symbol]
    bid_hist.append(total_bid)
    if len(bid_hist) > 10:
        bid_hist.pop(0)

    if len(bid_hist) >= 3:
        prev_bid_avg = sum(bid_hist[:-1]) / len(bid_hist[:-1])
        if prev_bid_avg > 0:
            bid_ratio = total_bid / prev_bid_avg
            if bid_ratio < 0.4:
                signals.append(f"🚨 Bid wall hilang <b>{round((1-bid_ratio)*100)}%</b> — pembeli mundur!")
                score += 2
            elif bid_ratio < 0.6:
                signals.append(f"⚠️ Bid wall menipis <b>{round((1-bid_ratio)*100)}%</b>")
                score += 1

    # ── 4. Volume Spike tapi Harga Turun (distribusi) ─
    hist = volume_history[symbol]
    if len(hist) >= 5:
        avg_vol = sum(hist[:-1]) / len(hist[:-1])
        if avg_vol > 0:
            vol_ratio = vol_usdt / avg_vol
            chg = ticker["price_chg"]
            if vol_ratio >= 2.0 and chg <= -3:
                signals.append(f"💀 Volume spike <b>{round(vol_ratio,1)}x</b> tapi harga turun <b>{round(chg,1)}%</b> — DISTRIBUSI!")
                score += 3
            elif vol_ratio >= 1.5 and chg <= -2:
                signals.append(f"⚠️ Volume tinggi tapi harga melemah <b>{round(chg,1)}%</b>")
                score += 1

    # ── 5. Harga Turun dari High 24H ──────────────────
    high_24h = ticker["high"]
    if high_24h > 0:
        drop_from_high = (high_24h - last_price) / high_24h * 100
        if drop_from_high >= 10:
            signals.append(f"📉 Harga turun <b>{round(drop_from_high,1)}%</b> dari high 24H (${high_24h})")
            score += 2
        elif drop_from_high >= 5:
            signals.append(f"⚠️ Harga turun <b>{round(drop_from_high,1)}%</b> dari high 24H")
            score += 1

    return score, signals

# ── Format pesan JUAL ─────────────────────────────────
def format_sell_alert(symbol, ticker, score, signals, bids, asks):
    now_str    = datetime.now().strftime("%H:%M:%S")
    last_price = ticker["last"]
    coin       = symbol.replace("-USDT", "")  # BTC-USDT → BTC

    support    = bids[0][0] if bids else "-"
    resistance = asks[0][0] if asks else "-"

    signal_text = "\n".join([f"   {s}" for s in signals])

    if score >= 5:
        alert_level = "🔴 DISTRIBUSI KUAT — SEGERA JUAL!"
        emoji = "🔴"
    elif score >= 3:
        alert_level = "🟠 DISTRIBUSI TERDETEKSI — PERTIMBANGKAN JUAL"
        emoji = "🟠"
    else:
        alert_level = "🟡 MULAI MELEMAH — WASPADA"
        emoji = "🟡"

    msg = (
        f"{emoji} <b>SELL SIGNAL — {coin}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Waktu       : {now_str}\n"
        f"💰 Harga       : ${last_price}\n"
        f"📈 High 24H    : ${ticker['high']}\n"
        f"📉 Low 24H     : ${ticker['low']}\n"
        f"📊 Perubahan   : {ticker['price_chg']}%\n"
        f"💹 Volume 24H  : ${int(ticker['vol_usdt']):,}\n"
        f"🛡️ Support     : ${support}\n"
        f"🚧 Resistance  : ${resistance}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Score: {score} | {alert_level}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Sinyal terdeteksi:</b>\n{signal_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Bukan jaminan turun, tetap gunakan analisis sendiri!\n"
        f"💡 Selalu gunakan risk management!"
    )
    return msg

# ── Format pesan alert ────────────────────────────────
def format_alert(symbol, ticker, score, signals, bids, asks):
    now_str    = datetime.now().strftime("%H:%M:%S")
    last_price = ticker["last"]
    coin       = symbol.replace("-USDT", "")  # BTC-USDT → BTC

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
        f"📊 Perubahan   : {ticker['price_chg']}%\n"
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
    print("  KuCoin Whale Accumulation Detector")
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
        "🟢 Sinyal BELI:\n"
        "   🔥 Volume spike abnormal\n"
        "   🚪 Ask wall tiba-tiba hilang\n"
        "   💚 Bid wall besar muncul\n"
        "   🐋 Transaksi beli besar beruntun\n"
        "   ⚡ Spread ketat\n"
        "   🚀 Harga naik signifikan\n"
        "🔴 Sinyal JUAL:\n"
        "   🚨 Bid wall hilang (pembeli mundur)\n"
        "   🧱 Ask wall meledak (penjual masuk)\n"
        "   🐳 Transaksi jual besar beruntun\n"
        "   💀 Volume spike + harga turun\n"
        "   📉 Harga turun dari high 24H\n"
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
                bids, asks = get_order_book(symbol)
                total_ask = sum(p*q for p,q in asks[:15]) if asks else 0
                total_bid = sum(p*q for p,q in bids[:15]) if bids else 0
                ask_wall_history[symbol].append(total_ask)
                bid_wall_history[symbol].append(total_bid)
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
                    print(f"[{symbol}] BUY Score: {score} | {len(signals)} sinyal")

                if score >= SCORE_MIN:
                    last_sent = sent_cache.get(symbol, 0)
                    if time.time() - last_sent >= 1800:
                        msg = format_alert(symbol, ticker, score, signals, bids, asks)
                        send_telegram(msg)
                        sent_cache[symbol] = time.time()
                        alerts_sent += 1
                        print(f"[{symbol}] ✅ BUY Alert dikirim! Score: {score}")
                        time.sleep(0.5)
                    else:
                        print(f"[{symbol}] Cooldown BUY aktif, skip")

                # ── Cek sinyal JUAL ───────────────────
                sell_score, sell_signals = detect_distribution_signals(symbol, ticker, bids, asks, trades)

                if sell_score > 0:
                    print(f"[{symbol}] SELL Score: {sell_score} | {len(sell_signals)} sinyal")

                if sell_score >= SCORE_MIN:
                    last_sell_sent = sell_sent_cache.get(symbol, 0)
                    if time.time() - last_sell_sent >= 1800:
                        sell_msg = format_sell_alert(symbol, ticker, sell_score, sell_signals, bids, asks)
                        send_telegram(sell_msg)
                        sell_sent_cache[symbol] = time.time()
                        alerts_sent += 1
                        print(f"[{symbol}] 🔴 SELL Alert dikirim! Score: {sell_score}")
                        time.sleep(0.5)
                    else:
                        print(f"[{symbol}] Cooldown SELL aktif, skip")

            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")
                continue

        print(f"\n[CYCLE DONE] {alerts_sent} alert dikirim dari {len(pairs)} pair")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
