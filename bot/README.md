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

## Neu: Lernen, Haltedauer, Sessions
- **Beobachten statt nur handeln:** Pro Lauf werden die 2 besten Signale zusätzlich notiert (auch wenn nicht gekauft wird)
  und nach 4 h / 24 h / 72 h / 7 Tagen mit dem echten Kurs verglichen (nach Kosten, Richtung beachtet).
  Rohdaten: `obs.json` (nur für den Bot), Zusammenfassung für den Tracker: `state.json` → `learn`.
- **Gewichte** = Lernen aus echten Trades × Lernen aus Beobachtungen (ab 25 ausgewerteten Beobachtungen), je Long/Short.
- **Handelssessions:** Asien 0–8, Europa 8–14, USA 14–21, spät 21–24 Uhr UTC, Wochenende. Ab 40 Beobachtungen pro Session
  verschiebt ein Faktor (0,8–1,2) die Punktzahl.
- **Haltedauer-Profile:** „kurz“ (Stopp 8–25 % je nach Schwankung, bis 10 Tage) und „swing“ (Stopp ab 14 %, Trailing ab +20 %,
  Ziel +60 %, bis 30 Tage) für stabile Trends bei größeren Coins. Einsatz höchstens 0,4 % Kapitalrisiko pro Trade.
- **Nicht oben kaufen:** Ausbruch zählt nur bis +12 % am Tag, danach Abschlag; neue Signale „Rücksetzer im Trend“ (Kurs in der
  24-h-Spanne) und „Funding“ (Hyperliquid, Gegenpositionierung).
- **Aktivitätsprotokoll:** Käufe, Verkäufe, Marktfilter und Lernschritte in `state.json` → `activity`.
