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
    def __init__(self, mk, lb=None, pos=None, trend=None, news=None, fng=None):
        self.mk, self.lb, self.pos = mk, lb, pos or {}
        self._trend, self._news, self._fng = trend or [], news or [], fng
    def trending(self): return self._trend
    def news(self): return self._news
    def fng(self): return self._fng
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
        self.assertGreater(st["weights"]["long"]["mom"], 1.0)
        self.assertLess(st["weights"]["long"]["brk"], 1.0)
        self.assertEqual(st["weights"]["short"]["mom"], 1.0)      # keine Short-Trades -> unveraendert
        for side in st["weights"].values():
            for v in side.values():
                self.assertTrue(0.5 <= v <= 1.5)

DROP = {5: dict(price=2.0, c1h=-1.5, c24h=-12.0, c7d=-35.0, c30d=-60.0, vol=4e8, mcap=1e9)}

class Shorts(unittest.TestCase):
    def test_short_profit_with_costs(self):
        st = rb.new_state()
        rb.run(st, Fake(market(DROP)), ts=T0)
        shorts = [p for p in st["positions"] if p["side"] == "short"]
        self.assertEqual(len(shorts), 1)
        p = shorts[0]
        self.assertLess(p["entry"], 2.0)                       # Verkauf mit Slippage etwas tiefer
        self.assertGreater(p["stop"], p["entry"])
        self.assertLess(p["target"], p["entry"])
        self.assertAlmostEqual(st["cash"], rb.START_EQUITY - p["size"] - p["fee_in"], 4)
        # Kurs faellt weiter auf unter das Ziel -> Gewinn
        rb.run(st, Fake(market({5: dict(price=p["entry"] * 0.70, c1h=0, c24h=0, c7d=0, c30d=0)})), ts=T0 + 86400)
        t = st["trades"][0]
        self.assertEqual((t["side"], t["reason"]), ("short", "Ziel erreicht"))
        self.assertGreater(t["pnl"], 0)
        self.assertGreater(t["pnl"], 0.2 * t["size"])          # ~ +30 % minus Kosten
        self.assertLess(t["pnl"], 0.31 * t["size"])
        self.assertAlmostEqual(st["equity"], st["cash"], 2)
        self.assertGreater(t["fees"], 0)                       # inkl. Finanzierung

    def test_short_stop_loss(self):
        st = rb.new_state()
        rb.run(st, Fake(market(DROP)), ts=T0)
        p = [x for x in st["positions"] if x["side"] == "short"][0]
        rb.run(st, Fake(market({5: dict(price=p["entry"] * 1.12, c1h=0, c24h=0, c7d=0, c30d=0)})), ts=T0 + 1800)
        t = st["trades"][0]
        self.assertEqual((t["side"], t["reason"]), ("short", "Stopp"))
        self.assertLess(t["pnl"], 0)
        self.assertAlmostEqual(st["equity"], st["cash"], 2)

    def test_equity_marks_short_unrealized(self):
        st = rb.new_state()
        rb.run(st, Fake(market(DROP)), ts=T0)
        p = [x for x in st["positions"] if x["side"] == "short"][0]
        rb.run(st, Fake(market({5: dict(price=p["entry"] * 0.9, c1h=0, c24h=0, c7d=0, c30d=0)})), ts=T0 + 1800)
        self.assertEqual(len(st["positions"]), 1)
        self.assertGreater(st["equity"], 10000 - 10)           # Short im Plus (+10 %) deckt Gebuehren

    def test_no_shorts_when_btc_pumps_or_small_coin(self):
        st = rb.new_state()
        o = dict(DROP)
        o[0] = dict(c24h=6.0)                                  # Bitcoin pumpt
        rb.run(st, Fake(market(o)), ts=T0)
        self.assertEqual([p for p in st["positions"] if p["side"] == "short"], [])
        st = rb.new_state()
        small = {250: dict(price=2.0, c1h=-1.5, c24h=-12.0, c7d=-35.0, c30d=-60.0, vol=4e8, mcap=1e9, rank=700)}
        rb.run(st, Fake(market(small)), ts=T0)
        self.assertEqual([p for p in st["positions"] if p["side"] == "short"], [])

    def test_short_learning_separate(self):
        st = rb.new_state()
        for i in range(20):
            st["trades"].append({"side": "short", "pnl_pct": 10.0 if i % 2 == 0 else -9.0, "pnl": 1,
                                 "comp": {"mom": 0.9 if i % 2 == 0 else 0.0, "brk": 0.0 if i % 2 == 0 else 0.9}})
        rb.learn(st)
        self.assertGreater(st["weights"]["short"]["mom"], 1.0)
        self.assertEqual(st["weights"]["long"]["mom"], 1.0)

class Extras(unittest.TestCase):
    def test_news_buzz_and_trending_raise_score(self):
        cs = market({5: dict(name="Zorbix", sym="ZRB", price=2.0, c1h=0.5, c24h=4.0, c7d=10.0, c30d=20.0, vol=1e8, mcap=5e8)})
        heads = [{"title": "Zorbix surges as $ZRB listed on big exchange", "ts": T0 - 3600, "src": "x"},
                 {"title": "Zorbix partners with bank", "ts": T0 - 7200, "src": "x"},
                 {"title": "Zorbix old news", "ts": T0 - 5 * 86400, "src": "x"}]
        buzz = rb.news_buzz(heads, cs, T0)
        self.assertEqual(buzz.get("coin5"), 2)                 # alte Schlagzeile zaehlt nicht
        a = rb.components(cs[5], 1.0, {}, "long", (), {})
        b = rb.components(cs[5], 1.0, {}, "long", ("coin5",), buzz)
        self.assertEqual((a["trend"], a["news"]), (0.0, 0.0))
        self.assertEqual(b["trend"], 1.0)
        self.assertAlmostEqual(b["news"], 2 / 3, 3)
        self.assertGreater(rb.total_score(b, {}), rb.total_score(a, {}))
        st = rb.new_state()
        rb.run(st, Fake(cs, trend=["coin5"], news=heads, fng={"value": 40, "label": "Fear"}), ts=T0)
        self.assertEqual(st["info"]["fng"]["value"], 40)
        self.assertEqual(st["info"]["trending"][0]["sym"], "ZRB")
        self.assertTrue(st["info"]["headlines"])

    def test_rss_parse(self):
        xml = "<rss><channel><item><title>Hallo Welt</title><pubDate>Wed, 01 Oct 2026 12:00:00 +0000</pubDate></item><item><title>Zwei</title></item></channel></rss>"
        out = rb.parse_rss(xml, "T")
        self.assertEqual([o["title"] for o in out], ["Hallo Welt", "Zwei"])
        self.assertEqual(rb.parse_rss("kaputt", "T"), [])

    def test_failing_extras_do_not_stop_run(self):
        class Boom(Fake):
            def trending(self): raise RuntimeError("x")
            def news(self): raise RuntimeError("x")
            def fng(self): raise RuntimeError("x")
        st = rb.new_state()
        rb.run(st, Boom(market()), ts=T0)
        self.assertEqual(st["runs"], 1)

    def test_migrate_old_state(self):
        old = {"v": 1, "cash": 9000.0, "weights": {"mom": 1.2, "brk": 0.9}, "start_equity": 10000.0,
               "positions": [{"id": "x", "sym": "X", "name": "X", "kind": "cg", "entry": 1.0, "qty": 1000.0, "size": 1000.0,
                              "fee_in": 1.0, "slip": 0.001, "stop": .92, "target": 1.3, "hw": 1.1, "last": 1.1,
                              "t_in": "2026-10-01T00:00:00Z", "ts_in": T0, "score": 50, "comp": {}, "why": []}],
               "trades": [{"id": "y", "pnl": 5, "pnl_pct": 1.0, "reason": "Ziel erreicht", "comp": {"mom": .9}, "days": 1, "sym": "Y"}]}
        base = rb.new_state()
        base.update(old)                                       # wie load_state()
        st = rb.migrate(base)
        self.assertEqual(st["weights"]["long"]["mom"], 1.2)
        self.assertEqual(st["weights"]["short"]["mom"], 1.0)
        self.assertEqual(st["positions"][0]["side"], "long")
        self.assertEqual(st["positions"][0]["best"], 1.1)
        self.assertEqual(st["trades"][0]["side"], "long")
        rb.run(st, Fake(market()), ts=T0 + 1800)               # laeuft mit altem Stand durch
        self.assertIn("by_side", st["stats"])

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
