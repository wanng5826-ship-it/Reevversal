"""
=======================================================
  Indodax Whale Accumulation Detector
  Exchange : Indodax (Public API, no key needed)
  Deteksi  : Akumulasi whale tahap awal
  Sinyal   :
    1. Volume spike abnormal
    2. Ask wall tiba-tiba hilang
    3. Trade besar beruntun
    4. Bid wall besar muncul tiba-tiba
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
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "120"))  # 2 menit

# ── Threshold deteksi ─────────────────────────────────
VOLUME_SPIKE_MULTIPLIER = 3.0    # volume sekarang > 3x rata-rata → spike
MIN_VOLUME_IDR          = 10_000_000   # min volume 10 juta IDR (termasuk koin kecil)
WALL_IDR_THRESHOLD      = 3_000_000   # wall dianggap besar jika > 3 juta IDR
BIG_TRADE_IDR           = 1_000_000   # transaksi dianggap besar jika > 1 juta IDR
BIG_TRADE_COUNT         = 3           # minimal 3 transaksi besar dalam 1 siklus
SCORE_MIN               = 2           # minimal 2 sinyal terpenuhi untuk alert

# ── Penyimpanan histori ───────────────────────────────
volume_history  = defaultdict(list)   # histori volume per coin
ask_wall_history= defaultdict(list)   # histori total ask wall per coin
sent_cache      = {}                  # cache sinyal terakhir per coin

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
            print(f"[TELEGRAM] ❌ {r.text}")
    except Exception as e:
        print(f"[TELEGRAM] ❌ {e}")

# ── Ambil semua pair IDR ──────────────────────────────
def get_all_pairs():
    try:
        r = requests.get("https://indodax.com/api/pairs", timeout=10)
        data = r.json()
        pairs = []
        for p in data:
            if p.get("quote_currency", "").lower() == "idr":
                pairs.append({
                    "pair_id"  : p["id"],
                    "symbol"   : p["base_currency"].upper(),
                    "ticker_id": p["ticker_id"],
                })
        return pairs
    except Exception as e:
        print(f"[PAIRS ERROR] {e}")
        return []

# ── Ambil ticker ──────────────────────────────────────
def get_ticker(ticker_id):
    try:
        r = requests.get(f"https://indodax.com/api/ticker/{ticker_id}", timeout=8)
        d = r.json().get("ticker", {})
        return {
            "last"   : float(d.get("last", 0)),
            "vol_idr": float(d.get("vol_idr", 0)),
            "high"   : float(d.get("high", 0)),
            "low"    : float(d.get("low", 0)),
            "buy"    : float(d.get("buy", 0)),
            "sell"   : float(d.get("sell", 0)),
        }
    except:
        return None

# ── Ambil order book ──────────────────────────────────
def get_order_book(pair_id):
    try:
        r = requests.get(f"https://indodax.com/api/{pair_id}/depth", timeout=8)
        data = r.json()
        bids = [[float(x[0]), float(x[1])] for x in data.get("buy", [])]
        asks = [[float(x[0]), float(x[1])] for x in data.get("sell", [])]
        return bids, asks
    except:
        return [], []

# ── Ambil trade history ───────────────────────────────
def get_trade_history(pair_id):
    try:
        r = requests.get(f"https://indodax.com/api/{pair_id}/trades", timeout=8)
        trades = r.json()
        result = []
        for t in trades:
            result.append({
                "type"    : t.get("type", ""),
                "price"   : float(t.get("price", 0)),
                "amount"  : float(t.get("amount", 0)),
                "idr"     : float(t.get("price", 0)) * float(t.get("amount", 0)),
            })
        return result
    except:
        return []

# ── Deteksi sinyal whale ──────────────────────────────
def detect_whale_signals(symbol, ticker, bids, asks, trades):
    signals = []
    score   = 0
    last_price = ticker["last"]
    vol_idr    = ticker["vol_idr"]

    # ── 1. Volume Spike ───────────────────────────────
    hist = volume_history[symbol]
    hist.append(vol_idr)
    if len(hist) > 20:
        hist.pop(0)

    if len(hist) >= 5:
        avg_vol = sum(hist[:-1]) / len(hist[:-1])
        if avg_vol > 0:
            ratio = vol_idr / avg_vol
            if ratio >= VOLUME_SPIKE_MULTIPLIER:
                signals.append(f"🔥 Volume spike <b>{round(ratio,1)}x</b> dari rata-rata")
                score += 2
            elif ratio >= 1.5:
                signals.append(f"📈 Volume naik <b>{round(ratio,1)}x</b> dari rata-rata")
                score += 1

    # ── 2. Ask Wall Hilang ────────────────────────────
    top_asks    = asks[:15]
    total_ask   = sum(p * q for p, q in top_asks)
    ask_hist    = ask_wall_history[symbol]
    ask_hist.append(total_ask)
    if len(ask_hist) > 10:
        ask_hist.pop(0)

    if len(ask_hist) >= 3:
        prev_ask_avg = sum(ask_hist[:-1]) / len(ask_hist[:-1])
        if prev_ask_avg > 0:
            ask_ratio = total_ask / prev_ask_avg
            if ask_ratio < 0.4:
                signals.append(f"🚪 Ask wall turun drastis (<b>{round((1-ask_ratio)*100)}%</b> hilang) — penjual mundur")
                score += 2
            elif ask_ratio < 0.6:
                signals.append(f"📉 Ask wall menipis (<b>{round((1-ask_ratio)*100)}%</b> berkurang)")
                score += 1

    # ── 3. Bid Wall Besar Muncul ──────────────────────
    big_bids = [(p, p*q) for p, q in bids[:15] if p*q >= WALL_IDR_THRESHOLD]
    if big_bids:
        total_big_bid = sum(idr for _, idr in big_bids)
        signals.append(f"💚 Bid wall besar: <b>{len(big_bids)} level</b> total {int(total_big_bid/1_000_000)}jt IDR")
        score += 1
        if total_big_bid >= WALL_IDR_THRESHOLD * 5:
            score += 1  # bonus kalau sangat besar

    # ── 4. Transaksi Beli Besar Beruntun ──────────────
    recent_trades = trades[:30]  # 30 transaksi terakhir
    big_buys = [t for t in recent_trades if t["type"] == "buy" and t["idr"] >= BIG_TRADE_IDR]
    big_sells= [t for t in recent_trades if t["type"] == "sell" and t["idr"] >= BIG_TRADE_IDR]

    if len(big_buys) >= BIG_TRADE_COUNT:
        total_big_buy = sum(t["idr"] for t in big_buys)
        signals.append(f"🐋 <b>{len(big_buys)} transaksi beli besar</b> total {int(total_big_buy/1_000_000)}jt IDR")
        score += 2

    # ── 5. Spread ketat (pembeli agresif) ─────────────
    if bids and asks:
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        if best_ask > 0:
            spread_pct = (best_ask - best_bid) / best_ask * 100
            if spread_pct < 0.5:
                signals.append(f"⚡ Spread sangat ketat <b>{round(spread_pct,2)}%</b> — pembeli agresif")
                score += 1

    return score, signals

# ── Format pesan alert ────────────────────────────────
def format_alert(symbol, ticker, score, signals, bids, asks):
    now_str    = datetime.now().strftime("%H:%M:%S")
    last_price = ticker["last"]
    high_24h   = ticker["high"]
    low_24h    = ticker["low"]
    vol_idr    = ticker["vol_idr"]

    # Level support & resistance dari order book
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
        f"{emoji} <b>WHALE DETECTOR — {symbol}/IDR</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Waktu      : {now_str}\n"
        f"💰 Harga      : {int(last_price):,} IDR\n"
        f"📈 High 24H   : {int(high_24h):,} IDR\n"
        f"📉 Low 24H    : {int(low_24h):,} IDR\n"
        f"💹 Volume 24H : {int(vol_idr/1_000_000)} jt IDR\n"
        f"🛡️ Support    : {int(support):,} IDR\n"
        f"🚧 Resistance : {int(resistance):,} IDR\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Score: {score} | {alert_level}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Sinyal yang terdeteksi:</b>\n{signal_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Ini deteksi akumulasi awal, bukan jaminan pump!\n"
        f"💡 Selalu gunakan risk management!"
    )
    return msg

# ── Main loop ─────────────────────────────────────────
def main():
    print("=" * 55)
    print("  Indodax Whale Accumulation Detector")
    print(f"  Interval : {CHECK_INTERVAL}s")
    print(f"  Min Score: {SCORE_MIN} sinyal")
    print("=" * 55)

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN / CHAT_ID belum diset!")
        return

    send_telegram(
        "🐋 <b>Whale Detector — ONLINE!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 Exchange  : Indodax\n"
        "🔍 Deteksi   :\n"
        "   🔥 Volume spike abnormal\n"
        "   🚪 Ask wall tiba-tiba hilang\n"
        "   💚 Bid wall besar muncul\n"
        "   🐋 Transaksi beli besar beruntun\n"
        "   ⚡ Spread ketat (pembeli agresif)\n"
        f"⏱️ Interval  : setiap {CHECK_INTERVAL//60} menit\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Bot berjalan di Railway!"
    )

    # Warmup: 2 siklus pertama untuk bangun histori
    print("[WARMUP] Membangun histori data awal...")
    pairs = get_all_pairs()
    for p in pairs:
        try:
            ticker = get_ticker(p["ticker_id"])
            if ticker and ticker["vol_idr"] >= MIN_VOLUME_IDR:
                volume_history[p["symbol"]].append(ticker["vol_idr"])
                _, asks = get_order_book(p["pair_id"])
                total_ask = sum(float(x[0])*float(x[1]) for x in asks[:15]) if asks else 0
                ask_wall_history[p["symbol"]].append(total_ask)
        except:
            continue
    print(f"[WARMUP] Selesai, {len(volume_history)} coin ditrack")
    time.sleep(CHECK_INTERVAL)

    while True:
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now_str}] Scanning semua pair Indodax...")

        pairs = get_all_pairs()
        if not pairs:
            time.sleep(30)
            continue

        alerts_sent = 0

        for p in pairs:
            pair_id   = p["pair_id"]
            symbol    = p["symbol"]
            ticker_id = p["ticker_id"]

            try:
                ticker = get_ticker(ticker_id)
                if ticker is None or ticker["vol_idr"] < MIN_VOLUME_IDR:
                    continue

                bids, asks = get_order_book(pair_id)
                if not bids or not asks:
                    continue

                trades = get_trade_history(pair_id)

                score, signals = detect_whale_signals(symbol, ticker, bids, asks, trades)

                print(f"[{symbol}] Score: {score} | Signals: {len(signals)}")

                if score < SCORE_MIN:
                    continue

                # Anti spam: cooldown 30 menit per coin
                last_sent = sent_cache.get(symbol, 0)
                if time.time() - last_sent < 1800:
                    print(f"[{symbol}] Cooldown aktif, skip")
                    continue

                msg = format_alert(symbol, ticker, score, signals, bids, asks)
                send_telegram(msg)
                sent_cache[symbol] = time.time()
                alerts_sent += 1
                print(f"[{symbol}] ✅ Alert dikirim! Score: {score}")

                time.sleep(1)

            except Exception as e:
                print(f"[ERROR] {symbol}: {e}")
                continue

        print(f"\n[CYCLE DONE] {alerts_sent} alert dikirim dari {len(pairs)} pair")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
          
