# Krypto-Radar Paper-Bot

Simulierter Handel mit echten Kursen – **kein echtes Geld, keine Börsenverbindung**.

**Ablauf (alle 15 Minuten, GitHub Actions):** Markt laden (Top ~1000 Coins + neue DEX-Token, Trend-Coins,
Nachrichten, Fear & Greed) → erfolgreiche Hyperliquid-Trader beobachten → Coins bewerten (Long und Short) →
virtuelle Käufe/Verkäufe mit Gebühren und Kursabweichung → aus Ergebnissen lernen → `state.json` auf den Branch `data` schreiben.
Der Reiter **Paper-Bot** im Tracker liest diese Datei.

**Regeln (siehe Konstanten oben in `radar_bot.py`):** Start 10.000 $, max. 12 Positionen, 1–3 % je Einstieg
(DEX-Token ein Drittel davon), Stopp −8 %, Trailing ab +10 %, Ziel +30 % (Short: −25 %, max. 4 Shorts, 0,03 % Finanzierung pro Tag), Zeit-Stopp nach 10 Tagen,
kein Einstieg bei Bitcoin-Crash (24 h < −6 % oder 7 Tage < −15 %).

**Lernen:** Sobald eine Signalart in mindestens 8 Trades stark war, wird ihr Gewicht (0,5×–1,5×)
nach dem Erfolg dieser Trades angepasst.

**Einrichten:** Repository ▸ Actions ▸ „Paper-Trading-Bot“ ▸ Run workflow (läuft danach automatisch).
Tests lokal: `python3 bot/test_bot.py`.
