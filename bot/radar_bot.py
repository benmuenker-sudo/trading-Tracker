#!/usr/bin/env python3
"""Krypto-Radar Paper-Trading-Bot (nur Simulation, kein echtes Geld).

Ablauf bei jedem Lauf:
 1. Markt laden: Top ~1000 Coins (CoinGecko) + neue Kleinst-Token (DexScreener), dazu
    Trend-Coins, Krypto-Nachrichten (RSS) und den Fear-&-Greed-Index.
 2. "Smart Money": erfolgreiche Trader auf Hyperliquid auswaehlen und ihre offenen
    Positionen beobachten (oeffentliche Daten). Aenderungen = Signale.
 3. Jeden Coin bewerten - fuer Long (steigende Kurse) und Short (fallende Kurse).
 4. Offene Papier-Positionen pruefen (Stopp, Trailing, Ziel, Zeit) und ggf. schliessen.
 5. Neue Papier-Positionen eroeffnen - mit Gebuehren und Kursabweichung (Slippage).
 6. Aus den abgeschlossenen Trades lernen: Gewichte der Signalarten anpassen.
 7. Beobachten & lernen: Auch nicht gehandelte Signale werden notiert und nach 4 h / 24 h / 72 h / 7 Tagen
    ausgewertet (Schattenportfolio). Daraus lernt der Bot, welche Signale, Tageszeiten (Handelssessions)
    und Haltedauern wirklich funktionieren.
 8. Alles in state.json speichern (wird vom Tracker angezeigt).

Nur Python-Standardbibliothek. Keine API-Schluessel noetig.
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ----------------------------------------------------------------- Einstellungen
START_EQUITY = 10_000.0          # virtuelles Startkapital in USD
MAX_OPEN = 12                    # maximal gleichzeitig offene Positionen
MAX_NEW_PER_RUN = 2              # maximal neue Positionen pro Lauf
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

SHORT_ENTRY_SCORE = 45.0         # Shorts (auf fallende Kurse setzen) brauchen etwas mehr Punkte
MAX_SHORTS = 4                   # maximal gleichzeitig offene Shorts
SHORT_SIZE_FACTOR = 0.7          # Shorts bekommen nur 70 % der normalen Groesse (Squeeze-Risiko)
SHORT_MIN_RANK = 300             # Shorts nur bei Coins bis Rang 300 ...
SHORT_MIN_VOL = 5_000_000        # ... und mindestens 5 Mio. $ Tagesvolumen (dort gibt es Futures)
SHORT_TARGET_PCT = 0.25          # Short-Ziel: Kurs -25 %
FUNDING_DAY = 0.0003             # angenommene Kosten fuer Shorts: 0,03 % pro Tag
SHORT_MAX_BTC24 = 4.0            # keine neuen Shorts, wenn Bitcoin gerade >4 % pumpt
NEWS_HOURS = 24

# Haltedauer-Profile: "kurz" fuer schnelle Ausbrueche, "swing" fuer stabile Trends (laenger halten, weiterer Stopp)
PROFILES = {
    "kurz": {"stop": 0.08, "trail_start": 0.10, "trail_gap": 0.08, "target": 0.30, "short_target": 0.25, "days": 10},
    "swing": {"stop": 0.14, "trail_start": 0.20, "trail_gap": 0.12, "target": 0.60, "short_target": 0.40, "days": 30},
}
MAX_STOP = 0.25                  # Stopp nie weiter als 25 %
RISK_PER_TRADE = 0.004           # pro Trade hoechstens 0,4 % des Kapitals riskieren (weiter Stopp -> kleinerer Einsatz)
OBS_PER_RUN = 2                  # so viele Signale pro Lauf zusaetzlich nur beobachten (Schattenportfolio)
OBS_MIN_SCORE = 30.0
OBS_REPEAT_H = 8                 # gleiches Signal erst nach 8 h erneut beobachten
OBS_MIN_LEARN = 25               # ab so vielen ausgewerteten Beobachtungen wird gelernt
HORIZONS = [("4h", 4), ("24h", 24), ("72h", 72), ("7d", 168)]

BASE_WEIGHTS = {"mom": 0.17, "brk": 0.10, "vol": 0.13, "rs": 0.09, "smart": 0.17, "small": 0.03,
                "trend": 0.06, "news": 0.05, "fund": 0.05, "pull": 0.15}
COMP_ORDER = list(BASE_WEIGHTS)
LABELS = {"mom": "Momentum", "brk": "Ausbruch", "vol": "Volumen-Spike",
          "rs": "Stärke vs. Bitcoin", "smart": "Smart Money", "small": "Kleiner Coin",
          "trend": "Trend-Coin", "news": "In den Nachrichten", "fund": "Funding (Gegenposition)",
          "pull": "Rücksetzer im Aufwärtstrend"}
SHORT_LABELS = dict(LABELS, mom="Abwärtstrend", brk="Abbruch nach unten", rs="Schwäche vs. Bitcoin",
                    fund="Funding (überfüllte Longs)", pull="Erholung im Abwärtstrend")
FEEDS = [("Cointelegraph", "https://cointelegraph.com/rss"), ("Decrypt", "https://decrypt.co/feed"),
         ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/")]
COMMON = {"core", "gas", "one", "near", "mina", "ark", "loop", "sun", "dash", "pi", "bone", "gold", "play", "link",
          "world", "alpha", "swap", "open", "nexus", "grass", "dog", "cat", "hot", "pepe_"}

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


def http_text(url, retries=2, timeout=30):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 krypto-radar-bot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(2 * (a + 1))
    return None


def parse_rss(xml_text, src):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = now_ts()
        if title:
            out.append({"title": title, "ts": ts, "src": src})
    return out


def news_buzz(headlines, coins, ts):
    """Wie oft wird ein Coin in den Schlagzeilen der letzten 24 h genannt? -> {id: Anzahl}"""
    recent = [h for h in headlines if ts - h["ts"] <= NEWS_HOURS * 3600]
    buzz = {}
    if not recent:
        return buzz
    for c in coins:
        if c["kind"] != "cg" or c["rank"] > 1000:
            continue
        pats = []
        if len(c["sym"]) >= 3:
            pats.append(r"\$" + re.escape(c["sym"]) + r"(?!\w)")
        nm = c["name"]
        if len(nm) >= 4 and nm.lower() not in COMMON:
            pats.append(r"(?<![\w$])" + re.escape(nm) + r"(?!\w)")
        if not pats:
            continue
        rx = re.compile("|".join(pats))
        n = sum(1 for h in recent if rx.search(h["title"]))
        if n:
            buzz[c["id"]] = n
    return buzz


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
                    "h24": num(r.get("high_24h")), "l24": num(r.get("low_24h")),
                    "img": r.get("image") or "", "kind": "cg"})
            time.sleep(2.5)
        return out

    def funding(self):
        """Hyperliquid: Funding-Rate und Open Interest je Coin (alle in einem Aufruf)."""
        d = http_json("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
        out = {}
        try:
            for u, ctx in zip(d[0]["universe"], d[1]):
                sym = hl_symbol(u["name"])
                if sym not in out:
                    out[sym] = {"f8": num(ctx.get("funding")) * 8, "oi": num(ctx.get("openInterest")) * num(ctx.get("markPx"))}
        except Exception:
            return {}
        return out

    def trending(self):
        d = http_json("https://api.coingecko.com/api/v3/search/trending")
        try:
            return [x["item"]["id"] for x in d["coins"]]
        except Exception:
            return []

    def fng(self):
        d = http_json("https://api.alternative.me/fng/?limit=1")
        try:
            x = d["data"][0]
            return {"value": int(x["value"]), "label": x.get("value_classification", "")}
        except Exception:
            return None

    def news(self):
        out = []
        for name, url in FEEDS:
            txt = http_text(url)
            if txt:
                out += parse_rss(txt, name)
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
            a = agg.setdefault(hl_symbol(coin), {"long": 0, "short": 0, "recent": 0, "recent_short": 0, "traders": []})
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
        if et >= cutoff and e["type"] in ("open", "add", "flip"):
            a = agg.setdefault(hl_symbol(e["coin"]), {"long": 0, "short": 0, "recent": 0, "recent_short": 0, "traders": []})
            a["recent" if e["side"] == "long" else "recent_short"] += 1
    out = {}
    for sym, a in agg.items():
        s = 22 * min(a["recent"], 3) + 11 * min(a["long"], 5) - 16 * a["short"]
        ss = 22 * min(a["recent_short"], 3) + 11 * min(a["short"], 5) - 16 * a["long"]
        out[sym] = {"score": clamp(s, 0, 100), "sscore": clamp(ss, 0, 100), "long": a["long"], "short": a["short"],
                    "recent": a["recent"], "recent_short": a["recent_short"]}
    return out


# ----------------------------------------------------------------- Bewertung
def slippage(c):
    if c["kind"] == "dex":
        return 0.02
    r = c["rank"]
    return 0.0005 if r <= 50 else 0.0015 if r <= 200 else 0.004 if r <= 500 else 0.008


def pos24(c):
    """Wo steht der Kurs in der 24-h-Spanne? 0 = Tagestief, 1 = Tageshoch."""
    h, l = c.get("h24") or 0.0, c.get("l24") or 0.0
    return clamp((c["price"] - l) / (h - l)) if h > l > 0 else None


def components(c, btc7, smart, side="long", trend=(), buzz=None, fund=None):
    """Signalstaerken 0..1 (Long = steigende Kurse, Short = fallende Kurse)."""
    short = side == "short"
    pp = pos24(c)
    if short:
        mom = 0.6 * clamp(-c["c7d"] / 35.0) + 0.4 * clamp(-c["c30d"] / 100.0)
        if c["c24h"] > 3:
            mom *= 0.3
        brk = 0.0
        if c["c1h"] < -0.3 and -35 <= c["c24h"] <= -2:
            d = -c["c24h"]
            brk = clamp((d - 2) / 8.0) * (1.0 if d <= 12 else clamp(1 - (d - 12) / 12.0)) * (1.0 if c["c1h"] < -0.8 else 0.6)
        pull = 0.0
        if pp is not None and c["c7d"] < -5 and c["c30d"] < -10 and -12 <= c["c24h"] <= 4:
            pull = clamp(1 - abs(pp - 0.55) / 0.35)
    else:
        mom = 0.6 * clamp(c["c7d"] / 35.0) + 0.4 * clamp(c["c30d"] / 100.0)
        if c["c24h"] < -3:
            mom *= 0.3
        brk = 0.0
        if c["c1h"] > 0.3 and 2 <= c["c24h"] <= 35:
            d = c["c24h"]   # Ausbruch zaehlt nur im "fruehen" Bereich - nach +12 % wird er ausgeblendet (nicht am Hoch kaufen)
            brk = clamp((d - 2) / 8.0) * (1.0 if d <= 12 else clamp(1 - (d - 12) / 12.0)) * (1.0 if c["c1h"] > 0.8 else 0.6)
        pull = 0.0
        if pp is not None and c["c7d"] > 5 and c["c30d"] > 10 and -4 <= c["c24h"] <= 12:
            pull = clamp(1 - abs(pp - 0.45) / 0.35)
    basis = c.get("liq") or c["mcap"] or 1.0
    ratio = c["vol"] / basis if basis else 0.0
    vol = clamp((ratio - 0.08) / 0.4)
    rs = 0.0
    if c["kind"] == "cg":
        rs = clamp(((btc7 - c["c7d"]) if short else (c["c7d"] - btc7)) / 25.0)
    sm = 0.0
    if c["kind"] == "cg" and c["rank"] <= 400:
        sm = smart.get(c["sym"], {}).get("sscore" if short else "score", 0) / 100.0
    fd = 0.0
    f8 = ((fund or {}).get(c["sym"]) or {}).get("f8")
    if f8 is not None and c["kind"] == "cg" and c["rank"] <= 600:
        crowd = clamp(f8 / 0.0004, -1.0, 1.0)      # >0: viele Longs zahlen Funding (ueberfuellt)
        fd = clamp(crowd if short else -crowd)
    small = trd = nws = 0.0
    if not short:
        if c["kind"] == "dex":
            small = 1.0
        elif c["rank"] > 100 and c["vol"] >= 1_000_000:
            small = clamp((c["rank"] - 100) / 600.0)
        trd = 1.0 if c["id"] in trend else 0.0
        nws = clamp((buzz or {}).get(c["id"], 0) / 3.0)
    return {"mom": mom, "brk": brk, "vol": vol, "rs": rs, "smart": sm, "small": small, "trend": trd, "news": nws,
            "fund": fd, "pull": pull}


def heat_factor(c, side):
    """Abschlag fuer Coins, die schon stark gelaufen sind (Long) bzw. abgestuerzt sind (Short):
    der Bot soll nicht oben kaufen."""
    d = c["c24h"] if side == "long" else -c["c24h"]
    f = 1.0 - 0.6 * clamp((d - 15) / 25.0)
    pp = pos24(c)
    if pp is not None and d > 8 and ((side == "long" and pp > 0.92) or (side == "short" and pp < 0.08)):
        f *= 0.85
    return f


def choose_profile(c, side):
    """Stabiler Trend bei grossen/mittleren Coins -> "swing" (laenger halten), sonst "kurz"."""
    if c["kind"] != "cg":
        return "kurz"
    if side == "long" and c["rank"] <= 500 and c["c7d"] >= 5 and c["c30d"] >= 15 and c["c24h"] <= 12:
        return "swing"
    if side == "short" and c["rank"] <= 300 and c["c7d"] <= -5 and c["c30d"] <= -15:
        return "swing"
    return "kurz"


def stop_for(c, profile):
    """Stopp-Abstand: mindestens der Profilwert, bei stark schwankenden Coins weiter (Tagesbewegung x 0,6)."""
    base = PROFILES[profile]["stop"]
    vol = max(abs(c["c24h"]), abs(c["c7d"]) / 3.0, 2.0)
    return clamp(0.6 * vol / 100.0, base, MAX_STOP)


def session_of(ts):
    """Handelssession (UTC): Asien 0-8, Europa 8-14, USA 14-21, spaet 21-24; Wochenende extra."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    if d.weekday() >= 5:
        return "weekend"
    h = d.hour
    return "asia" if h < 8 else "eu" if h < 14 else "us" if h < 21 else "late"


def total_score(comp, adj):
    tot = sum(BASE_WEIGHTS[k] * adj.get(k, 1.0) * v for k, v in comp.items())
    return 100.0 * tot / sum(BASE_WEIGHTS.values())


def tradable(c, side="long"):
    if side == "short":
        return (c["kind"] == "cg" and c["rank"] <= SHORT_MIN_RANK and c["vol"] >= SHORT_MIN_VOL
                and not is_stable_or_wrapped(c))
    if c["kind"] == "dex":
        return (c["liq"] >= DEX_MIN_LIQ and c["vol"] >= DEX_MIN_VOL and c.get("age_h", 0) >= 24
                and 0 <= c["c24h"] <= 80 and c["c1h"] > -2)
    return c["vol"] >= MIN_VOL_CG and not is_stable_or_wrapped(c)


# ----------------------------------------------------------------- Konto
def new_weights():
    return {"long": {k: 1.0 for k in BASE_WEIGHTS}, "short": {k: 1.0 for k in BASE_WEIGHTS}}


def new_state():
    return {"v": 2, "start_equity": START_EQUITY, "cash": START_EQUITY, "equity": START_EQUITY, "runs": 0,
            "created": iso(), "updated": iso(), "btc_start": None, "btc_now": None, "fees_paid": 0.0,
            "positions": [], "trades": [], "equity_curve": [], "weights": new_weights(),
            "traders": [], "traders_t": 0, "trader_pos": {}, "events": [], "signals": [], "market": {},
            "info": {}, "cooldown": {}, "log": [], "activity": [], "learn": {}, "sess_adj": {}, "prof_adj": {},
            "w_trades": new_weights(), "w_obs": new_weights(),
            "obs": {"open": [], "done": [], "seen": {}}}


def migrate(st):
    """Aeltere Staende (nur Long) auf das neue Format bringen."""
    w = st.get("weights") or {}
    if "long" not in w:
        w = {"long": dict(w), "short": {}}
    for side in ("long", "short"):
        w.setdefault(side, {})
        for k in BASE_WEIGHTS:
            w[side].setdefault(k, 1.0)
    st["weights"] = w
    st.setdefault("fees_paid", 0.0)
    st.setdefault("info", {})
    st.setdefault("activity", [])
    st.setdefault("learn", {})
    st.setdefault("sess_adj", {})
    st.setdefault("prof_adj", {})
    st.setdefault("obs", {"open": [], "done": [], "seen": {}})
    for k in ("open", "done"):
        st["obs"].setdefault(k, [])
    st["obs"].setdefault("seen", {})
    if "w_trades" not in st:
        st["w_trades"] = json.loads(json.dumps(st["weights"]))     # bisherige Gewichte stammen aus den Trades
    if "w_obs" not in st:
        st["w_obs"] = new_weights()
    for wk in ("w_trades", "w_obs"):
        for side in ("long", "short"):
            st[wk].setdefault(side, {})
            for k in BASE_WEIGHTS:
                st[wk][side].setdefault(k, 1.0)
    for p in st.get("positions", []):
        p.setdefault("profile", "kurz")
        p.setdefault("trail_start", PROFILES["kurz"]["trail_start"])
        p.setdefault("trail_gap", PROFILES["kurz"]["trail_gap"])
        p.setdefault("days", PROFILES["kurz"]["days"])
        p.setdefault("side", "long")
        p.setdefault("best", p.get("hw", p["entry"]))
        p.setdefault("worst", p["entry"])
        p.setdefault("funding", 0.0)
        p.setdefault("ts_fund", p.get("ts_in", now_ts()))
    for t in st.get("trades", []):
        t.setdefault("side", "long")
        t.setdefault("fees", 0.0)
        t.setdefault("profile", "kurz")
    st["v"] = 2
    return st


def direction(p):
    return -1.0 if p.get("side") == "short" else 1.0


def act(st, kind, text, ts):
    """Eintrag ins Aktivitaetsprotokoll (wird im Tracker angezeigt)."""
    st["activity"].append({"t": iso(ts), "k": kind, "x": text})
    st["activity"] = st["activity"][-300:]


def pos_value(p, price):
    """Aktueller Wert einer Position fuer das Kapital (Short: Sicherheit + Gewinn/Verlust - Finanzierung)."""
    if p.get("side") == "short":
        return p["size"] + p["qty"] * (p["entry"] - price) - p.get("funding", 0.0)
    return p["qty"] * price


def mark_equity(st, prices):
    eq = st["cash"]
    for p in st["positions"]:
        eq += pos_value(p, prices.get(p["id"], p.get("last", p["entry"])))
    return eq


def close_position(st, p, price, reason, ts):
    short = p.get("side") == "short"
    exit_px = price * (1 + p["slip"]) if short else price * (1 - p["slip"])
    fee_out = p["qty"] * exit_px * FEE
    if short:
        gross = p["qty"] * (p["entry"] - exit_px)
        st["cash"] += p["size"] + gross - fee_out - p["funding"]
    else:
        gross = p["qty"] * exit_px
        st["cash"] += gross - fee_out
        gross = gross - p["size"]
    cost = p["size"] + p["fee_in"]
    pnl = gross - fee_out - p["funding"] - p["fee_in"]
    st["fees_paid"] = st.get("fees_paid", 0.0) + p["fee_in"] + fee_out
    sign = direction(p)
    t = {"id": p["id"], "sym": p["sym"], "name": p["name"], "kind": p["kind"], "side": p.get("side", "long"),
         "t_in": p["t_in"], "t_out": iso(ts), "entry": p["entry"], "exit": exit_px, "size": round(p["size"], 2),
         "pnl": round(pnl, 2), "pnl_pct": round(100 * pnl / cost, 2) if cost else 0.0, "reason": reason,
         "score": p["score"], "comp": p["comp"], "why": p["why"], "days": round((ts - p["ts_in"]) / 86400, 2),
         "fees": round(p["fee_in"] + fee_out + p["funding"], 2), "profile": p.get("profile", "kurz"),
         "mfe": round(100 * (p["best"] / p["entry"] - 1) * sign, 2), "mae": round(100 * (p["worst"] / p["entry"] - 1) * sign, 2)}
    st["trades"].append(t)
    st["trades"] = st["trades"][-500:]
    st["cooldown"][p["id"]] = ts
    act(st, "close", "%s %s geschlossen (%s): %+.1f%% = %+.2f $ nach %.1f Tagen" % (
        p["sym"], "Short" if short else "Long", reason, t["pnl_pct"], t["pnl"], t["days"]), ts)
    return t


def check_exits(st, prices, ts):
    keep, closed = [], []
    for p in st["positions"]:
        short = p.get("side") == "short"
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
        if short:
            p["funding"] += p["size"] * FUNDING_DAY * max(ts - p.get("ts_fund", ts), 0) / 86400.0
            p["best"], p["worst"] = min(p["best"], px), max(p["worst"], px)
        else:
            p["best"], p["worst"] = max(p["best"], px), min(p["worst"], px)
        p["ts_fund"] = ts
        entry, best = p["entry"], p["best"]
        reason = None
        if short:
            gain = 1 - px / entry
            if px >= p["stop"]:
                reason = "Stopp"
            elif px <= p["target"]:
                reason = "Ziel erreicht"
            elif best <= entry * (1 - p["trail_start"]) and px >= best * (1 + p["trail_gap"]):
                reason = "Trailing-Stopp"
        else:
            gain = px / entry - 1
            if px <= p["stop"]:
                reason = "Stopp"
            elif px >= p["target"]:
                reason = "Ziel erreicht"
            elif best >= entry * (1 + p["trail_start"]) and px <= best * (1 - p["trail_gap"]):
                reason = "Trailing-Stopp"
        if not reason and (ts - p["ts_in"]) > p["days"] * 86400 and gain < TIME_STOP_MIN_GAIN:
            reason = "Zeit-Stopp"
        if reason:
            closed.append(close_position(st, p, px, reason, ts))
        else:
            keep.append(p)
    st["positions"] = keep
    return closed


def open_position(st, c, comp, score, size, ts, side="long", profile="kurz", stop_pct=None):
    short = side == "short"
    pf = PROFILES[profile]
    stop_pct = stop_pct or pf["stop"]
    slip = slippage(c)
    entry = c["price"] * (1 - slip) if short else c["price"] * (1 + slip)
    fee = size * FEE
    if size + fee > st["cash"]:
        return None
    qty = size / entry
    st["cash"] -= size + fee
    labels = SHORT_LABELS if short else LABELS
    why = [labels[k] for k, v in sorted(comp.items(), key=lambda kv: -BASE_WEIGHTS[kv[0]] * kv[1]) if v >= 0.4][:4]
    p = {"id": c["id"], "sym": c["sym"], "name": c["name"], "kind": c["kind"], "side": side, "img": c.get("img", ""),
         "profile": profile, "t_in": iso(ts), "ts_in": ts, "entry": entry, "qty": qty, "size": size, "fee_in": fee,
         "slip": slip, "stop_pct": round(stop_pct, 4),
         "stop": entry * (1 + stop_pct) if short else entry * (1 - stop_pct),
         "target": entry * (1 - pf["short_target"]) if short else entry * (1 + pf["target"]),
         "trail_start": pf["trail_start"], "trail_gap": pf["trail_gap"], "days": pf["days"],
         "best": entry, "worst": entry, "funding": 0.0, "ts_fund": ts, "last": c["price"],
         "score": round(score, 1), "comp": {k: round(v, 2) for k, v in comp.items()}, "why": why,
         "chain": c.get("chain"), "addr": c.get("addr"), "rank": c["rank"]}
    st["positions"].append(p)
    act(st, "open", "%s %s eröffnet (%s-Profil): Score %.0f, Einsatz %.0f $, Stopp %.0f%%, Gründe: %s" % (
        c["sym"], "Short" if short else "Long", profile, score, size, stop_pct * 100, ", ".join(why) or "–"), ts)
    return p


# ----------------------------------------------------------------- Lernen
def learn(st):
    """Gewichte aus den echten Papier-Trades: je Signalart und Seite (Long/Short) werden Trades, in denen die
    Signalart stark war (>= 0.5), mit dem Durchschnitt aller Trades derselben Seite verglichen.
    Begrenzt auf 0,5x - 1,5x und mit Vorsicht bei wenigen Trades. Dazu Groessenfaktor je Haltedauer-Profil."""
    for side in ("long", "short"):
        tr = [t for t in st["trades"] if t.get("side", "long") == side]
        w = st["w_trades"][side]
        if len(tr) < WEIGHT_MIN_TRADES:
            for k in BASE_WEIGHTS:
                w[k] = 1.0
            continue
        mean_all = sum(t["pnl_pct"] for t in tr) / len(tr)
        for k in BASE_WEIGHTS:
            hi = [t["pnl_pct"] for t in tr if t["comp"].get(k, 0) >= 0.5]
            if len(hi) < WEIGHT_MIN_TRADES:
                w[k] = 1.0
                continue
            diff = (sum(hi) / len(hi) - mean_all) / 100.0
            shrink = len(hi) / (len(hi) + 10.0)
            w[k] = round(clamp(1 + 0.5 * math.tanh(diff / 0.05) * shrink, 0.5, 1.5), 3)
    allp = [t["pnl_pct"] for t in st["trades"]]
    for pf in PROFILES:
        tp = [t["pnl_pct"] for t in st["trades"] if t.get("profile", "kurz") == pf]
        if len(tp) >= WEIGHT_MIN_TRADES and allp:
            diff = (sum(tp) / len(tp) - sum(allp) / len(allp)) / 100.0
            st["prof_adj"][pf] = round(clamp(1 + 0.4 * math.tanh(diff / 0.05) * len(tp) / (len(tp) + 10.0), 0.6, 1.4), 3)
        else:
            st["prof_adj"][pf] = 1.0
    apply_weights(st)


def apply_weights(st, ts=None):
    """Endgueltige Gewichte = Lernen aus Trades x Lernen aus den beobachteten Signalen (jeweils 0,5-1,5)."""
    for side in ("long", "short"):
        for k in BASE_WEIGHTS:
            old = st["weights"][side].get(k, 1.0)
            new = round(clamp(st["w_trades"][side][k] * st["w_obs"][side][k], 0.4, 1.6), 3)
            st["weights"][side][k] = new
            if ts is not None and abs(new - old) >= 0.05:
                act(st, "learn", "Gewicht „%s“ (%s): %.2f → %.2f" % (LABELS[k], "Short" if side == "short" else "Long", old, new), ts)


# ----------------------------------------------------------------- Beobachten (Schattenportfolio)
def observe(st, ranked, ts, btc_px, sess):
    """Beste Signale notieren, auch wenn nicht gehandelt wird - so entstehen viel mehr Lerndaten als aus
    den wenigen echten Trades."""
    seen = st["obs"]["seen"]
    n = 0
    for score, confirm, c, comp, side in ranked:
        if n >= OBS_PER_RUN or score < OBS_MIN_SCORE:
            break
        key = c["id"] + "|" + side
        if ts - seen.get(key, 0) < OBS_REPEAT_H * 3600:
            continue
        seen[key] = ts
        st["obs"]["open"].append({
            "i": c["id"], "y": c["sym"], "t": int(ts), "p": c["price"], "b": btc_px, "s": "S" if side == "short" else "L",
            "sc": round(score, 1), "c": [int(round(100 * comp[k])) for k in COMP_ORDER],
            "k": round(200 * FEE + 200 * slippage(c), 2), "se": sess, "wd": datetime.fromtimestamp(ts, timezone.utc).weekday(),
            "pf": choose_profile(c, side)[0], "r": {}})
        n += 1
    st["obs"]["seen"] = {k: v for k, v in seen.items() if ts - v < 7 * 86400}


def resolve_obs(st, prices, btc_px, ts):
    """Beobachtete Signale nach 4 h / 24 h / 72 h / 7 Tagen auswerten (Gewinn nach Kosten, Richtung beachtet)."""
    still = []
    for o in st["obs"]["open"]:
        age = (ts - o["t"]) / 3600.0
        px = prices.get(o["i"])
        sign = -1.0 if o["s"] == "S" else 1.0
        for lab, h in HORIZONS:
            if lab in o["r"] or age < h:
                continue
            if px and px > 0:
                raw = sign * (px / o["p"] - 1) * 100
                bret = sign * (btc_px / o["b"] - 1) * 100 if btc_px and o.get("b") else 0.0
                o["r"][lab] = [round(raw - o["k"], 2), round(raw - bret, 2)]
            elif age > h + 24:
                o["r"][lab] = None
        if age > 9 * 24 or all(lab in o["r"] for lab, _ in HORIZONS):
            st["obs"]["done"].append(o)
        else:
            still.append(o)
    st["obs"]["open"] = still
    st["obs"]["done"] = st["obs"]["done"][-2500:]


def _r(o, lab):
    v = o["r"].get(lab)
    return v[0] if v else None


def learn_obs(st):
    """Gewichte aus den beobachteten Signalen (Rendite nach 24 h) + Tageszeit-Faktor je Handelssession."""
    done = [o for o in st["obs"]["done"] if _r(o, "24h") is not None]
    wo = new_weights()
    for side, sd in (("long", "L"), ("short", "S")):
        sub = [o for o in done if o["s"] == sd]
        if len(sub) < OBS_MIN_LEARN:
            continue
        mean_all = sum(_r(o, "24h") for o in sub) / len(sub)
        for idx, k in enumerate(COMP_ORDER):
            hi = [_r(o, "24h") for o in sub if o["c"][idx] >= 50]
            if len(hi) < OBS_MIN_LEARN:
                continue
            diff = (sum(hi) / len(hi) - mean_all) / 100.0
            wo[side][k] = round(clamp(1 + 0.5 * math.tanh(diff / 0.02) * len(hi) / (len(hi) + 30.0), 0.5, 1.5), 3)
    st["w_obs"] = wo
    sess = {}
    if len(done) >= 3 * OBS_MIN_LEARN:
        mean_all = sum(_r(o, "24h") for o in done) / len(done)
        for se in ("asia", "eu", "us", "late", "weekend"):
            sub = [_r(o, "24h") for o in done if o["se"] == se]
            if len(sub) >= 40:
                diff = sum(sub) / len(sub) - mean_all           # in Prozentpunkten
                sess[se] = round(clamp(1 + 0.25 * math.tanh(diff / 2.0) * len(sub) / (len(sub) + 40.0), 0.8, 1.2), 3)
    st["sess_adj"] = sess


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def learn_stats(st):
    """Zusammenfassung fuer den Tracker: Wie gut sind die Signale wirklich? Wo lohnt langes Halten?"""
    done, opn = st["obs"]["done"], st["obs"]["open"]
    allo = done + opn
    top = [o for o in allo if o["sc"] >= ENTRY_SCORE + 5]
    out = {"n_done": len(done), "n_open": len(opn), "n_top": len(top),
           "since": iso(min(o["t"] for o in allo)) if allo else None}
    out["horizon"] = {lab: {"n": sum(1 for o in allo if _r(o, lab) is not None),
                            "all": _avg([_r(o, lab) for o in allo]), "top": _avg([_r(o, lab) for o in top]),
                            "hit_top": (round(100 * sum(1 for o in top if (_r(o, lab) or 0) > 0) /
                                              max(1, sum(1 for o in top if _r(o, lab) is not None)), 1)
                                        if any(_r(o, lab) is not None for o in top) else None)}
                      for lab, _ in HORIZONS}
    out["buckets"] = []
    for lo, hi in ((30, 40), (40, 50), (50, 60), (60, 101)):
        g = [o for o in allo if lo <= o["sc"] < hi]
        out["buckets"].append({"lo": lo, "hi": min(hi, 100), "n": len(g), "r24": _avg([_r(o, "24h") for o in g]),
                               "r72": _avg([_r(o, "72h") for o in g])})
    out["by_comp"] = {}
    for side, sd in (("long", "L"), ("short", "S")):
        sub = [o for o in allo if o["s"] == sd]
        base = _avg([_r(o, "24h") for o in sub])
        d = {}
        for idx, k in enumerate(COMP_ORDER):
            hi = [o for o in sub if o["c"][idx] >= 50]
            a24 = _avg([_r(o, "24h") for o in hi])
            if a24 is not None:
                d[k] = {"n": len(hi), "r24": a24, "edge": round(a24 - base, 2) if base is not None else None}
        out["by_comp"][side] = {"base": base, "comps": d}
    for key, field, vals in (("by_session", "se", ["asia", "eu", "us", "late", "weekend"]),
                             ("by_weekday", "wd", list(range(7))), ("by_profile", "pf", ["k", "s"])):
        out[key] = {str(v): {"n": len(g), "r24": _avg([_r(o, "24h") for o in g]), "r72": _avg([_r(o, "72h") for o in g]),
                             "r7d": _avg([_r(o, "7d") for o in g])}
                    for v in vals for g in [[o for o in allo if o[field] == v]] if g}
    days = {}
    for o in allo:
        if _r(o, "24h") is None:
            continue
        d = days.setdefault(datetime.fromtimestamp(o["t"], timezone.utc).strftime("%Y-%m-%d"), {"all": [], "top": []})
        d["all"].append(_r(o, "24h"))
        if o["sc"] >= ENTRY_SCORE + 5:
            d["top"].append(_r(o, "24h"))
    cum_all, cum_top, curve = [], [], []
    for day in sorted(days):
        cum_all += days[day]["all"]
        cum_top += days[day]["top"]
        curve.append({"d": day, "n": len(days[day]["all"]), "all": _avg(days[day]["all"]), "top": _avg(days[day]["top"]),
                      "cum_all": _avg(cum_all), "cum_top": _avg(cum_top)})
    out["curve"] = curve[-60:]
    return out


def group_stats(tr):
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    n = len(tr)
    return {"n": n, "wins": len(wins), "win_rate": round(100 * len(wins) / n, 1) if n else None,
            "pnl": round(sum(t["pnl"] for t in tr), 2),
            "avg_pct": round(sum(t["pnl_pct"] for t in tr) / n, 2) if n else None,
            "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
            "profit_factor": round(gw / gl, 2) if gl > 0 else None,
            "avg_days": round(sum(t["days"] for t in tr) / n, 2) if n else None}


def stats(st):
    tr = st["trades"]
    peak, mdd = 0.0, 0.0
    for _, eq, _b in st["equity_curve"]:
        peak = max(peak, eq)
        if peak:
            mdd = max(mdd, (peak - eq) / peak)
    out = group_stats(tr)
    out["max_drawdown"] = round(100 * mdd, 2)
    out["fees_paid"] = round(st.get("fees_paid", 0.0), 2)
    out["best"] = max(tr, key=lambda t: t["pnl"])["sym"] + " %+.1f%%" % max(t["pnl_pct"] for t in tr) if tr else None
    out["worst"] = min(tr, key=lambda t: t["pnl"])["sym"] + " %+.1f%%" % min(t["pnl_pct"] for t in tr) if tr else None
    out["by_side"] = {sd: group_stats([t for t in tr if t.get("side", "long") == sd]) for sd in ("long", "short")}
    out["by_profile"] = {pf: group_stats([t for t in tr if t.get("profile", "kurz") == pf]) for pf in PROFILES}
    out["by_reason"] = {r: group_stats([t for t in tr if t["reason"] == r]) for r in sorted({t["reason"] for t in tr})}
    out["by_comp"] = {}
    for sd in ("long", "short"):
        d = {}
        for k in BASE_WEIGHTS:
            hi = [t for t in tr if t.get("side", "long") == sd and t["comp"].get(k, 0) >= 0.5]
            if hi:
                g = group_stats(hi)
                d[k] = {"n": g["n"], "win_rate": g["win_rate"], "pnl": g["pnl"], "avg_pct": g["avg_pct"]}
        out["by_comp"][sd] = d
    return out


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
    migrate(st)
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

    # Zusatzinfos aus dem Internet: Trend-Coins, Nachrichten, Fear & Greed (jede Quelle einzeln abgesichert)
    trend, headlines, fng, fund = [], [], None, {}
    for name, fn in (("Trend", lambda: src.trending()), ("Nachrichten", lambda: src.news()), ("Fear&Greed", lambda: src.fng()),
                     ("Funding", lambda: src.funding())):
        try:
            v = fn()
            if name == "Trend":
                trend = v or []
            elif name == "Nachrichten":
                headlines = v or []
            elif name == "Funding":
                fund = v or {}
            else:
                fng = v
        except Exception as e:
            log("%s Fehler: %r" % (name, e))
    buzz = news_buzz(headlines, cg, ts)

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
    resolve_obs(st, prices, btc_px, ts)
    learn_obs(st)
    if closed:
        learn(st)
    else:
        apply_weights(st, ts)

    regime_ok = not (btc24 < -6 or btc7 < -15)
    shorts_ok = btc24 < SHORT_MAX_BTC24
    prev_regime = st.get("market", {}).get("regime_ok")
    if prev_regime is not None and prev_regime != regime_ok:
        act(st, "market", "Marktfilter %s (Bitcoin 24 h %+.1f%%, 7 Tage %+.1f%%)" % (
            "aufgehoben – neue Longs wieder erlaubt" if regime_ok else "aktiv – keine neuen Longs", btc24, btc7), ts)
    fng_v = (fng or {}).get("value")
    sess = session_of(ts)
    smult = st["sess_adj"].get(sess, 1.0)
    ranked = []
    for c in universe.values():
        for side in ("long", "short"):
            if side == "short" and not shorts_ok:
                continue
            if not tradable(c, side):
                continue
            comp = components(c, btc7, smart, side, trend, buzz, fund)
            score = total_score(comp, st["weights"][side]) * heat_factor(c, side) * smult
            ranked.append((score, sum(1 for v in comp.values() if v >= 0.4), c, comp, side))
    ranked.sort(key=lambda x: -x[0])
    observe(st, ranked, ts, btc_px, sess)
    st["signals"] = [{"id": c["id"], "sym": c["sym"], "name": c["name"], "kind": c["kind"], "rank": c["rank"],
                      "side": side, "price": c["price"], "c1h": round(c["c1h"], 2), "c24h": round(c["c24h"], 2),
                      "c7d": round(c["c7d"], 2), "vol": round(c["vol"]), "mcap": round(c["mcap"]),
                      "score": round(s, 1), "confirm": cf, "comp": {k: round(v, 2) for k, v in comp.items()},
                      "img": c.get("img", "")} for s, cf, c, comp, side in ranked[:40]]

    opened = []
    equity = mark_equity(st, prices)
    invested = sum(p["size"] for p in st["positions"])
    held = {p["id"] for p in st["positions"]}
    n_short = sum(1 for p in st["positions"] if p.get("side") == "short")
    for score, confirm, c, comp, side in ranked:
        if len(opened) >= MAX_NEW_PER_RUN or len(st["positions"]) >= MAX_OPEN:
            break
        short = side == "short"
        if score < (SHORT_ENTRY_SCORE if short else ENTRY_SCORE):
            if score < ENTRY_SCORE:
                break
            continue
        if confirm < MIN_CONFIRM or c["id"] in held:
            continue
        if not short and not regime_ok:
            continue
        if short and n_short >= MAX_SHORTS:
            continue
        if ts - st["cooldown"].get(c["id"], 0) < COOLDOWN_DAYS * 86400:
            continue
        profile = choose_profile(c, side)
        stop_pct = stop_for(c, profile)
        frac = 0.03 if score >= 60 else 0.02 if score >= 50 else 0.01
        size = equity * frac * st["prof_adj"].get(profile, 1.0) * (DEX_SIZE_FACTOR if c["kind"] == "dex" else 1.0) * (SHORT_SIZE_FACTOR if short else 1.0)
        if fng_v is not None and ((fng_v >= 85 and not short) or (fng_v <= 15 and short)):
            size *= 0.7                      # extreme Stimmung in Kaufrichtung -> kleiner einsteigen
        size = min(size, 0.005 * max(c.get("liq") or c["vol"], 0), equity * RISK_PER_TRADE / stop_pct)
        if size < 25 or invested + size > equity * MAX_EXPOSURE:
            continue
        p = open_position(st, c, comp, score, size, ts, side, profile, stop_pct)
        if p:
            opened.append(p)
            invested += size
            held.add(c["id"])
            n_short += 1 if short else 0

    equity = mark_equity(st, prices)
    st["equity"] = round(equity, 2)
    btc_val = round(st["start_equity"] * btc_px / st["btc_start"], 2) if btc_px and st["btc_start"] else None
    st["equity_curve"].append([iso(ts), round(equity, 2), btc_val])
    if len(st["equity_curve"]) > 5000:       # aeltere Punkte ausduennen, damit die Datei klein bleibt
        st["equity_curve"] = st["equity_curve"][:2500:2] + st["equity_curve"][2500:]
    st["market"] = {"coins": len(cg), "dex": len(dex), "btc24h": round(btc24, 2), "btc7d": round(btc7, 2),
                    "regime_ok": regime_ok, "shorts_ok": shorts_ok, "traders": len(st["traders"]),
                    "headlines": len(headlines), "trending": len(trend)}
    byid = {c["id"]: c for c in cg}
    st["info"] = {"fng": fng,
                  "trending": [{"id": i, "sym": byid[i]["sym"], "name": byid[i]["name"], "c24h": round(byid[i]["c24h"], 2)}
                               for i in trend if i in byid][:10],
                  "headlines": [{"title": h["title"], "src": h["src"], "t": iso(h["ts"])}
                                for h in sorted(headlines, key=lambda h: -h["ts"])[:20]],
                  "buzz": sorted(({"sym": byid[i]["sym"], "name": byid[i]["name"], "n": n} for i, n in buzz.items() if i in byid),
                                 key=lambda x: -x["n"])[:10]}
    if fund:
        fl = sorted(({"sym": k, "f8": round(v["f8"] * 100, 4), "oi": round(v["oi"])} for k, v in fund.items() if v["oi"] > 5e6),
                    key=lambda x: x["f8"])
        st["info"]["funding"] = {"low": fl[:6], "high": fl[-6:][::-1]}
    st["market"]["session"] = sess
    st["market"]["session_mult"] = smult
    st["learn"] = learn_stats(st)
    st["stats"] = stats(st)
    st["updated"] = iso(ts)
    line = "%s: Kapital %.2f, offen %d, neu %d, geschlossen %d" % (iso(ts), equity, len(st["positions"]), len(opened), len(closed))
    st["log"] = (st["log"] + [line])[-40:]
    log(line)
    return opened, closed


def obs_path(path):
    return os.path.join(os.path.dirname(os.path.abspath(path)), "obs.json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        base = new_state()
        base.update(st)
    except FileNotFoundError:
        base = new_state()
    try:
        with open(obs_path(path), encoding="utf-8") as f:
            base["obs"] = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    return migrate(base)


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_state(st, path):
    """state.json (fuer den Tracker) und obs.json (Rohdaten der Beobachtungen, nur fuer den Bot) getrennt speichern."""
    _write(obs_path(path), st.get("obs", {"open": [], "done": [], "seen": {}}))
    _write(path, {k: v for k, v in st.items() if k != "obs"})


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
