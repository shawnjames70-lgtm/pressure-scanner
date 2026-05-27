---
title: 3-Step Pressure Method Scanner
emoji: 📡
colorFrom: blue
colorTo: yellow
sdk: docker
pinned: true
license: mit
app_port: 7860
---

# 📡 3-Step Pressure Method Scanner

A live real-time daytrading/scalping scanner powered by the Tastytrade Open API and dxFeed DXLink WebSocket.

## Strategy

**Step 1 — Volume Surge:** Current candle volume exceeds the 20-period rolling Volume SMA (RVOL > 1.0×).

**Step 2 — Shaved Candle:** Close in the top 10% of the candle range (buy) or bottom 10% (sell).

**Step 3 — VIX Confirmation:** VIX falling = buy pressure confirmed. VIX rising = sell pressure confirmed.

## Tiered Alert System

- 🔹 **Early Alert** — 1m + 5m aligned
- 🟦 **Strong Signal** — 1m + 5m + 15m aligned (bell rings once)
- 🔵 **Full Confirmation** — All 4 timeframes + VIX (bell rings 3×)

## Features

- Real-time SPY candles: 1m / 5m / 15m / 30m via Tastytrade dxFeed
- VIX real-time feed as Step 3 confirmation
- VWAP and RVOL calculations
- Bell audio alerts on signal triggers
- Colorblind-friendly: Blue (#0066ff) = bullish, Yellow (#ffd700) = bearish
- All timestamps in Eastern Time (ET)
- Auto-reconnect on disconnect

## Requirements

Requires a valid Tastytrade account (free to open). Login with your Tastytrade username and password.

## Disclaimer

Not financial advice. For educational purposes only.
