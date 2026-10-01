import unittest, copy
import radar_bot as rb

def coin(i, price=1.0, rank=None, c1h=1.0, c24h=8.0, c7d=25.0, c30d=60.0, vol=5e6, mcap=5e7, id=None):
    return {"id": id or "coin%d" % i, "sym": "C%d" % i, "name": "Coin %d" % i, "price": price, "mcap": mcap, "vol": vol,
            "rank": rank or (100 + i), "c1h": c1h, "c24h": c24h, "c7d": c7d, "c30d": c30d, "img": "", "kind": "cg"}

def market(overrides=None, n=300):
    cs = [coin(i, c1h=0.0, c24h=0.5, c7d=1.0, c30d=2.0, vol=2e6, mcap=1e9, rank=i + 3) for i in range(n)]
    cs[0].update(id="bitcoin", sym="BTC", name="Bitcoin", price=60000.0, rank=1, c7d=1.0, c24h=0.5)
    for k, v in (overrides or {}).items():
        cs[k].update(v)
    return cs

class Fake:
    def __init__(self, mk, lb=None, pos=None):
        self.mk, self.lb, self.pos = mk, lb, pos or {}
    def markets(self, pages=4): return copy.deepcopy(self.mk)
    def leaderboard(self): return self.lb
    def positions(self, a): return self.pos.get(a)
    def dex_candidates(self): return []
    def dex_prices(self, r): return {}

T0 = 1_700_000_000

class Engine(unittest.TestCase):
    def test_entry_fees_slippage_and_stop(self):
        st = rb.new_state()
        hot = {5: dict(price=2.0, c1h=1.5, c24h=12.0, c7d=35.0, c30d=100.0, vol=4e8, mcap=1e9)}
        rb.run(st, Fake(market(hot)), ts=T0)
        self.assertEqual(len(st["positions"]), 1)
        p = st["positions"][0]
        self.assertGreater(p["entry"], 2.0)                       # Slippage drauf
        self.assertAlmostEqual(st["cash"], rb.START_EQUITY - p["size"] - p["fee_in"], 4)
        # Kurs faellt unter Stopp -> geschlossen mit Verlust
        crash = {5: dict(price=1.5, c1h=-3, c24h=-20, c7d=0, c30d=0)}
        rb.run(st, Fake(market(crash)), ts=T0 + 1800)
        self.assertEqual(len(st["positions"]), 0)
        t = st["trades"][0]
        self.assertEqual(t["reason"], "Stopp")
        self.assertLess(t["pnl"], 0)
        self.assertAlmostEqual(st["equity"], st["cash"], 2)
        # Cooldown: kein sofortiger Wiedereinstieg
        rb.run(st, Fake(market(hot)), ts=T0 + 3600)
        self.assertEqual(len(st["positions"]), 0)

    def test_target_and_trailing(self):
        st = rb.new_state()
        hot = {5: dict(price=2.0, c1h=1.5, c24h=12.0, c7d=35.0, c30d=100.0, vol=4e8, mcap=1e9)}
        rb.run(st, Fake(market(hot)), ts=T0)
        e = st["positions"][0]["entry"]
        rb.run(st, Fake(market({5: dict(price=e * 1.2, c24h=0, c1h=0, c7d=0, c30d=0)})), ts=T0 + 1800)   # +20 %
        self.assertEqual(len(st["positions"]), 1)
        rb.run(st, Fake(market({5: dict(price=e * 1.2 * 0.9, c24h=0, c1h=0, c7d=0, c30d=0)})), ts=T0 + 3600)  # -10 % vom Hoch
        self.assertEqual(st["trades"][0]["reason"], "Trailing-Stopp")
        self.assertGreater(st["trades"][0]["pnl"], 0)

    def test_market_filter_and_min_volume(self):
        st = rb.new_state()
        hot = {5: dict(price=2.0, c1h=1.5, c24h=12.0, c7d=35.0, c30d=100.0, vol=4e8, mcap=1e9),
               6: dict(price=2.0, c1h=1.5, c24h=12.0, c7d=35.0, c30d=100.0, vol=1e5, mcap=1e6)}
        bad = {0: dict(c24h=-8.0)}
        bad.update(hot)
        rb.run(st, Fake(market(bad)), ts=T0)
        self.assertEqual(len(st["positions"]), 0)                 # Marktfilter
        rb.run(st, Fake(market(hot)), ts=T0 + 1800)
        ids = [p["id"] for p in st["positions"]]
        self.assertNotIn("coin6", ids)                            # zu wenig Volumen

    def test_stablecoins_excluded(self):
        c = coin(1, price=1.0, c24h=0.1, c7d=0.1)
        c["sym"] = "USDX"
        self.assertTrue(rb.is_stable_or_wrapped(c))
        w = coin(2); w["name"] = "Wrapped Ether"
        self.assertTrue(rb.is_stable_or_wrapped(w))

    def test_abort_on_bad_data(self):
        st = rb.new_state()
        with self.assertRaises(RuntimeError):
            rb.run(st, Fake([coin(1)]), ts=T0)

    def test_learning_bounds(self):
        st = rb.new_state()
        for i in range(30):
            good = i % 2 == 0
            st["trades"].append({"pnl_pct": 12.0 if good else -8.0, "pnl": 1 if good else -1,
                                 "comp": {"mom": 0.9 if good else 0.0, "brk": 0.0 if good else 0.9, "vol": 0, "rs": 0, "smart": 0, "small": 0}})
        rb.learn(st)
        self.assertGreater(st["weights"]["mom"], 1.0)
        self.assertLess(st["weights"]["brk"], 1.0)
        for v in st["weights"].values():
            self.assertTrue(0.5 <= v <= 1.5)

class Traders(unittest.TestCase):
    def row(self, addr, av=500000, wp=1e4, mp=5e4, ap=3e5, wr=.05, mr=.3, ar=1.0, vlm=1e6):
        return {"ethAddress": addr, "accountValue": str(av), "displayName": None, "windowPerformances": [
            ["day", {"pnl": "1", "roi": "0.0", "vlm": "1"}],
            ["week", {"pnl": str(wp), "roi": str(wr), "vlm": "1"}],
            ["month", {"pnl": str(mp), "roi": str(mr), "vlm": str(vlm)}],
            ["allTime", {"pnl": str(ap), "roi": str(ar), "vlm": "1"}]]}

    def test_selection_filters(self):
        raw = {"leaderboardRows": [self.row("0xgood"), self.row("0xloser", mp=-5), self.row("0xsmall", av=1000),
                                   self.row("0xmm", vlm=5e9), self.row("0xlucky", ar=0.05)]}
        sel = rb.select_traders(rb.parse_leaderboard(raw))
        self.assertEqual([t["addr"] for t in sel], ["0xgood"])

    def test_diff_and_smart_score(self):
        prev = {"BTC": {"szi": 1.0, "value": 100000, "entry": 1, "upnl": 0, "lev": 5}}
        new = {"BTC": {"szi": 2.0, "value": 200000, "entry": 1, "upnl": 0, "lev": 5},
               "kPEPE": {"szi": 1e6, "value": 80000, "entry": 1, "upnl": 0, "lev": 3}}
        ev = rb.diff_positions("0xa", "A", prev, new, T0)
        types = {(e["coin"], e["type"]) for e in ev}
        self.assertEqual(types, {("BTC", "add"), ("kPEPE", "open")})
        sc = rb.smart_scores({"0xa": new, "0xb": new}, ev, T0 + 60)
        self.assertEqual(rb.hl_symbol("kPEPE"), "PEPE")
        self.assertGreater(sc["PEPE"]["score"], 20)
        self.assertEqual(rb.parse_positions({"assetPositions": [{"position": {"coin": "ETH", "szi": "-3", "positionValue": "9000", "entryPx": "1", "unrealizedPnl": "5", "leverage": {"value": 4}}}]})["ETH"]["szi"], -3.0)

    def test_full_run_with_smart_money(self):
        lb = {"leaderboardRows": [self.row("0xgood")]}
        pos = {"0xgood": {"assetPositions": [{"position": {"coin": "C5", "szi": "100", "positionValue": "90000", "entryPx": "1", "unrealizedPnl": "0", "leverage": {"value": 3}}}]}}
        st = rb.new_state()
        warm = {5: dict(c1h=0.5, c24h=4.0, c7d=10.0, c30d=20.0, vol=1e8, mcap=5e8, price=2.0)}
        rb.run(st, Fake(market(warm), lb, pos), ts=T0)
        self.assertEqual(len(st["traders"]), 1)
        s5 = [s for s in st["signals"] if s["id"] == "coin5"]
        self.assertTrue(s5 and s5[0]["comp"]["smart"] > 0)

    def test_dex_parse(self):
        pairs = [{"chainId": "solana", "baseToken": {"address": "A", "symbol": "xx", "name": "XX"}, "priceUsd": "0.01",
                  "liquidity": {"usd": 90000}, "volume": {"h24": 300000}, "priceChange": {"h1": 2, "h24": 20},
                  "pairCreatedAt": (T0 - 5 * 86400) * 1000, "fdv": 1e6},
                 {"chainId": "solana", "baseToken": {"address": "A", "symbol": "xx", "name": "XX"}, "priceUsd": "0.01",
                  "liquidity": {"usd": 1000}, "volume": {"h24": 10}, "priceChange": {}, "pairCreatedAt": 0}]
        out = rb.parse_dex_pairs(pairs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "dex:solana:A")
        self.assertEqual(out[0]["liq"], 90000)

if __name__ == "__main__":
    unittest.main(verbosity=1)
