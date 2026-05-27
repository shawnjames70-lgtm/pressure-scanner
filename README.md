---
title: 3-Step Pressure Method Scanner
emoji: 📈
colorFrom: green
colorTo: red
sdk: docker
pinned: true
license: mit
app_port: 7860
---

# 3-Step Pressure Method Scanner

A live daytrading/scalping scanner powered by the Tastytrade Open API and dxFeed WebSocket.

## Strategy

**Step 1 — Volume Surge:** Current 5-min candle volume must exceed the 20-period rolling Volume SMA.

**Step 2 — Shaved Candle:** Close in the top 10% of range (buy) or bottom 10% (sell).

**Step 3 — NYSE TICK:** $TICK above +800 (buy) or below -800 (sell).

## Signals

- 🟢 **ALL-GREEN GO LONG** — All three buy conditions align
- 🔴 **ALL-RED GO SHORT** — All three sell conditions align

## Deployment

Deployed on Railway. Requires a valid Tastytrade account.

## Disclaimer

Not financial advice. For educational purposes only.
