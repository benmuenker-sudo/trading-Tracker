#!/usr/bin/env python3
"""Krypto-Radar Paper-Trading-Bot (nur Simulation, kein echtes Geld).

Ablauf bei jedem Lauf:
 1. Markt laden: Top ~1000 Coins (CoinGecko) + optional neue Kleinst-Token (DexScreener).
 2. "Smart Money": erfolgreiche Trader auf Hyperliquid auswaehlen und ihre offenen
    Positionen beobachten (oeffentliche Daten). Aenderungen = Signale.
 3. Jeden Coin bewerten (Momentum, Ausbruch, Volumen, relative Staerke, Smart Money, Groesse).
 4. Offene Papier-Positionen pruefen (Stopp, Trailing, Ziel, Zeit) und ggf. schliessen.
 5. Neue Papier-Positionen eroeffnen - mit Gebuehren und Kursabweichung (Slippage).
 6. Aus den abgeschlossenen Trades lernen: Gewichte der Signalarten anpassen.
 7. Alles in state.json speichern (wird vom Tracker angezeigt).

Nur Python-Standardbibliothek. Keine API-Schluessel noetig.
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- Einstellungen
START_EQUITY = 10_000.0          # virtuelles Startkapital in USD
MAX_OPEN = 12                    # maximal gleichzeitig offene Positionen
MAX_NEW_PER_RUN = 3              # maximal neue Positionen pro Lauf
MAX_EXPOSURE = 0.60              # max. Anteil des Kapitals, der investiert sein darf
FEE = 0.001                      # 0,10 % Gebuehr pro Seite
STOP_PCT = 0.08                  # Anfangs-Stopp: -8 %
TARGET_PCT = 0.30                # Ziel: +30 %
TRAIL_START = 0.10               # Trailing-Stopp ab +10 % ...
TRAIL_GAP = 0.08                 # ... mit 8 % Abstand zum Hoechststand
TIME_STOP_DAYS = 10              # nach 10 Tagen raus, wenn nicht mindestens +5 %
TIME_STOP_MIN_GAIN = 0.05
COOLDOWN_DAYS = 3                # gleicher Coin erst nach 3 Tagen wieder
ENTRY_SCORE = 40.0               # Mindestpunktzahl (0-100) fuer einen Einstieg
MIN_CONFIRM = 2                  # mindestens so viele Signalarten muessen >= 0.4 sein
MIN_VOL_CG = 750_000             # Mindest-Tagesvolumen CoinGecko-Coins (USD)
DEX_MIN_LIQ = 75_000             # Mindest-Liquiditaet DEX-Token (USD)
DEX_MIN_VOL = 100_000
DEX_SIZE_FACTOR = 0.35           # DEX-Token bekommen nur einen Bruchteil der normalen Groesse
TRADERS_N = 25                   # so viele Trader beobachten
TRADERS_REFRESH_H = 24
MIN_POS_VALUE = 25_000           # Positionen unter diesem Wert (USD) zaehlen nicht als Signal
WEIGHT_MIN_TRADES = 8            # ab so vielen Trades pro Signalart wird gelernt

BASE_WEIGHTS = {"mom": 0.22, "brk": 0.22, "vol": 0.18, "rs": 0.13, "smart": 0.20, "small": 0.05}
LABELS = {"mom": "Momentum", "brk": "Ausbruch", "vol": "Volumen-Spike",
          "rs": "Stärke vs. Bitcoin", "smart": "Smart Money", "small": "Kleiner Coin"}

STABLE_SYMS = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDS", "PYUSD", "USDD", "USDP",
               "GUSD", "FRAX", "LUSD", "BUSD", "USD1", "RLUSD", "USDY", "USDG", "EURC", "EURT"}

UA = {"User-Agent": "krypto-radar-bot/1.0", "Accept": "application/json",
      "Content-Type": "application/json"}


def now_ts():
    return time.time()


def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else now_ts(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def num(x, d=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except (TypeError, ValueError):
        return d


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------- Datenquellen
def http_json(url, data=None, retries=3, timeout=40):
    body = json.dumps(data).encode() if data is not None else None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            time.sleep(25 * (a + 1) if e.code == 429 else 2 * (a + 1))
        except Exception:
            time.sleep(2 * (a + 1))
    return None


def is_stable_or_wrapped(c):
    sym, name, price = c["sym"], c["name"].lower(), c["price"]
    if sym in STABLE_SYMS or "usd" in sym.lower() and abs(price - 1) < 0.05:
        return True
    if abs(price - 1) < 0.03 and abs(c["c7d"]) < 1.5 and abs(c["c24h"]) < 1.0:
        return True
    return any(w in name for w in ("wrapped", "staked", "bridged", "liquid staking", "restaked"))


class Sources:
    """Alle Netzwerkzugriffe. In Tests wird diese Klasse ersetzt."""

    def markets(self, pages=4):
        out = []
        for p in range(1, pages + 1):
            url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc"
                   "&per_page=250&page=%d&sparkline=false&price_change_percentage=1h,24h,7d,30d" % p)
            rows = http_json(url)
            if not isinstance(rows, list):
                log("CoinGecko Seite %d fehlgeschlagen" % p)
                break
            for r in rows:
                price = num(r.get("current_price"))
                if price <= 0:
                    continue
                out.append({
                    "id": r.get("id"), "sym": str(r.get("symbol", "")).upper(), "name": r.get("name") or "",
                    "price": price, "mcap": num(r.get("market_cap")), "vol": num(r.get("total_volume")),
                    "rank": r.get("market_cap_rank") or 9999,
                    "c1h": num(r.get("price_change_percentage_1h_in_currency")),
                    "c24h": num(r.get("price_change_percentage_24h_in_currency")),
                    "c7d": num(r.get("price_change_percentage_7d_in_currency")),
                    "c30d": num(r.get("price_change_percentage_30d_in_currency")),
                    "img": r.get("image") or "", "kind": "cg"})
            time.sleep(2.5)
        return out

    def leaderboard(self):
        return http_json("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", timeout=120)

    def positions(self, addr):
        d = http_json("https://api.hyperliquid.xyz/info", {"type": "clearinghouseState", "user": addr})
        time.sleep(0.25)
        return d

    def dex_candidates(self):
        """Neue/gehypte Kleinst-Token ueber DexScreener."""
        res = []
        seen = set()
        refs = []
        for u in ("https://api.dexscreener.com/token-boosts/top/v1",
                  "https://api.dexscreener.com/token-boosts/latest/v1",
                  "https://api.dexscreener.com/token-profiles/latest/v1"):
            lst = http_json(u)
            if isinstance(lst, list):
                for x in lst:
                    k = (x.get("chainId"), x.get("tokenAddress"))
                    if k[0] and k[1] and k not in seen:
                        seen.add(k)
                        refs.append(k)
            time.sleep(1.0)
        by_chain = {}
        for ch, ad in refs:
            by_chain.setdefault(ch, []).append(ad)
        for ch, addrs in by_chain.items():
            for i in range(0, len(addrs), 30):
                res += parse_dex_pairs(http_json("https://api.dexscreener.com/tokens/v1/%s/%s" % (ch, ",".join(addrs[i:i + 30]))))
                time.sleep(1.0)
        return res

    def dex_prices(self, refs):
        out = {}
        by_chain = {}
        for ch, ad in refs:
            by_chain.setdefault(ch, []).append(ad)
        for ch, addrs in by_chain.items():
            for i in range(0, len(addrs), 30):
                for c in parse_dex_pairs(http_json("https://api.dexscreener.com/tokens/v1/%s/%s" % (ch, ",".join(addrs[i:i + 30])))):
                    out[c["id"]] = c["price"]
                time.sleep(1.0)
        return out


def parse_dex_pairs(pairs):
    """DexScreener-Paare -> Kandidaten (je Token das liquideste Paar)."""
    best = {}
    for p in pairs if isinstance(pairs, list) else []:
        try:
            base = p.get("baseToken") or {}
            liq = num((p.get("liquidity") or {}).get("usd"))
            price = num(p.get("priceUsd"))
            if not base.get("address") or price <= 0:
                continue
            cid = "dex:%s:%s" % (p.get("chainId"), base["address"])
            if cid in best and best[cid]["liq"] >= liq:
                continue
            pc = p.get("priceChange") or {}
            created = num(p.get("pairCreatedAt")) / 1000.0
            best[cid] = {"id": cid, "sym": str(base.get("symbol", "?")).upper(), "name": base.get("name") or "",
                         "price": price, "mcap": num(p.get("fdv") or p.get("marketCap")), "liq": liq,
                         "vol": num((p.get("volume") or {}).get("h24")), "rank": 9999,
                         "c1h": num(pc.get("h1")), "c24h": num(pc.get("h24")), "c7d": 0.0, "c30d": 0.0,
                         "age_h": (now_ts() - created) / 3600.0 if created > 0 else 0.0,
                         "chain": p.get("chainId"), "addr": base["address"],
                         "img": "", "kind": "dex"}
        except Exception:
            continue
    return list(best.values())


# ----------------------------------------------------------------- Trader (Smart Money)
def parse_leaderboard(raw):
    rows = raw.get("leaderboardRows", []) if isinstance(raw, dict) else (raw or [])
    out = []
    for r in rows:
        try:
            perf = {k: v for k, v in r.get("windowPerformances", [])}
            g = lambda w, f: num((perf.get(w) or {}).get(f))
            out.append({"addr": r["ethAddress"], "name": r.get("displayName"), "av": num(r.get("accountValue")),
                        "week_pnl": g("week", "pnl"), "week_roi": g("week", "roi"),
                        "month_pnl": g("month", "pnl"), "month_roi": g("month", "roi"), "month_vlm": g("month", "vlm"),
                        "all_pnl": g("allTime", "pnl"), "all_roi": g("allTime", "roi")})
        except Exception:
            continue
    return out


def select_traders(rows, n=TRADERS_N):
    """Konstant profitable Trader: Gewinn ueber Woche, Monat und gesamt, nicht nur ein Glueckstreffer,
    ausreichend Kapital, und kein extrem hoher Umschlag (typisch fuer Market-Maker/Bots)."""
    cands = []
    for t in rows:
        if t["av"] < 100_000:
            continue
        if min(t["week_pnl"], t["month_pnl"], t["all_pnl"]) <= 0:
            continue
        if t["month_roi"] < 0.05 or t["all_roi"] < 0.2:
            continue
        if t["month_vlm"] / max(t["av"], 1) > 400:
            continue
        score = (math.log10(max(t["all_pnl"], 1)) + 2 * min(t["month_roi"], 1.0)
                 + 2 * min(max(t["week_roi"], 0), 0.5) + 0.5 * math.log10(t["av"]))
        cands.append((score, t))
    cands.sort(key=lambda x: -x[0])
    out = []
    for score, t in cands[:n]:
        out.append({"addr": t["addr"], "name": t["name"] or (t["addr"][:6] + "…" + t["addr"][-4:]),
                    "score": round(score, 2), "av": round(t["av"]), "month_roi": round(t["month_roi"], 3),
                    "all_pnl": round(t["all_pnl"]), "month_pnl": round(t["month_pnl"])})
    return out


def parse_positions(raw):
    pos = {}
    for ap in (raw or {}).get("assetPositions", []) if isinstance(raw, dict) else []:
        p = ap.get("position") or {}
        coin = p.get("coin")
        szi = num(p.get("szi"))
        if not coin or szi == 0:
            continue
        pos[coin] = {"szi": szi, "value": num(p.get("positionValue")), "entry": num(p.get("entryPx")),
                     "upnl": num(p.get("unrealizedPnl")), "lev": num((p.get("leverage") or {}).get("value"))}
    return pos


def hl_symbol(coin):
    """Hyperliquid-Name -> Symbol (z. B. kPEPE -> PEPE)."""
    if len(coin) > 1 and coin[0] == "k" and coin[1:].isupper():
        return coin[1:]
    return coin.upper()


def diff_positions(addr, name, prev, new, ts):
    ev = []
    for coin in set(prev) | set(new):
        p, n = prev.get(coin), new.get(coin)
        ps, ns = (p or {}).get("szi", 0.0), (n or {}).get("szi", 0.0)
        val = (n or p or {}).get("value", 0.0)
        if val < MIN_POS_VALUE:
            continue
        side = "long" if (ns or ps) > 0 else "short"
        t = None
        if ps == 0 and ns != 0:
            t = "open"
        elif ps != 0 and ns == 0:
            t = "close"
        elif ps * ns < 0:
            t, side = "flip", ("long" if ns > 0 else "short")
        elif abs(ns) > abs(ps) * 1.25:
            t = "add"
        elif abs(ns) < abs(ps) * 0.75:
            t = "reduce"
        if t:
            ev.append({"t": iso(ts), "trader": name, "addr": addr, "coin": coin, "type": t, "side": side,
                       "value": round(val)})
    return ev


def smart_scores(trader_pos, events, ts):
    """Pro Symbol: Punktzahl 0-100 aus (a) wie viele beobachtete Trader long/short sind,
    (b) wie viele in den letzten 24 h neu long eingestiegen sind."""
    agg = {}
    for addr, pos in trader_pos.items():
        for coin, p in pos.items():
            if p["value"] < MIN_POS_VALUE:
                continue
            a = agg.setdefault(hl_symbol(coin), {"long": 0, "short": 0, "recent": 0, "traders": []})
            if p["szi"] > 0:
                a["long"] += 1
                a["traders"].append(addr)
            else:
                a["short"] += 1
    cutoff = ts - 24 * 3600
    for e in events:
        try:
            et = datetime.strptime(e["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
        if et >= cutoff and e["side"] == "long" and e["type"] in ("open", "add", "flip"):
            a = agg.setdefault(hl_symbol(e["coin"]), {"long": 0, "short": 0, "recent": 0, "traders": []})
            a["recent"] += 1
    out = {}
    for sym, a in agg.items():
        s = 22 * min(a["recent"], 3) + 11 * min(a["long"], 5) - 16 * a["short"]
        out[sym] = {"score": clamp(s, 0, 100), "long": a["long"], "short": a["short"], "recent": a["recent"]}
    return out


# ----------------------------------------------------------------- Bewertung
def slippage(c):
    if c["kind"] == "dex":
        return 0.02
    r = c["rank"]
    return 0.0005 if r <= 50 else 0.0015 if r <= 200 else 0.004 if r <= 500 else 0.008


def components(c, btc7, smart):
    """Signalstaerken 0..1."""
    mom = 0.6 * clamp(c["c7d"] / 35.0) + 0.4 * clamp(c["c30d"] / 100.0)
    if c["c24h"] < -3:
        mom *= 0.3
    brk = 0.0
    if c["c1h"] > 0.3 and 2 <= c["c24h"] <= 35:
        brk = clamp((c["c24h"] - 2) / 15.0) * (1.0 if c["c1h"] > 0.8 else 0.6)
    basis = c.get("liq") or c["mcap"] or 1.0
    ratio = c["vol"] / basis if basis else 0.0
    vol = clamp((ratio - 0.08) / 0.4)
    rs = clamp((c["c7d"] - btc7) / 25.0) if c["kind"] == "cg" else 0.0
    sm = (smart.get(c["sym"], {}).get("score", 0) / 100.0) if c["kind"] == "cg" and c["rank"] <= 400 else 0.0
    small = 0.0
    if c["kind"] == "dex":
        small = 1.0
    elif c["rank"] > 100 and c["vol"] >= 1_000_000:
        small = clamp((c["rank"] - 100) / 600.0)
    return {"mom": mom, "brk": brk, "vol": vol, "rs": rs, "smart": sm, "small": small}


def total_score(comp, adj):
    tot = sum(BASE_WEIGHTS[k] * adj.get(k, 1.0) * v for k, v in comp.items())
    return 100.0 * tot / sum(BASE_WEIGHTS.values())


def tradable(c):
    if c["kind"] == "dex":
        return (c["liq"] >= DEX_MIN_LIQ and c["vol"] >= DEX_MIN_VOL and c.get("age_h", 0) >= 24
                and 0 <= c["c24h"] <= 80 and c["c1h"] > -2)
    return c["vol"] >= MIN_VOL_CG and not is_stable_or_wrapped(c)


# ----------------------------------------------------------------- Konto
def new_state():
    return {"v": 1, "start_equity": START_EQUITY, "cash": START_EQUITY, "equity": START_EQUITY, "runs": 0,
            "created": iso(), "updated": iso(), "btc_start": None, "btc_now": None,
            "positions": [], "trades": [], "equity_curve": [], "weights": {k: 1.0 for k in BASE_WEIGHTS},
            "traders": [], "traders_t": 0, "trader_pos": {}, "events": [], "signals": [], "market": {},
            "cooldown": {}, "log": []}


def mark_equity(st, prices):
    eq = st["cash"]
    for p in st["positions"]:
        eq += p["qty"] * prices.get(p["id"], p.get("last", p["entry"]))
    return eq


def close_position(st, p, price, reason, ts):
    exit_px = price * (1 - p["slip"])
    gross = p["qty"] * exit_px
    fee = gross * FEE
    st["cash"] += gross - fee
    cost = p["size"] + p["fee_in"]
    pnl = gross - fee - cost
    t = {"id": p["id"], "sym": p["sym"], "name": p["name"], "kind": p["kind"], "t_in": p["t_in"], "t_out": iso(ts),
         "entry": p["entry"], "exit": exit_px, "size": round(p["size"], 2), "pnl": round(pnl, 2),
         "pnl_pct": round(100 * pnl / cost, 2) if cost else 0.0, "reason": reason, "score": p["score"],
         "comp": p["comp"], "why": p["why"], "days": round((ts - p["ts_in"]) / 86400, 2)}
    st["trades"].append(t)
    st["trades"] = st["trades"][-500:]
    st["cooldown"][p["id"]] = ts
    return t


def check_exits(st, prices, ts):
    keep, closed = [], []
    for p in st["positions"]:
        px = prices.get(p["id"])
        if px is None or px <= 0:
            p["missed"] = p.get("missed", 0) + 1
            if p["missed"] > 96:
                closed.append(close_position(st, p, p.get("last", p["entry"]), "Kein Kurs mehr", ts))
            else:
                keep.append(p)
            continue
        p["missed"] = 0
        p["last"] = px
        p["hw"] = max(p["hw"], px)
        reason = None
        entry = p["entry"]
        if px <= p["stop"]:
            reason = "Stopp"
        elif px >= p["target"]:
            reason = "Ziel erreicht"
        elif p["hw"] >= entry * (1 + TRAIL_START) and px <= p["hw"] * (1 - TRAIL_GAP):
            reason = "Trailing-Stopp"
        elif (ts - p["ts_in"]) > TIME_STOP_DAYS * 86400 and px < entry * (1 + TIME_STOP_MIN_GAIN):
            reason = "Zeit-Stopp"
        if reason:
            closed.append(close_position(st, p, px, reason, ts))
        else:
            keep.append(p)
    st["positions"] = keep
    return closed


def open_position(st, c, comp, score, size, ts):
    slip = slippage(c)
    entry = c["price"] * (1 + slip)
    fee = size * FEE
    if size + fee > st["cash"]:
        return None
    qty = size / entry
    st["cash"] -= size + fee
    why = [LABELS[k] for k, v in sorted(comp.items(), key=lambda kv: -BASE_WEIGHTS[kv[0]] * kv[1]) if v >= 0.4][:4]
    p = {"id": c["id"], "sym": c["sym"], "name": c["name"], "kind": c["kind"], "img": c.get("img", ""),
         "t_in": iso(ts), "ts_in": ts, "entry": entry, "qty": qty, "size": size, "fee_in": fee, "slip": slip,
         "stop": entry * (1 - STOP_PCT), "target": entry * (1 + TARGET_PCT), "hw": entry, "last": c["price"],
         "score": round(score, 1), "comp": {k: round(v, 2) for k, v in comp.items()}, "why": why,
         "chain": c.get("chain"), "addr": c.get("addr"), "rank": c["rank"]}
    st["positions"].append(p)
    return p


# ----------------------------------------------------------------- Lernen
def learn(st):
    """Gewichte je Signalart anpassen: Trades, in denen eine Signalart stark war (>= 0.5),
    werden mit dem Durchschnitt aller Trades verglichen. Begrenzt auf 0,5x - 1,5x und mit
    Vorsicht bei wenigen Trades."""
    tr = st["trades"]
    if len(tr) < WEIGHT_MIN_TRADES:
        return
    mean_all = sum(t["pnl_pct"] for t in tr) / len(tr)
    for k in BASE_WEIGHTS:
        hi = [t["pnl_pct"] for t in tr if t["comp"].get(k, 0) >= 0.5]
        if len(hi) < WEIGHT_MIN_TRADES:
            st["weights"][k] = 1.0
            continue
        diff = (sum(hi) / len(hi) - mean_all) / 100.0
        shrink = len(hi) / (len(hi) + 10.0)
        st["weights"][k] = round(clamp(1 + 0.5 * math.tanh(diff / 0.05) * shrink, 0.5, 1.5), 3)


def stats(st):
    tr = st["trades"]
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    peak, mdd = 0.0, 0.0
    for _, eq, _b in st["equity_curve"]:
        peak = max(peak, eq)
        if peak:
            mdd = max(mdd, (peak - eq) / peak)
    by = {}
    for t in tr:
        for k, v in t["comp"].items():
            if v >= 0.5:
                b = by.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
                b["n"] += 1
                b["wins"] += 1 if t["pnl"] > 0 else 0
                b["pnl"] += t["pnl"]
    return {"n": len(tr), "win_rate": round(100 * len(wins) / len(tr), 1) if tr else None,
            "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
            "profit_factor": round(gw / gl, 2) if gl > 0 else None, "max_drawdown": round(100 * mdd, 2),
            "by_comp": {k: {"n": v["n"], "win_rate": round(100 * v["wins"] / v["n"], 1), "pnl": round(v["pnl"], 2)}
                        for k, v in by.items()}}


# ----------------------------------------------------------------- Hauptlauf
def update_traders(st, src, ts):
    """Trader auswaehlen (taeglich) und aktuelle Positionen/Aenderungen einlesen."""
    if ts - st.get("traders_t", 0) > TRADERS_REFRESH_H * 3600 or not st["traders"]:
        raw = src.leaderboard()
        rows = parse_leaderboard(raw) if raw else []
        if rows:
            st["traders"] = select_traders(rows)
            st["traders_t"] = ts
            log("Trader ausgewaehlt: %d" % len(st["traders"]))
        else:
            log("Leaderboard nicht verfuegbar - behalte alte Trader")
    new_pos = {}
    for t in st["traders"]:
        raw = src.positions(t["addr"])
        if raw is None:
            if t["addr"] in st["trader_pos"]:
                new_pos[t["addr"]] = st["trader_pos"][t["addr"]]
            continue
        new_pos[t["addr"]] = parse_positions(raw)
        if t["addr"] in st["trader_pos"]:
            st["events"] += diff_positions(t["addr"], t["name"], st["trader_pos"][t["addr"]], new_pos[t["addr"]], ts)
    st["trader_pos"] = new_pos
    st["events"] = st["events"][-300:]
    # kompakte Anzeige-Positionen je Trader
    for t in st["traders"]:
        ps = new_pos.get(t["addr"], {})
        t["positions"] = sorted(({"coin": c, "side": "long" if p["szi"] > 0 else "short", "value": round(p["value"]),
                                  "upnl": round(p["upnl"]), "lev": p["lev"]} for c, p in ps.items()
                                 if p["value"] >= MIN_POS_VALUE), key=lambda x: -x["value"])[:8]


def run(st, src, ts=None, use_dex=True):
    ts = ts if ts is not None else now_ts()
    st["runs"] += 1
    cg = src.markets()
    if len(cg) < 200:
        raise RuntimeError("Zu wenige Marktdaten (%d) - Lauf abgebrochen, nichts veraendert" % len(cg))
    btc = next((c for c in cg if c["id"] == "bitcoin"), None)
    btc_px = btc["price"] if btc else None
    if btc_px and not st["btc_start"]:
        st["btc_start"] = btc_px
    st["btc_now"] = btc_px
    btc7 = btc["c7d"] if btc else 0.0
    btc24 = btc["c24h"] if btc else 0.0

    try:
        update_traders(st, src, ts)
    except Exception as e:  # Smart-Money-Fehler duerfen den Lauf nicht stoppen
        log("Trader-Update Fehler: %r" % e)
    smart = smart_scores(st["trader_pos"], st["events"], ts)

    dex = []
    if use_dex:
        try:
            dex = src.dex_candidates()
        except Exception as e:
            log("DexScreener Fehler: %r" % e)
    universe = {c["id"]: c for c in cg}
    for c in dex:
        universe.setdefault(c["id"], c)

    prices = {i: c["price"] for i, c in universe.items()}
    # Kurse fuer offene DEX-Positionen, die nicht in den Kandidaten sind
    miss = [(p["chain"], p["addr"]) for p in st["positions"] if p["kind"] == "dex" and p["id"] not in prices]
    if miss:
        try:
            prices.update(src.dex_prices(miss))
        except Exception as e:
            log("DEX-Preise Fehler: %r" % e)

    closed = check_exits(st, prices, ts)
    if closed:
        learn(st)

    regime_ok = not (btc24 < -6 or btc7 < -15)
    ranked = []
    for c in universe.values():
        if not tradable(c):
            continue
        comp = components(c, btc7, smart)
        score = total_score(comp, st["weights"])
        confirm = sum(1 for v in comp.values() if v >= 0.4)
        ranked.append((score, confirm, c, comp))
    ranked.sort(key=lambda x: -x[0])
    st["signals"] = [{"id": c["id"], "sym": c["sym"], "name": c["name"], "kind": c["kind"], "rank": c["rank"],
                      "price": c["price"], "c24h": round(c["c24h"], 2), "c7d": round(c["c7d"], 2),
                      "score": round(s, 1), "confirm": cf, "comp": {k: round(v, 2) for k, v in comp.items()},
                      "img": c.get("img", "")} for s, cf, c, comp in ranked[:30]]

    opened = []
    equity = mark_equity(st, prices)
    invested = equity - st["cash"]
    held = {p["id"] for p in st["positions"]}
    if regime_ok:
        for score, confirm, c, comp in ranked:
            if len(opened) >= MAX_NEW_PER_RUN or len(st["positions"]) >= MAX_OPEN:
                break
            if score < ENTRY_SCORE:
                break
            if confirm < MIN_CONFIRM or c["id"] in held:
                continue
            if ts - st["cooldown"].get(c["id"], 0) < COOLDOWN_DAYS * 86400:
                continue
            frac = 0.03 if score >= 60 else 0.02 if score >= 50 else 0.01
            size = equity * frac * (DEX_SIZE_FACTOR if c["kind"] == "dex" else 1.0)
            size = min(size, 0.005 * max(c.get("liq") or c["vol"], 0))
            if size < 25 or invested + size > equity * MAX_EXPOSURE:
                continue
            p = open_position(st, c, comp, score, size, ts)
            if p:
                opened.append(p)
                invested += size
                held.add(c["id"])

    equity = mark_equity(st, prices)
    st["equity"] = round(equity, 2)
    btc_val = round(st["start_equity"] * btc_px / st["btc_start"], 2) if btc_px and st["btc_start"] else None
    st["equity_curve"].append([iso(ts), round(equity, 2), btc_val])
    st["equity_curve"] = st["equity_curve"][-3000:]
    st["market"] = {"coins": len(cg), "dex": len(dex), "btc24h": round(btc24, 2), "btc7d": round(btc7, 2),
                    "regime_ok": regime_ok, "traders": len(st["traders"])}
    st["stats"] = stats(st)
    st["updated"] = iso(ts)
    line = "%s: Kapital %.2f, offen %d, neu %d, geschlossen %d" % (iso(ts), equity, len(st["positions"]), len(opened), len(closed))
    st["log"] = (st["log"] + [line])[-40:]
    log(line)
    return opened, closed


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        base = new_state()
        base.update(st)
        return base
    except FileNotFoundError:
        return new_state()


def save_state(st, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    path = os.environ.get("BOT_STATE", "state.json")
    st = load_state(path)
    try:
        run(st, Sources(), use_dex=os.environ.get("BOT_DEX", "1") != "0")
    except Exception as e:
        log("FEHLER: %r" % e)
        st["log"] = (st.get("log", []) + ["%s: FEHLER %r" % (iso(), e)])[-40:]
        save_state(st, path)
        return 1
    save_state(st, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
