"""
3-Step Pressure Method Scanner
================================
Real-time SPY daytrading scanner powered by Tastytrade dxFeed.

Step 1: Volume Surge      — Current candle volume > 20-bar SMA
Step 2: Shaved Candle     — Close in top 10% (buy) or bottom 10% (sell) of range
Step 3: VIX Confirmation  — VIX falling = buy pressure | VIX rising = sell pressure

Tiered Alert System:
  1m passes             → Watching
  1m + 5m pass          → Early Alert
  1m + 5m + 15m pass    → Strong Signal  (bell 1×)
  1m + 5m + 15m + 30m   → Full Confirmation (bell 3×)

Colorblind-friendly: Blue (#0066ff) = bullish, Yellow (#ffd700) = bearish
All times in Eastern Time (ET)
"""

import streamlit as st
import pandas as pd
import asyncio
import threading
import time
import json
import base64
import requests as _req
import numpy as np
import wave
import io
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────────────────────────────
# Page config — MUST be first Streamlit call
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="3-Step Pressure Scanner",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Thread-Safe Data Bridge
# Background thread writes here; Streamlit UI reads via get_data()
# NEVER write to st.session_state from a background thread
# ─────────────────────────────────────────────────────────────────────────────
import threading as _threading

_lock = _threading.Lock()
_bridge = {
    "connected": False,
    "status": "Waiting to connect...",
    "error": "",
    "spy_price": 0.0,
    "vix_price": 0.0,
    "vix_prev": 0.0,
    "vwap": 0.0,
    "candles": {
        "1m":  deque(maxlen=120),
        "5m":  deque(maxlen=120),
        "15m": deque(maxlen=120),
        "30m": deque(maxlen=120),
    },
    "tf_state": {"1m": {}, "5m": {}, "15m": {}, "30m": {}},
    "signal": "WAIT",
    "candle_count": 0,
    "last_update": "",
    "stream_log": deque(maxlen=30),
}

def _bset(key, val):
    with _lock:
        _bridge[key] = val

def _blog(msg):
    with _lock:
        _bridge["stream_log"].append(f"[{_et_now()}] {msg}")

def get_data():
    with _lock:
        import copy
        return copy.deepcopy(_bridge)

def _et_now():
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# Bell Audio — generated at startup, cached
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource
def _make_bell(strikes: int) -> str:
    RATE = 44100
    def tone(freq, dur, amp=0.6):
        t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
        env = amp * np.exp(-3.5 * t)
        return env * (
            0.6 * np.sin(2 * np.pi * freq * t) +
            0.3 * np.sin(2 * np.pi * freq * 2.76 * t) +
            0.1 * np.sin(2 * np.pi * freq * 5.4 * t)
        )
    silence = np.zeros(int(RATE * 0.12))
    if strikes == 1:
        audio = tone(880, 1.5, 0.7)
    else:
        audio = np.concatenate([
            tone(880, 1.2, 0.7), silence,
            tone(1047, 1.0, 0.6), silence,
            tone(1319, 0.9, 0.55),
        ])
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode()

BELL1 = _make_bell(1)
BELL3 = _make_bell(3)

def _play_bell(n=3):
    b64 = BELL3 if n == 3 else BELL1
    st.markdown(
        f'<audio autoplay style="display:none">'
        f'<source src="data:audio/wav;base64,{b64}" type="audio/wav"></audio>',
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# CSS — Dark terminal theme, blue/yellow only
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Share+Tech+Mono&display=swap');

html,body,[data-testid="stAppViewContainer"]{
  background:radial-gradient(ellipse at top,#080d1a 0%,#040609 100%) !important;
  color:#c9d1d9; font-family:Arial,Helvetica,sans-serif;}
[data-testid="stSidebar"]{background:#060a12 !important;border-right:1px solid #1a2233;}
[data-testid="stSidebar"] label{color:#8b949e !important;}
[data-testid="stSidebar"] h2{color:#4d9fff !important;}
h1,h2,h3,h4{color:#e6edf3;font-family:'Orbitron',sans-serif;letter-spacing:2px;}
.stButton>button{background:#0d1f3c;color:#4d9fff;border:1px solid #1e3a6e;border-radius:8px;}
.stButton>button:hover{background:#0066ff22;border-color:#0066ff;}
.stTextInput>div>div>input{background:#0d1117;color:#e6edf3;border:1px solid #1e2d45;}
.stCheckbox>label{color:#8b949e !important;}

/* Ticker tape */
.tape{background:#060a12;border-top:1px solid #1a2233;border-bottom:1px solid #1a2233;
  overflow:hidden;white-space:nowrap;padding:7px 0;margin-bottom:14px;}
.tape-inner{display:inline-block;animation:scroll 25s linear infinite;
  font-size:13px;letter-spacing:1px;}
@keyframes scroll{0%{transform:translateX(100vw)}100%{transform:translateX(-100%)}}

/* Metric cards */
.mcard{background:linear-gradient(135deg,#0d1117,#111827);border:1px solid #1e2d45;
  border-radius:14px;padding:18px 14px;text-align:center;margin:3px;
  box-shadow:0 4px 20px rgba(0,0,0,.4);}
.mlabel{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:3px;margin-bottom:6px;}
.mval{font-size:28px;font-weight:900;font-family:'Orbitron',sans-serif;margin:4px 0;}
.msub{font-size:11px;color:#555;}

/* VIX gauge */
.vix-wrap{background:#0d1117;border:1px solid #1a2233;border-radius:14px;padding:16px;text-align:center;}
.vix-label{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:3px;margin-bottom:6px;}
.vix-val{font-size:42px;font-weight:900;font-family:'Orbitron',sans-serif;margin:4px 0;}
.vix-bar-bg{background:#1a2233;border-radius:8px;height:12px;width:100%;margin:8px 0;position:relative;overflow:hidden;}
.vix-bar{height:12px;border-radius:8px;transition:width .4s ease;}

/* Signal banners */
.sig-full-long{
  background:linear-gradient(135deg,#002288,#0066ff,#002288);background-size:200%;
  animation:glow-b .8s infinite alternate,shimmer 3s linear infinite;
  color:#fff;padding:38px 20px;border-radius:18px;text-align:center;margin:10px 0;
  font-family:'Orbitron',sans-serif;box-shadow:0 0 60px #0066ffaa;}
.sig-full-short{
  background:linear-gradient(135deg,#886600,#ffd700,#886600);background-size:200%;
  animation:glow-y .8s infinite alternate,shimmer 3s linear infinite;
  color:#000;padding:38px 20px;border-radius:18px;text-align:center;margin:10px 0;
  font-family:'Orbitron',sans-serif;box-shadow:0 0 60px #ffd700aa;}
.sig-strong-long{background:linear-gradient(135deg,#001a55,#0044cc);color:#fff;
  padding:26px 20px;border-radius:14px;text-align:center;border:2px solid #0066ff;
  margin:10px 0;font-family:'Orbitron',sans-serif;box-shadow:0 0 30px #0066ff55;}
.sig-strong-short{background:linear-gradient(135deg,#332200,#996600);color:#fff;
  padding:26px 20px;border-radius:14px;text-align:center;border:2px solid #ffd700;
  margin:10px 0;font-family:'Orbitron',sans-serif;box-shadow:0 0 30px #ffd70055;}
.sig-early-long{background:#0a0e1a;color:#4d9fff;padding:16px;border-radius:10px;
  text-align:center;border:1px dashed #4d9fff;margin:10px 0;font-family:'Orbitron',sans-serif;}
.sig-early-short{background:#1a1000;color:#ffd700;padding:16px;border-radius:10px;
  text-align:center;border:1px dashed #ffd700;margin:10px 0;font-family:'Orbitron',sans-serif;}
.sig-wait{background:#0a0e1a;color:#2a3a55;padding:34px 20px;border-radius:18px;
  text-align:center;border:1px solid #1a2233;margin:10px 0;font-family:'Orbitron',sans-serif;}

/* Step check cards */
.step-pass{background:linear-gradient(90deg,#0a1a3a,#0d1f45);border-left:5px solid #4d9fff;
  padding:11px 15px;border-radius:8px;margin:4px 0;color:#4d9fff;font-weight:bold;}
.step-fail{background:linear-gradient(90deg,#1a1200,#221800);border-left:5px solid #ffd700;
  padding:11px 15px;border-radius:8px;margin:4px 0;color:#ffd700;font-weight:bold;}
.step-na{background:#0d1117;border-left:5px solid #333;
  padding:11px 15px;border-radius:8px;margin:4px 0;color:#444;font-weight:bold;}

/* Login box */
.login-box{background:#0d1117;border:1px solid #1e2d45;border-radius:16px;
  padding:36px;max-width:460px;margin:50px auto;box-shadow:0 0 60px #0066ff22;}

/* Debug log */
.debug-log{background:#060a12;border:1px solid #1a2233;border-radius:8px;
  padding:12px;font-family:'Share Tech Mono',monospace;font-size:11px;
  color:#4d9fff;max-height:200px;overflow-y:auto;margin-top:8px;}

@keyframes glow-b{from{box-shadow:0 0 40px #0066ff66}to{box-shadow:0 0 80px #0066ffcc}}
@keyframes glow-y{from{box-shadow:0 0 40px #ffd70066}to{box-shadow:0 0 80px #ffd700cc}}
@keyframes shimmer{0%{background-position:0% 50%}100%{background-position:200% 50%}}

[data-testid="stAppViewContainer"]::before{
  content:'';position:fixed;top:0;left:0;right:0;bottom:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.03) 2px,rgba(0,0,0,.03) 4px);
  z-index:9999;}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
TT_API  = "https://api.tastyworks.com"
TT_CERT = "https://api.cert.tastyworks.com"
ET = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = dict(
    authenticated=False,
    session_token="",
    dxlink_url="",
    auth_token="",
    otp_needed=False,
    challenge_token="",
    _login_user="",
    _login_pass="",
    _login_test=False,
    running=False,
    _auto_started=False,
    prev_signal="WAIT",
    show_debug=False,
)
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────
def _login(user, pw, test, otp=None, challenge=None):
    base = TT_CERT if test else TT_API
    hdrs = {"Content-Type": "application/json"}
    if challenge: hdrs["X-Tastyworks-Challenge-Token"] = challenge
    if otp:       hdrs["X-Tastyworks-OTP"] = otp
    return _req.post(f"{base}/sessions",
                     headers=hdrs,
                     json={"login": user, "password": pw, "remember-me": True},
                     timeout=15)

def _trigger_otp(challenge, test):
    base = TT_CERT if test else TT_API
    return _req.post(f"{base}/device-challenge",
                     headers={"Content-Type": "application/json",
                               "X-Tastyworks-Challenge-Token": challenge},
                     timeout=15)

def _get_quote_token(session_token, test):
    base = TT_CERT if test else TT_API
    r = _req.get(f"{base}/api-quote-tokens",
                 headers={"Authorization": session_token}, timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Quote token {r.status_code}: {r.text[:200]}")
    d = r.json().get("data", r.json())
    return d.get("dxlink-url"), d.get("token")

# ─────────────────────────────────────────────────────────────────────────────
# Signal logic (runs in background thread — writes to _bridge only)
# ─────────────────────────────────────────────────────────────────────────────
def _compute_tf(candles, sma_period=20):
    """Compute timeframe state from candle list. Returns dict or None."""
    if len(candles) < 2:
        return None
    df = pd.DataFrame(list(candles))
    df["vol_sma"] = df["volume"].rolling(min(sma_period, len(df))).mean()
    c = df.iloc[-1]
    price  = float(c["close"])
    volume = float(c["volume"])
    sma_v  = float(c["vol_sma"]) if not pd.isna(c["vol_sma"]) else 0.0
    rng    = float(c["high"]) - float(c["low"])
    # Shaved candle: close in top/bottom 10% of range
    st_    = (price >= float(c["high"]) - rng * 0.10) if rng > 0 else False
    sb_    = (price <= float(c["low"])  + rng * 0.10) if rng > 0 else False
    rvol   = (volume / sma_v) if sma_v > 0 else 0.0
    s1     = volume > sma_v and sma_v > 0  # Step 1: volume surge
    return dict(price=price, volume=volume, sma_vol=sma_v, rvol=rvol,
                st=st_, sb=sb_, s1=s1, s2b=st_, s2s=sb_)

def _eval_signal(vix_price, vix_prev, tf_state):
    """
    Step 3: VIX confirmation
      - VIX falling (vix < vix_prev) = buy pressure confirmed
      - VIX rising  (vix > vix_prev) = sell pressure confirmed
    """
    if vix_price <= 0 or vix_prev <= 0:
        vix_bull = vix_bear = False
    else:
        vix_bull = vix_price < vix_prev   # VIX dropping = bullish
        vix_bear = vix_price > vix_prev   # VIX rising   = bearish

    def is_long(k):  return tf_state.get(k, {}).get("s1", False) and tf_state.get(k, {}).get("s2b", False)
    def is_short(k): return tf_state.get(k, {}).get("s1", False) and tf_state.get(k, {}).get("s2s", False)

    if vix_bull and is_long("1m"):
        if is_long("5m") and is_long("15m") and is_long("30m"): sig = "FULL_LONG"
        elif is_long("5m") and is_long("15m"):                   sig = "STRONG_LONG"
        elif is_long("5m"):                                       sig = "EARLY_LONG"
        else:                                                     sig = "WAIT"
    elif vix_bear and is_short("1m"):
        if is_short("5m") and is_short("15m") and is_short("30m"): sig = "FULL_SHORT"
        elif is_short("5m") and is_short("15m"):                     sig = "STRONG_SHORT"
        elif is_short("5m"):                                          sig = "EARLY_SHORT"
        else:                                                         sig = "WAIT"
    else:
        sig = "WAIT"

    return sig

# ─────────────────────────────────────────────────────────────────────────────
# dxFeed WebSocket stream (background thread)
# Uses raw websockets library — tastytrade SDK has asyncio threading issues in Streamlit
# ─────────────────────────────────────────────────────────────────────────────
async def _run_stream(dxlink_url, auth_token, spy_sym, vix_sym, sma_period):
    import websockets

    _blog(f"Connecting to {dxlink_url[:50]}...")

    # Channel assignments — MUST be odd numbers per dxFeed DXLink spec
    CH_1M  = 1
    CH_5M  = 3
    CH_15M = 5
    CH_30M = 7
    CH_VIX = 9   # VIX Quote/Trade

    ch_candle_map = {CH_1M: "1m", CH_5M: "5m", CH_15M: "15m", CH_30M: "30m"}
    from_time = int(datetime(datetime.now(ET).year,
                             datetime.now(ET).month,
                             datetime.now(ET).day,
                             tzinfo=ET).timestamp() * 1000)

    async with websockets.connect(
        dxlink_url,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=10 * 1024 * 1024,
    ) as ws:

        async def send(msg):
            await ws.send(json.dumps(msg))

        async def recv_text():
            data = await ws.recv()
            if isinstance(data, bytes):
                return data.decode('utf-8')
            return data

        # 1. SETUP
        await send({"type": "SETUP", "channel": 0,
                    "version": "0.1-DXF-JS/0.3.0",
                    "minVersion": "0.1-DXF-JS/0.3.0",
                    "keepaliveTimeout": 60,
                    "acceptKeepaliveTimeout": 60})
        _blog("SETUP sent")

        # 2. AUTH
        await send({"type": "AUTH", "channel": 0, "token": auth_token})
        _blog("AUTH sent")

        # Wait for AUTHORIZED
        # NOTE: dxFeed sends UNAUTHORIZED first (initial state), then AUTHORIZED
        # after processing the AUTH message. Must NOT break on UNAUTHORIZED.
        authorized = False
        for _ in range(20):
            raw = await asyncio.wait_for(recv_text(), timeout=8.0)
            msg = json.loads(raw)
            mtype = msg.get("type", "")
            state = msg.get("state", "")
            _blog(f"← {mtype} state={state}")
            if mtype == "AUTH_STATE":
                if state == "AUTHORIZED":
                    authorized = True
                    break
                elif state == "UNAUTHORIZED":
                    # This is the initial state — keep waiting for AUTHORIZED
                    continue
            # Other messages (SETUP response etc.) — keep looping

        if not authorized:
            _blog("ERROR: AUTH failed — check token")
            _bset("error", "AUTH failed — check your Tastytrade credentials")
            return

        _blog("✅ Authorized!")

        # 3. Open channels
        for ch in (CH_1M, CH_5M, CH_15M, CH_30M, CH_VIX):
            await send({"type": "CHANNEL_REQUEST", "channel": ch,
                        "service": "FEED",
                        "parameters": {"contract": "AUTO"}})

        opened = set()
        for _ in range(30):
            raw = await asyncio.wait_for(recv_text(), timeout=5.0)
            msg = json.loads(raw)
            if msg.get("type") == "CHANNEL_OPENED":
                opened.add(msg["channel"])
                _blog(f"Channel {msg['channel']} opened")
            if {CH_1M, CH_5M, CH_15M, CH_30M, CH_VIX}.issubset(opened):
                break

        _blog(f"Channels open: {opened}")

        # 4. Subscribe to candles
        for ch, tf in [(CH_1M, "1m"), (CH_5M, "5m"), (CH_15M, "15m"), (CH_30M, "30m")]:
            await send({
                "type": "FEED_SUBSCRIPTION",
                "channel": ch,
                "add": [{"type": "Candle",
                          "symbol": f"{spy_sym}{{={tf}}}",
                          "fromTime": from_time}]
            })
            _blog(f"Subscribed {spy_sym}{{{tf}}} on ch{ch}")

        # 5. Subscribe to VIX Quote
        await send({
            "type": "FEED_SUBSCRIPTION",
            "channel": CH_VIX,
            "add": [
                {"type": "Quote", "symbol": vix_sym},
                {"type": "Trade", "symbol": vix_sym},
            ]
        })
        _blog(f"Subscribed {vix_sym} Quote/Trade on ch{CH_VIX}")

        _bset("status", f"🔴 LIVE — {spy_sym} 1m/5m/15m/30m + {vix_sym}")
        _bset("connected", True)
        _blog("Stream live ✅")

        # 6. Main receive loop
        while True:
            try:
                raw = await asyncio.wait_for(recv_text(), timeout=3.0)
            except asyncio.TimeoutError:
                await send({"type": "KEEPALIVE", "channel": 0})
                continue

            msg = json.loads(raw)
            mtype = msg.get("type", "")

            if mtype == "KEEPALIVE":
                await send({"type": "KEEPALIVE", "channel": 0})
                continue

            if mtype != "FEED_DATA":
                continue

            ch   = msg.get("channel", 0)
            data = msg.get("data", [])
            if not isinstance(data, list):
                continue

            for evt in data:
                if not isinstance(evt, dict):
                    continue
                etype = evt.get("eventType", "")

                # ── Candle events ─────────────────────────────────────────
                if etype == "Candle" and ch in ch_candle_map:
                    try:
                        close = evt.get("close", 0)
                        if str(close) in ("NaN", "nan", "0", "") or float(close) <= 0:
                            continue
                        row = dict(
                            time   = evt.get("time", 0),
                            open   = float(evt.get("open",  close) or close),
                            high   = float(evt.get("high",  close) or close),
                            low    = float(evt.get("low",   close) or close),
                            close  = float(close),
                            volume = float(evt.get("volume", 0) or 0),
                        )
                        tf_key = ch_candle_map[ch]
                        with _lock:
                            cq = _bridge["candles"][tf_key]
                            # Update existing candle or append new one
                            if cq and cq[-1]["time"] == row["time"]:
                                cq[-1] = row
                            else:
                                cq.append(row)
                            _bridge["candle_count"] += 1
                            _bridge["last_update"] = _et_now()
                            # Update SPY price from 1m candle
                            if tf_key == "1m":
                                _bridge["spy_price"] = row["close"]
                                # Simple VWAP from candle's vwap field if available
                                vwap_val = evt.get("vwap", 0)
                                if vwap_val and str(vwap_val) not in ("NaN", "nan", "0"):
                                    _bridge["vwap"] = float(vwap_val)
                            # Recompute TF state
                            candles_list = list(cq)
                            res = _compute_tf(candles_list, sma_period)
                            if res:
                                _bridge["tf_state"][tf_key] = res
                            # Recompute signal
                            _bridge["signal"] = _eval_signal(
                                _bridge["vix_price"],
                                _bridge["vix_prev"],
                                _bridge["tf_state"],
                            )
                    except Exception as e:
                        _blog(f"Candle parse error: {e}")

                # ── VIX Quote/Trade events ────────────────────────────────
                elif etype in ("Quote", "Trade") and ch == CH_VIX:
                    try:
                        if etype == "Quote":
                            price = evt.get("bidPrice") or evt.get("askPrice")
                        else:
                            price = evt.get("price")
                        if price and str(price) not in ("NaN", "nan", "0"):
                            with _lock:
                                old = _bridge["vix_price"]
                                if old > 0:
                                    _bridge["vix_prev"] = old
                                _bridge["vix_price"] = float(price)
                                _bridge["signal"] = _eval_signal(
                                    _bridge["vix_price"],
                                    _bridge["vix_prev"],
                                    _bridge["tf_state"],
                                )
                    except Exception as e:
                        _blog(f"VIX parse error: {e}")


def _stream_thread(dxlink_url, auth_token, spy_sym, vix_sym, sma_period):
    """Run the async stream in a dedicated event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _run_stream(dxlink_url, auth_token, spy_sym, vix_sym, sma_period)
        )
    except Exception as e:
        _blog(f"Stream crashed: {e}")
        _bset("status", f"❌ Stream error: {str(e)[:100]}")
        _bset("connected", False)
    finally:
        loop.close()


def start_stream(dxlink_url, auth_token, spy_sym, vix_sym, sma_period):
    # Reset bridge
    with _lock:
        _bridge["connected"] = False
        _bridge["candle_count"] = 0
        _bridge["candles"] = {k: deque(maxlen=120) for k in ("1m","5m","15m","30m")}
        _bridge["tf_state"] = {"1m": {}, "5m": {}, "15m": {}, "30m": {}}
        _bridge["signal"] = "WAIT"
        _bridge["stream_log"].clear()
    t = _threading.Thread(
        target=_stream_thread,
        args=(dxlink_url, auth_token, spy_sym, vix_sym, sma_period),
        daemon=True,
    )
    t.start()
    return t


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN SCREEN
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    st.markdown("""
    <div style="text-align:center;padding:30px 0 10px;">
      <div style="font-family:'Orbitron',sans-serif;font-size:28px;font-weight:900;
        color:#4d9fff;letter-spacing:4px;">📡 3-STEP PRESSURE SCANNER</div>
      <div style="color:#555;font-size:13px;margin-top:8px;letter-spacing:2px;">
        TASTYTRADE · DXFEED · REAL-TIME SPY</div>
    </div>
    """, unsafe_allow_html=True)

    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown('<div class="login-box">', unsafe_allow_html=True)
        st.markdown("### 🔐 Connect to Tastytrade")

        with st.form("login_form"):
            username = st.text_input("Username / Email", placeholder="your@email.com")
            password = st.text_input("Password", type="password")
            is_test  = st.checkbox("Use Sandbox (cert) environment", value=False)
            submitted = st.form_submit_button("▶ Connect", type="primary", use_container_width=True)

        if st.session_state.otp_needed:
            st.info("📱 A verification code was sent to your phone. Enter it below.")
            with st.form("otp_form"):
                otp_code = st.text_input("Verification Code", max_chars=8, placeholder="123456")
                otp_sub  = st.form_submit_button("✅ Verify & Connect", type="primary", use_container_width=True)
            if otp_sub and otp_code:
                with st.spinner("Verifying..."):
                    r = _login(
                        st.session_state._login_user,
                        st.session_state._login_pass,
                        st.session_state._login_test,
                        otp=otp_code,
                        challenge=st.session_state.challenge_token,
                    )
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    tok = d.get("session-token", "")
                    try:
                        url, atk = _get_quote_token(tok, st.session_state._login_test)
                        st.session_state.session_token = tok
                        st.session_state.dxlink_url    = url
                        st.session_state.auth_token    = atk
                        st.session_state.authenticated = True
                        st.session_state.otp_needed    = False
                        # Reset stream flags so auto-start fires fresh
                        st.session_state._auto_started = False
                        st.session_state.running       = False
                        st.rerun()
                    except Exception as e:
                        st.error(f"Stream token error: {e}")
                else:
                    st.error(f"Verification failed ({r.status_code}) — try again")
        else:
            otp_sub = False

        if submitted and username and password:
            with st.spinner("Connecting..."):
                r = _login(username, password, is_test)
            if r.status_code in (200, 201):
                d = r.json().get("data", r.json())
                tok = d.get("session-token", "")
                try:
                    url, atk = _get_quote_token(tok, is_test)
                    st.session_state.session_token = tok
                    st.session_state.dxlink_url    = url
                    st.session_state.auth_token    = atk
                    st.session_state.authenticated = True
                    # Reset stream flags so auto-start fires fresh
                    st.session_state._auto_started = False
                    st.session_state.running       = False
                    st.rerun()
                except Exception as e:
                    st.error(f"Stream token error: {e}")
            elif r.status_code in (401, 403):
                challenge = r.headers.get("X-Tastyworks-Challenge-Token", "")
                if challenge:
                    _trigger_otp(challenge, is_test)
                    st.session_state.otp_needed      = True
                    st.session_state.challenge_token = challenge
                    st.session_state._login_user     = username
                    st.session_state._login_pass     = password
                    st.session_state._login_test     = is_test
                    st.rerun()
                else:
                    st.error("Login failed — check username and password")
            else:
                st.error(f"Login error ({r.status_code})")

        st.markdown("</div>", unsafe_allow_html=True)
    st.stop()


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — Settings & Controls
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Settings")
    spy_sym     = st.text_input("SPY Symbol", "SPY").upper().strip()
    vix_sym     = st.text_input("VIX Symbol", "VIX").upper().strip()
    sma_period  = st.slider("Volume SMA Period", 5, 50, 20)
    vix_thresh  = st.slider("VIX Change Threshold (%)", 0.1, 2.0, 0.3, step=0.1,
                             help="Minimum % VIX move to count as Step 3 confirmation")

    st.markdown("---")
    c1, c2 = st.columns(2)
    start_btn = c1.button("▶ Start", type="primary",  use_container_width=True)
    stop_btn  = c2.button("⏹ Stop",  type="secondary", use_container_width=True)

    d = get_data()
    if d["connected"]:
        st.success(f"🔴 LIVE  |  Candles: {d['candle_count']}")
    else:
        st.warning(d["status"])

    st.markdown("---")
    st.session_state.show_debug = st.checkbox("Show Stream Log", value=st.session_state.show_debug)

    if st.button("🔓 Logout", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ── Auto-start on first load after login ─────────────────────────────────────
# Guard: only auto-start when we actually have valid tokens (not during OTP flow)
if (not st.session_state.running
        and not st.session_state._auto_started
        and st.session_state.dxlink_url
        and st.session_state.auth_token):
    st.session_state._auto_started = True
    st.session_state.running = True
    start_stream(
        st.session_state.dxlink_url,
        st.session_state.auth_token,
        spy_sym, vix_sym, sma_period,
    )

if start_btn and not st.session_state.running:
    st.session_state.running = True
    start_stream(
        st.session_state.dxlink_url,
        st.session_state.auth_token,
        spy_sym, vix_sym, sma_period,
    )

if stop_btn:
    st.session_state.running = False
    _bset("connected", False)
    _bset("status", "Stream stopped")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
d = get_data()

# ── Header bar ───────────────────────────────────────────────────────────────
et_time = datetime.now(ET).strftime("%I:%M:%S %p ET")
live_dot = '<span style="color:#4d9fff;">⬤ LIVE</span>' if d["connected"] else '<span style="color:#333;">⬤ OFFLINE</span>'

st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0 2px;">
  <div style="font-family:'Orbitron',sans-serif;font-size:20px;font-weight:900;
    color:#4d9fff;letter-spacing:4px;">📡 3-STEP PRESSURE SCANNER</div>
  <div style="font-size:12px;color:#555;font-family:'Share Tech Mono',monospace;">
    {live_dot} &nbsp; {et_time} &nbsp; Candles: {d['candle_count']}
  </div>
</div>
""", unsafe_allow_html=True)

# ── Ticker tape ───────────────────────────────────────────────────────────────
spy   = d["spy_price"]
vwap  = d["vwap"]
vix   = d["vix_price"]
vix_p = d["vix_prev"]
tf    = d["tf_state"]

spy_col  = "#4d9fff" if spy > vwap > 0 else "#ffd700" if spy < vwap and vwap > 0 else "#e6edf3"
vix_col  = "#ffd700" if vix > vix_p > 0 else "#4d9fff" if vix < vix_p and vix_p > 0 else "#e6edf3"
vix_dir  = "▲" if vix > vix_p > 0 else "▼" if vix < vix_p and vix_p > 0 else "─"

rvol1  = tf.get("1m",  {}).get("rvol", 0)
rvol5  = tf.get("5m",  {}).get("rvol", 0)
rvol15 = tf.get("15m", {}).get("rvol", 0)
rvol30 = tf.get("30m", {}).get("rvol", 0)

tape_parts = [
    f'<span style="color:{spy_col};">{spy_sym} ${spy:,.2f}</span>',
    f'<span style="color:#888;">VWAP ${vwap:,.2f}</span>',
    f'<span style="color:{vix_col};">VIX {vix:.2f} {vix_dir}</span>',
    f'<span style="color:#888;">RVOL 1m:{rvol1:.1f}x  5m:{rvol5:.1f}x  15m:{rvol15:.1f}x  30m:{rvol30:.1f}x</span>',
    f'<span style="color:#555;">Last: {d["last_update"]}</span>',
]
tape_html = " &nbsp;|&nbsp; ".join(tape_parts)
st.markdown(
    f'<div class="tape"><div class="tape-inner">{tape_html}&nbsp;&nbsp;|&nbsp;&nbsp;{tape_html}</div></div>',
    unsafe_allow_html=True,
)

# ── Signal banner + bell ──────────────────────────────────────────────────────
sig      = d["signal"]
prev_sig = st.session_state.prev_signal

if sig != prev_sig:
    if "FULL"   in sig: _play_bell(3)
    elif "STRONG" in sig: _play_bell(1)
    st.session_state.prev_signal = sig

if sig == "FULL_LONG":
    st.markdown(f"""<div class="sig-full-long">
      <div style="font-size:48px;font-weight:900;letter-spacing:4px;">🔵 FULL CONFIRMATION: GO LONG 🔵</div>
      <div style="font-size:15px;opacity:.85;margin-top:8px;">
        1m + 5m + 15m + 30m ALL ALIGNED &nbsp;|&nbsp; VIX FALLING ▼ &nbsp;|&nbsp; VIX: {vix:.2f}</div>
    </div>""", unsafe_allow_html=True)
elif sig == "STRONG_LONG":
    st.markdown(f"""<div class="sig-strong-long">
      <div style="font-size:34px;font-weight:900;letter-spacing:3px;">🟦 STRONG SIGNAL: GO LONG</div>
      <div style="font-size:13px;opacity:.8;margin-top:6px;">
        1m + 5m + 15m ALIGNED &nbsp;|&nbsp; VIX FALLING ▼ &nbsp;|&nbsp; VIX: {vix:.2f}</div>
    </div>""", unsafe_allow_html=True)
elif sig == "EARLY_LONG":
    st.markdown(f"""<div class="sig-early-long">
      <div style="font-size:24px;font-weight:900;letter-spacing:2px;">🔹 EARLY ALERT: LONG WATCH</div>
      <div style="font-size:12px;margin-top:4px;">1m + 5m ALIGNED &nbsp;|&nbsp; VIX FALLING ▼ &nbsp;|&nbsp; Awaiting 15m</div>
    </div>""", unsafe_allow_html=True)
elif sig == "FULL_SHORT":
    st.markdown(f"""<div class="sig-full-short">
      <div style="font-size:48px;font-weight:900;letter-spacing:4px;">🟡 FULL CONFIRMATION: GO SHORT 🟡</div>
      <div style="font-size:15px;opacity:.85;margin-top:8px;">
        1m + 5m + 15m + 30m ALL ALIGNED &nbsp;|&nbsp; VIX RISING ▲ &nbsp;|&nbsp; VIX: {vix:.2f}</div>
    </div>""", unsafe_allow_html=True)
elif sig == "STRONG_SHORT":
    st.markdown(f"""<div class="sig-strong-short">
      <div style="font-size:34px;font-weight:900;letter-spacing:3px;">🟨 STRONG SIGNAL: GO SHORT</div>
      <div style="font-size:13px;opacity:.8;margin-top:6px;">
        1m + 5m + 15m ALIGNED &nbsp;|&nbsp; VIX RISING ▲ &nbsp;|&nbsp; VIX: {vix:.2f}</div>
    </div>""", unsafe_allow_html=True)
elif sig == "EARLY_SHORT":
    st.markdown(f"""<div class="sig-early-short">
      <div style="font-size:24px;font-weight:900;letter-spacing:2px;">🔸 EARLY ALERT: SHORT WATCH</div>
      <div style="font-size:12px;margin-top:4px;">1m + 5m ALIGNED &nbsp;|&nbsp; VIX RISING ▲ &nbsp;|&nbsp; Awaiting 15m</div>
    </div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div class="sig-wait">
      <div style="font-size:26px;font-weight:700;letter-spacing:3px;">── SCANNING FOR SETUP ──</div>
      <div style="font-size:12px;color:#2a3a55;margin-top:6px;">Monitoring 1m / 5m / 15m / 30m + VIX</div>
    </div>""", unsafe_allow_html=True)

# ── VIX gauge + SPY metrics ───────────────────────────────────────────────────
vix_col_main, spy_col_main, vwap_col_main, rvol_col_main = st.columns([2, 1, 1, 1])

with vix_col_main:
    VIX_MAX = 50
    vix_pct = min(max(vix / VIX_MAX * 100, 0), 100)
    vix_is_bull = vix < vix_p and vix_p > 0
    vix_is_bear = vix > vix_p and vix_p > 0
    bar_clr  = "#4d9fff" if vix_is_bull else "#ffd700" if vix_is_bear else "#2a3a55"
    vix_lbl  = "FALLING ▼ (Bullish)" if vix_is_bull else "RISING ▲ (Bearish)" if vix_is_bear else "NEUTRAL"
    vix_text = "#4d9fff" if vix_is_bull else "#ffd700" if vix_is_bear else "#888"
    st.markdown(f"""
    <div class="vix-wrap">
      <div class="vix-label">VIX — STEP 3 CONFIRMATION</div>
      <div class="vix-val" style="color:{vix_text};">{vix:.2f}</div>
      <div style="font-size:11px;color:{vix_text};letter-spacing:2px;margin-bottom:4px;">{vix_lbl}</div>
      <div class="vix-bar-bg">
        <div class="vix-bar" style="width:{vix_pct:.1f}%;background:linear-gradient(90deg,#4d9fff,{bar_clr});"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#444;margin-top:2px;">
        <span>0</span><span>10</span><span>20</span><span>30</span><span>40</span><span>50+</span>
      </div>
      <div style="font-size:11px;color:#444;margin-top:6px;">Prev: {vix_p:.2f} &nbsp;|&nbsp; Change: {vix-vix_p:+.2f}</div>
    </div>
    """, unsafe_allow_html=True)

with spy_col_main:
    above_vwap = spy > vwap > 0
    sc = "#4d9fff" if above_vwap else "#ffd700" if vwap > 0 else "#888"
    st.markdown(f"""<div class="mcard">
      <div class="mlabel">{spy_sym} Price</div>
      <div class="mval" style="color:{sc};">${spy:,.2f}</div>
      <div class="msub">{"▲ Above VWAP" if above_vwap else "▼ Below VWAP" if vwap > 0 else "Awaiting data"}</div>
    </div>""", unsafe_allow_html=True)

with vwap_col_main:
    st.markdown(f"""<div class="mcard">
      <div class="mlabel">VWAP</div>
      <div class="mval" style="color:#888;font-size:22px;">${vwap:,.2f}</div>
      <div class="msub">Volume Weighted Avg</div>
    </div>""", unsafe_allow_html=True)

with rvol_col_main:
    r1 = tf.get("1m", {}).get("rvol", 0)
    rc = "#4d9fff" if r1 >= 1.5 else "#ffd700" if r1 >= 1.0 else "#555"
    st.markdown(f"""<div class="mcard">
      <div class="mlabel">RVOL (1m)</div>
      <div class="mval" style="color:{rc};">{r1:.1f}x</div>
      <div class="msub">{"HIGH VOLUME" if r1>=1.5 else "NORMAL" if r1>=1.0 else "LOW"}</div>
    </div>""", unsafe_allow_html=True)

# ── 3-Step Checklist ──────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:10px;'></div>", unsafe_allow_html=True)
buy_col, sell_col = st.columns(2)

def _card(cls, title, detail=""):
    return (f'<div class="{cls}"><b>{title}</b>'
            + (f'<br><span style="font-size:11px;font-weight:normal;color:#8b949e;">{detail}</span>' if detail else "")
            + '</div>')

def _tf_cards(tf_name, state):
    if not state:
        return (_card("step-fail", f"{tf_name} BUY ⏳", "Waiting for candle data..."),
                _card("step-fail", f"{tf_name} SELL ⏳", "Waiting for candle data..."))
    vol, sma, rvol = state.get("volume",0), state.get("sma_vol",0), state.get("rvol",0)
    s1, s2b, s2s   = state.get("s1",False), state.get("s2b",False), state.get("s2s",False)
    detail = f"Vol: {vol:,.0f} | SMA: {sma:,.0f} | RVOL: {rvol:.1f}x"
    buy_cls  = "step-pass" if (s1 and s2b) else "step-fail"
    sell_cls = "step-pass" if (s1 and s2s) else "step-fail"
    return (_card(buy_cls,  f"{tf_name} BUY  {'✅' if (s1 and s2b) else '❌'}", detail),
            _card(sell_cls, f"{tf_name} SELL {'✅' if (s1 and s2s) else '❌'}", detail))

b1,  s1_c  = _tf_cards("1m",  tf.get("1m",  {}))
b5,  s5_c  = _tf_cards("5m",  tf.get("5m",  {}))
b15, s15_c = _tf_cards("15m", tf.get("15m", {}))
b30, s30_c = _tf_cards("30m", tf.get("30m", {}))

# VIX step card
vix_bull_card = _card(
    "step-pass" if vix_is_bull else "step-fail",
    f"VIX STEP 3 {'✅' if vix_is_bull else '❌'} (BUY)",
    f"VIX: {vix:.2f} → {'FALLING ▼ confirms buy pressure' if vix_is_bull else 'NOT falling'}"
)
vix_bear_card = _card(
    "step-pass" if vix_is_bear else "step-fail",
    f"VIX STEP 3 {'✅' if vix_is_bear else '❌'} (SELL)",
    f"VIX: {vix:.2f} → {'RISING ▲ confirms sell pressure' if vix_is_bear else 'NOT rising'}"
)

with buy_col:
    st.markdown('<h4 style="color:#4d9fff;font-size:13px;letter-spacing:3px;">🔵 BUY PRESSURE CHECKS</h4>',
                unsafe_allow_html=True)
    st.markdown(b1 + b5 + b15 + b30 + vix_bull_card, unsafe_allow_html=True)

with sell_col:
    st.markdown('<h4 style="color:#ffd700;font-size:13px;letter-spacing:3px;">🟡 SELL PRESSURE CHECKS</h4>',
                unsafe_allow_html=True)
    st.markdown(s1_c + s5_c + s15_c + s30_c + vix_bear_card, unsafe_allow_html=True)

# ── Debug / Stream Log ────────────────────────────────────────────────────────
if st.session_state.show_debug:
    st.markdown("---")
    st.markdown("**Stream Log**")
    log_lines = list(d["stream_log"])
    if log_lines:
        log_html = "<br>".join(log_lines[-20:])
        st.markdown(f'<div class="debug-log">{log_html}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="debug-log">No log entries yet...</div>', unsafe_allow_html=True)

    # Show error if any
    if d["error"]:
        st.error(d["error"])

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;color:#1a2233;font-size:10px;padding:14px 0 4px;
  border-top:1px solid #0d1117;margin-top:14px;font-family:Arial,Helvetica,sans-serif;">
  3-STEP PRESSURE METHOD &nbsp;·&nbsp; TASTYTRADE OPEN API + DXFEED DXLINK
  &nbsp;·&nbsp; NOT FINANCIAL ADVICE &nbsp;·&nbsp; {et_time}
</div>
""", unsafe_allow_html=True)

# ── Auto-refresh every 2 seconds while stream is running ─────────────────────
if st.session_state.running:
    time.sleep(2)
    st.rerun()
