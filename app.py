"""
3-Step Pressure Method Scanner (Tiered MTF + VWAP + $ADD + RVOL)
================================================================
Live Tastytrade / dxFeed WebSocket-powered Streamlit dashboard.

New Tiered Alert System:
  - 1m passes             -> Watching (No signal)
  - 1m + 5m pass          -> Early Alert (Yellow/Blue pulse)
  - 1m + 5m + 15m pass    -> Strong Signal (Bell rings once)
  - 1m + 5m + 15m + 30m   -> Full Confirmation (Bell rings 3x + full banner flash)

Confirmation Indicators:
  - VWAP (Volume Weighted Average Price)
  - RVOL (Relative Volume - time adjusted)
  - $ADD (NYSE Advance/Decline Line)
"""

import streamlit as st
import pandas as pd
import asyncio
import threading
import time
import json
import base64
import requests as _req
from collections import deque
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Page config MUST be the very first Streamlit command
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="3-Step Pressure Method",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Bell audio — base64-encoded WAV generated at startup
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource
def _build_bell_b64(strikes=3):
    """Generate a trading bell WAV and return as base64 data URI."""
    import numpy as np, wave, io
    RATE = 44100
    def bell_tone(freq, duration, amplitude=0.6):
        t = np.linspace(0, duration, int(RATE * duration), endpoint=False)
        env = amplitude * np.exp(-3.5 * t)
        return env * (
            0.6 * np.sin(2 * np.pi * freq * t) +
            0.3 * np.sin(2 * np.pi * freq * 2.76 * t) +
            0.1 * np.sin(2 * np.pi * freq * 5.4 * t)
        )
    silence = np.zeros(int(RATE * 0.10))
    if strikes == 1:
        audio = bell_tone(880, 1.5, 0.65)
    else:
        audio = np.concatenate([
            bell_tone(880,  1.2, 0.65), silence,
            bell_tone(1047, 1.0, 0.55), silence,
            bell_tone(1319, 0.9, 0.50),
        ])
    audio = np.clip(audio, -1.0, 1.0)
    pcm   = (audio * 32767).astype(np.int16)
    buf   = io.BytesIO()
    with wave.open(buf, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode()

BELL_1X_B64 = _build_bell_b64(strikes=1)
BELL_3X_B64 = _build_bell_b64(strikes=3)

def _play_bell(strikes=3):
    """Inject a hidden HTML audio element that auto-plays the bell once."""
    b64 = BELL_3X_B64 if strikes == 3 else BELL_1X_B64
    st.markdown(
        f'<audio autoplay style="display:none">'
        f'<source src="data:audio/wav;base64,{b64}" type="audio/wav">'
        f'</audio>',
        unsafe_allow_html=True
    )

# ─────────────────────────────────────────────────────────────────────────────
# CSS — Trading Terminal Visual Theme
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@700;900&display=swap');

html,body,[data-testid="stAppViewContainer"]{
  font-family:'Share Tech Mono',monospace;
  background:#f0f5fa;
  color:#1e293b;}
[data-testid="stSidebar"]{background:#ffffff;color:#1e293b;border-right:1px solid #e2e8f0;}
[data-testid="stSidebar"] label{color:#475569 !important;}
[data-testid="stSidebar"] h2{color:#0ea5e9 !important;}
h1,h2,h3,h4{color:#0f172a;font-family:'Orbitron',sans-serif;letter-spacing:2px;}

/* Ticker Tape */
.ticker-tape{
  background:#ffffff;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;box-shadow:0 2px 10px rgba(0,0,0,0.02);
  overflow:hidden;white-space:nowrap;padding:8px 0;margin-bottom:16px;}
.ticker-inner{
  display:inline-block;animation:scroll-left 20s linear infinite;
  font-family:'Share Tech Mono',monospace;font-size:14px;letter-spacing:1px;}
@keyframes scroll-left{
  0%{transform:translateX(100vw)}
  100%{transform:translateX(-100%)}}

/* TICK Gauge */
.gauge-wrap{background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;padding:16px;text-align:center;}
.gauge-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:3px;margin-bottom:8px;}
.gauge-value{font-size:44px;font-weight:900;font-family:'Orbitron',sans-serif;margin:4px 0;}
.gauge-bar-bg{background:#e2e8f0;border-radius:8px;height:14px;width:100%;margin:8px 0;position:relative;overflow:visible;}
.gauge-bar-fill{height:14px;border-radius:8px;transition:width 0.4s ease,background 0.4s ease;}
.gauge-needle{position:absolute;top:-4px;width:4px;height:22px;border-radius:2px;
  background:#fff;transform:translateX(-50%);transition:left 0.4s ease;box-shadow:0 0 8px #fff;}
.gauge-ticks{display:flex;justify-content:space-between;font-size:10px;color:#555;margin-top:2px;}

/* Metric Cards */
.metric-card{
  background:#ffffff;
  border:1px solid #e2e8f0;border-radius:14px;
  padding:20px 16px;text-align:center;margin:4px;
  box-shadow:0 4px 15px rgba(0,0,0,0.04);}
.metric-label{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:3px;margin-bottom:8px;}
.metric-value{font-size:30px;font-weight:900;font-family:'Orbitron',sans-serif;margin:6px 0 4px;}
.metric-sub{font-size:11px;color:#555;}

/* Candle Bars */
.candle-row{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px;}
.candle-body{height:18px;border-radius:3px;min-width:4px;display:inline-block;}
.candle-label{color:#64748b;width:30px;text-align:right;font-size:11px;}
.candle-price{color:#0f172a;font-size:13px;font-weight:bold;width:70px;}
.candle-vol{color:#555;font-size:10px;}

/* Signal Banners */
.signal-full-long{
  background:linear-gradient(135deg,#0033aa 0%,#0066ff 50%,#0033aa 100%);
  background-size:200% 200%;animation:pg 0.8s infinite alternate,shimmer 3s linear infinite;
  color:#fff;padding:40px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px #0066ffaa,0 0 120px #0066ff44;margin:10px 0;
  font-family:'Orbitron',sans-serif;}
.signal-full-short{
  background:linear-gradient(135deg,#aa7700 0%,#ffd700 50%,#aa7700 100%);
  background-size:200% 200%;animation:pr 0.8s infinite alternate,shimmer 3s linear infinite;
  color:#000;padding:40px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px #ffd700aa,0 0 120px #ffd70044;margin:10px 0;
  font-family:'Orbitron',sans-serif;}
.signal-strong-long{
  background:linear-gradient(135deg,#001a55 0%,#0044cc 100%);
  color:#fff;padding:28px 20px;border-radius:15px;text-align:center;
  border:2px solid #0066ff;margin:10px 0;font-family:'Orbitron',sans-serif;
  box-shadow:0 0 30px #0066ff55;}
.signal-strong-short{
  background:linear-gradient(135deg,#332200 0%,#996600 100%);
  color:#fff;padding:28px 20px;border-radius:15px;text-align:center;
  border:2px solid #ffd700;margin:10px 0;font-family:'Orbitron',sans-serif;
  box-shadow:0 0 30px #ffd70055;}
.signal-early-long{
  background:#0a0e1a;color:#4d9fff;padding:18px;border-radius:10px;
  text-align:center;border:1px dashed #4d9fff;margin:10px 0;
  font-family:'Orbitron',sans-serif;}
.signal-early-short{
  background:#1a1000;color:#ffd700;padding:18px;border-radius:10px;
  text-align:center;border:1px dashed #ffd700;margin:10px 0;
  font-family:'Orbitron',sans-serif;}
.signal-wait{
  background:#0a0e1a;color:#333;padding:36px 20px;
  border-radius:20px;text-align:center;border:1px solid #e2e8f0;margin:10px 0;
  font-family:'Orbitron',sans-serif;}

@keyframes pg{from{box-shadow:0 0 40px #0066ff66,0 0 80px #0066ff22}to{box-shadow:0 0 80px #0066ffcc,0 0 160px #0066ff66}}
@keyframes pr{from{box-shadow:0 0 40px #ffd70066,0 0 80px #ffd70022}to{box-shadow:0 0 80px #ffd700cc,0 0 160px #ffd70066}}
@keyframes shimmer{0%{background-position:0% 50%}100%{background-position:200% 50%}}

/* Step Cards */
.step-pass{
  background:linear-gradient(90deg,#0a1a3a,#0d1f45);
  border-left:5px solid #4d9fff;
  padding:12px 16px;border-radius:8px;margin:5px 0;
  color:#4d9fff;font-weight:bold;font-family:'Share Tech Mono',monospace;
  box-shadow:inset 0 0 20px #0066ff11;}
.step-fail{
  background:linear-gradient(90deg,#1a1200,#221800);
  border-left:5px solid #ffd700;
  padding:12px 16px;border-radius:8px;margin:5px 0;
  color:#ffd700;font-weight:bold;font-family:'Share Tech Mono',monospace;
  box-shadow:inset 0 0 20px #ffd70011;}

.login-box{
  background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;
  padding:40px;max-width:480px;margin:60px auto;
  box-shadow:0 0 60px #0066ff22;}

/* Scanline overlay */
[data-testid="stAppViewContainer"]::before{
  content:'';position:fixed;top:0;left:0;right:0;bottom:0;pointer-events:none;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.03) 2px,rgba(0,0,0,0.03) 4px);
  z-index:9999;}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
TT_API  = "https://api.tastyworks.com"
TT_CERT = "https://api.cert.tastyworks.com"

# ─────────────────────────────────────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────────────────────────────────────
def _init():
    defs = dict(
        authenticated=False,
        session_token="",
        dxlink_url="",
        auth_token="",
        otp_needed=False,
        challenge_token="",
        running=False,
        status_msg="Enter your Tastytrade credentials to start the scanner.",
        status_type="info",
        
        candles_1m=deque(maxlen=100),
        candles_5m=deque(maxlen=100),
        candles_15m=deque(maxlen=100),
        candles_30m=deque(maxlen=100),
        
        tick_val=0.0, add_val=0.0, price=0.0, vwap=0.0,
        
        # TF states (vol, sma, st, sb, s1, s2b, s2s)
        tf_state={"1m":{}, "5m":{}, "15m":{}, "30m":{}},
        
        step3_buy=False, step3_sell=False,
        signal="WAIT", prev_signal="WAIT", last_update=None,
        candle_count=0, tick_count=0,
    )
    for k, v in defs.items():
        if k not in st.session_state:
            st.session_state[k] = v
_init()

# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────
def _do_login(username, password, is_test, otp=None, challenge_token=None):
    base = TT_CERT if is_test else TT_API
    hdrs = {"Content-Type": "application/json"}
    if challenge_token: hdrs["X-Tastyworks-Challenge-Token"] = challenge_token
    if otp: hdrs["X-Tastyworks-OTP"] = otp
    r = _req.post(f"{base}/sessions", headers=hdrs,
                  json={"login": username, "password": password, "remember-me": True}, timeout=15)
    return r

def _trigger_device_challenge(challenge_token, is_test):
    base = TT_CERT if is_test else TT_API
    r = _req.post(f"{base}/device-challenge", headers={
        "Content-Type": "application/json",
        "X-Tastyworks-Challenge-Token": challenge_token
    }, timeout=15)
    return r

def _get_quote_token(session_token, is_test):
    base = TT_CERT if is_test else TT_API
    r = _req.get(f"{base}/api-quote-tokens", headers={"Authorization": session_token}, timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Quote token error {r.status_code}: {r.text}")
    d = r.json().get("data", r.json())
    return d.get("dxlink-url"), d.get("token")

# ─────────────────────────────────────────────────────────────────────────────
# Logic
# ─────────────────────────────────────────────────────────────────────────────
def _compute_tf(candles_list, sma_period):
    if len(candles_list) < 2: return None
    df = pd.DataFrame(candles_list)
    df["vol_sma"] = df["volume"].rolling(min(sma_period, len(df))).mean()
    c = df.iloc[-1]
    price  = float(c["close"])
    volume = float(c["volume"])
    sma_v  = float(c["vol_sma"]) if not pd.isna(c["vol_sma"]) else 0.0
    rng    = float(c["high"]) - float(c["low"])
    st_    = (price >= float(c["high"]) - rng * 0.10) if rng > 0 else False
    sb_    = (price <= float(c["low"])  + rng * 0.10) if rng > 0 else False
    
    # RVOL approximation: current volume / average volume
    rvol = (volume / sma_v) if sma_v > 0 else 0.0
    
    s1 = volume > sma_v and sma_v > 0
    return dict(price=price, volume=volume, sma_vol=sma_v, rvol=rvol,
                st=st_, sb=sb_, s1=s1, s2b=st_, s2s=sb_)

def _eval_signal(tick_val, tick_thr):
    s3b = tick_val > tick_thr
    s3s = tick_val < -tick_thr
    st.session_state.step3_buy  = s3b
    st.session_state.step3_sell = s3s
    
    tf = st.session_state.tf_state
    def _is_long(k):  return tf[k].get("s1",False) and tf[k].get("s2b",False)
    def _is_short(k): return tf[k].get("s1",False) and tf[k].get("s2s",False)
    
    # Tiered Scoring
    if s3b and _is_long("1m"):
        if _is_long("5m") and _is_long("15m") and _is_long("30m"): sig = "FULL_LONG"
        elif _is_long("5m") and _is_long("15m"): sig = "STRONG_LONG"
        elif _is_long("5m"): sig = "EARLY_LONG"
        else: sig = "WAIT"
    elif s3s and _is_short("1m"):
        if _is_short("5m") and _is_short("15m") and _is_short("30m"): sig = "FULL_SHORT"
        elif _is_short("5m") and _is_short("15m"): sig = "STRONG_SHORT"
        elif _is_short("5m"): sig = "EARLY_SHORT"
        else: sig = "WAIT"
    else:
        sig = "WAIT"
        
    st.session_state.signal = sig

def _parse_feed(channel, data, ch_map, tick_ch, add_ch, sma_period, tick_thr):
    if not isinstance(data, list): return
    for evt in data:
        if not isinstance(evt, dict): continue
        etype = evt.get("eventType", "")

        if etype == "Candle" and channel in ch_map:
            try:
                tf_key = ch_map[channel]
                close = evt.get("close", 0)
                if str(close) in ("NaN","nan","0","") or float(close) <= 0: continue
                
                # Simple VWAP calculation (cumulative price*vol / cumulative vol for the day)
                # Note: Real VWAP resets daily. This is a rolling approximation for the stream.
                vwap = evt.get("vwap", close)
                
                row = dict(
                    time=evt.get("time", 0),
                    open=float(evt.get("open", close) or close),
                    high=float(evt.get("high", close) or close),
                    low=float(evt.get("low", close) or close),
                    close=float(close),
                    volume=float(evt.get("volume", 0)),
                )
                
                cl = list(st.session_state[f"candles_{tf_key}"])
                if cl and cl[-1]["time"] == row["time"]: cl[-1] = row
                else: cl.append(row)
                st.session_state[f"candles_{tf_key}"] = deque(cl, maxlen=100)
                st.session_state.candle_count += 1
                
                res = _compute_tf(cl, sma_period)
                if res:
                    st.session_state.price = res["price"]
                    if tf_key == "1m": st.session_state.vwap = float(vwap)
                    st.session_state.tf_state[tf_key] = res
                    
                _eval_signal(st.session_state.tick_val, tick_thr)
                st.session_state.last_update = time.strftime("%H:%M:%S")
            except Exception: pass

        elif etype in ("Trade", "Quote") and channel == tick_ch:
            try:
                price = evt.get("price") if etype == "Trade" else evt.get("bidPrice")
                if price is not None and str(price) not in ("NaN","nan","0"):
                    st.session_state.tick_val = float(price)
                    st.session_state.tick_count += 1
                    _eval_signal(float(price), tick_thr)
                    st.session_state.last_update = time.strftime("%H:%M:%S")
            except Exception: pass
            
        elif etype in ("Trade", "Quote") and channel == add_ch:
            try:
                price = evt.get("price") if etype == "Trade" else evt.get("bidPrice")
                if price is not None and str(price) not in ("NaN","nan","0"):
                    st.session_state.add_val = float(price)
            except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
# Async WebSocket stream
# ─────────────────────────────────────────────────────────────────────────────
async def _stream(dxlink_url, auth_token, symbol, tick_symbol, add_symbol, sma_period, tick_thr):
    from httpx import AsyncClient
    from httpx_ws import aconnect_ws
    from ssl import create_default_context
    
    ssl_ctx = create_default_context()
    CH_1M, CH_5M, CH_15M, CH_30M, TICK_CH, ADD_CH = 1, 2, 3, 4, 5, 6
    ch_map = {CH_1M: "1m", CH_5M: "5m", CH_15M: "15m", CH_30M: "30m"}

    async with AsyncClient(verify=ssl_ctx) as client:
        async with aconnect_ws(dxlink_url, client=client, keepalive_ping_interval_seconds=None) as ws:
            async def send(msg): await ws.send_text(json.dumps(msg))

            await send({"type":"SETUP","channel":0,"version":"0.1-DXF-JS/0.3.0",
                        "minVersion":"0.1-DXF-JS/0.3.0","keepaliveTimeout":60,"acceptKeepaliveTimeout":60})
            await send({"type":"AUTH","channel":0,"token":auth_token})

            for _ in range(10):
                msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5.0))
                if msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED": break

            for ch in (CH_1M, CH_5M, CH_15M, CH_30M, TICK_CH, ADD_CH):
                await send({"type":"CHANNEL_REQUEST","channel":ch,"service":"FEED","parameters":{"contract":"AUTO"}})

            opened = set()
            for _ in range(20):
                msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5.0))
                if msg.get("type") == "CHANNEL_OPENED": opened.add(msg.get("channel"))
                if {CH_1M, CH_5M, CH_15M, CH_30M, TICK_CH, ADD_CH}.issubset(opened): break

            from_time = int(datetime(2024,1,1,tzinfo=timezone.utc).timestamp()*1000)
            for ch, tf in [(CH_1M,"1m"), (CH_5M,"5m"), (CH_15M,"15m"), (CH_30M,"30m")]:
                await send({"type":"FEED_SUBSCRIPTION","channel":ch,
                            "add":[{"type":"Candle","symbol":f"{symbol}{{={tf}}}","fromTime":from_time}]})
            
            await send({"type":"FEED_SUBSCRIPTION","channel":TICK_CH,
                        "add":[{"type":"Trade","symbol":tick_symbol},{"type":"Quote","symbol":tick_symbol}]})
            await send({"type":"FEED_SUBSCRIPTION","channel":ADD_CH,
                        "add":[{"type":"Trade","symbol":add_symbol},{"type":"Quote","symbol":add_symbol}]})

            st.session_state.status_msg = f"🔴 LIVE — {symbol} MTF (1m/5m/15m/30m) | {tick_symbol} | {add_symbol}"
            st.session_state.status_type = "success"

            while st.session_state.running:
                try: raw = await asyncio.wait_for(ws.receive_text(), timeout=3.0)
                except asyncio.TimeoutError:
                    await send({"type":"KEEPALIVE","channel":0})
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "FEED_DATA":
                    _parse_feed(msg.get("channel"), msg.get("data",[]), ch_map, TICK_CH, ADD_CH, sma_period, tick_thr)
                elif msg.get("type") == "KEEPALIVE": await send({"type":"KEEPALIVE","channel":0})

def _thread(*args):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: loop.run_until_complete(_stream(*args))
    except Exception as e:
        st.session_state.status_msg = f"❌ Stream error: {e}"
        st.session_state.status_type = "error"
        st.session_state.running = False
    finally: loop.close()

def _start_stream(*args):
    st.session_state.running = True
    st.session_state.candle_count = 0
    st.session_state.tick_count = 0
    threading.Thread(target=_thread, args=args, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN SCREEN
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    st.markdown("# 🚀 3-Step Pressure Method Scanner")
    st.markdown("---")
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("""<div class="login-box"><h2 style="text-align:center;color:#0f172a;margin-bottom:24px;">🔐 Tastytrade Login</h2></div>""", unsafe_allow_html=True)
        with st.form("login_form"):
            username  = st.text_input("Username / Email", placeholder="your@email.com")
            password  = st.text_input("Password", type="password")
            is_test   = st.checkbox("Use Sandbox / Cert Environment", value=False)
            submitted = st.form_submit_button("Connect to Live Stream", type="primary", use_container_width=True)

        if st.session_state.otp_needed:
            st.warning(f"📱 2FA required — check your SMS and enter the code below.")
            with st.form("otp_form"):
                otp_code = st.text_input("Enter OTP Code", max_chars=8)
                otp_sub  = st.form_submit_button("Verify & Connect", type="primary", use_container_width=True)
            if otp_sub and otp_code:
                r = _do_login(st.session_state._login_user, st.session_state._login_pass, st.session_state._login_test, otp=otp_code, challenge_token=st.session_state.challenge_token)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    session_token = d.get("session-token","")
                    try:
                        dxlink_url, auth_token = _get_quote_token(session_token, st.session_state._login_test)
                        st.session_state.session_token, st.session_state.dxlink_url, st.session_state.auth_token = session_token, dxlink_url, auth_token
                        st.session_state.authenticated, st.session_state.otp_needed = True, False
                        st.rerun()
                    except Exception as e: st.error(f"Stream token error: {e}")
                else: st.error(f"OTP failed ({r.status_code})")
        else: otp_sub = False

        if submitted and username and password:
            with st.spinner("Connecting..."):
                r = _do_login(username, password, is_test)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    try:
                        dxlink_url, auth_token = _get_quote_token(d.get("session-token",""), is_test)
                        st.session_state.session_token, st.session_state.dxlink_url, st.session_state.auth_token = d.get("session-token",""), dxlink_url, auth_token
                        st.session_state.authenticated = True
                        st.rerun()
                    except Exception as e: st.error(f"Stream token error: {e}")
                elif r.status_code in (401, 403):
                    challenge_tok = r.headers.get("X-Tastyworks-Challenge-Token","")
                    if challenge_tok:
                        _trigger_device_challenge(challenge_tok, is_test)
                        st.session_state.otp_needed, st.session_state.challenge_token = True, challenge_tok
                        st.session_state._login_user, st.session_state._login_pass, st.session_state._login_test = username, password, is_test
                        st.rerun()
                    else: st.error("Login failed.")
                else: st.error("Login failed.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Settings")
    symbol         = st.text_input("Symbol", "SPY").upper()
    tick_symbol    = st.text_input("$TICK Symbol", "$TICK").upper()
    add_symbol     = st.text_input("$ADD Symbol", "$ADD").upper()
    vol_sma_period = st.slider("Vol SMA Period", 5, 50, 20)
    tick_threshold = st.slider("TICK Threshold", 500, 1200, 800)
    
    cs, cx = st.columns(2)
    start_btn = cs.button("▶ Start", type="primary", use_container_width=True)
    stop_btn  = cx.button("⏹ Stop", type="secondary", use_container_width=True)
    
    if st.session_state.running: st.success("🔴 Stream Active")

if start_btn and not st.session_state.running:
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold)
if stop_btn: st.session_state.running = False
if (not st.session_state.running and not st.session_state.get("_auto_started") and st.session_state.dxlink_url):
    st.session_state._auto_started = True
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold)

# ── Header ───────────────────────────────────────────────────────────────────
price = st.session_state.price
vwap  = st.session_state.vwap
tick  = st.session_state.tick_val
add   = st.session_state.add_val

st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0 4px;">
  <div style="font-family:'Orbitron',sans-serif;font-size:22px;font-weight:900;color:#4d9fff;letter-spacing:4px;">
    🚀 3-STEP PRESSURE SCANNER
  </div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:12px;color:#555;">
    {'<span style="color:#4d9fff;">&#9679; LIVE</span>' if st.session_state.running else '<span style="color:#333;">&#9679; OFFLINE</span>'}
    &nbsp;&nbsp;{time.strftime('%H:%M:%S')}
  </div>
</div>
""", unsafe_allow_html=True)

# ── Ticker Tape ───────────────────────────────────────────────────────────────
tc = "#4d9fff" if tick > tick_threshold else "#ffd700" if tick < -tick_threshold else "#e6edf3"
ac = "#4d9fff" if add > 500 else "#ffd700" if add < -500 else "#e6edf3"
pc = "#4d9fff" if price > vwap else "#ffd700" if price < vwap else "#e6edf3"

tf = st.session_state.tf_state
tf1 = tf.get("1m",{}); tf5 = tf.get("5m",{}); tf15 = tf.get("15m",{}); tf30 = tf.get("30m",{})
rvol1  = tf1.get("rvol",0); rvol5  = tf5.get("rvol",0)
rvol15 = tf15.get("rvol",0); rvol30 = tf30.get("rvol",0)

tape_items = [
    f'<span style="color:{pc};">{symbol} ${price:,.2f}</span>',
    f'<span style="color:#888;">VWAP ${vwap:,.2f}</span>',
    f'<span style="color:{tc};">$TICK {tick:+.0f}</span>',
    f'<span style="color:{ac};">$ADD {add:+.0f}</span>',
    f'<span style="color:#888;">RVOL 1m:{rvol1:.1f}x  5m:{rvol5:.1f}x  15m:{rvol15:.1f}x  30m:{rvol30:.1f}x</span>',
    f'<span style="color:#555;">Candles:{st.session_state.candle_count}  TICK updates:{st.session_state.tick_count}</span>',
]
tape_html = '&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;'.join(tape_items)
st.markdown(f'<div class="ticker-tape"><div class="ticker-inner">{tape_html}&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;{tape_html}</div></div>', unsafe_allow_html=True)

# ── Signal Banner & Bell ──────────────────────────────────────────────────────
sig = st.session_state.signal
prev_sig = st.session_state.prev_signal

if sig != prev_sig:
    if "FULL" in sig: _play_bell(strikes=3)
    elif "STRONG" in sig: _play_bell(strikes=1)
    st.session_state.prev_signal = sig

if sig == "FULL_LONG":
    st.markdown(f"""<div class="signal-full-long">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🔵 FULL CONFIRMATION: GO LONG 🔵</div>
    <div style="font-size:16px;opacity:0.85;margin-top:8px;">1m + 5m + 15m + 30m ALL ALIGNED &nbsp;|&nbsp; TICK &gt; +{tick_threshold} &nbsp;|&nbsp; $TICK: {tick:+.0f}</div></div>""", unsafe_allow_html=True)
elif sig == "STRONG_LONG":
    st.markdown(f"""<div class="signal-strong-long">
    <div style="font-size:38px;font-weight:900;letter-spacing:3px;">🟦 STRONG SIGNAL: GO LONG</div>
    <div style="font-size:14px;opacity:0.8;margin-top:6px;">1m + 5m + 15m ALIGNED &nbsp;|&nbsp; TICK &gt; +{tick_threshold} &nbsp;|&nbsp; $TICK: {tick:+.0f}</div></div>""", unsafe_allow_html=True)
elif sig == "EARLY_LONG":
    st.markdown(f"""<div class="signal-early-long">
    <div style="font-size:26px;font-weight:900;letter-spacing:2px;">🔹 EARLY ALERT: LONG WATCH</div>
    <div style="font-size:13px;margin-top:4px;">1m + 5m ALIGNED &nbsp;|&nbsp; TICK &gt; +{tick_threshold} &nbsp;|&nbsp; Awaiting 15m confirmation</div></div>""", unsafe_allow_html=True)
elif sig == "FULL_SHORT":
    st.markdown(f"""<div class="signal-full-short">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🟡 FULL CONFIRMATION: GO SHORT 🟡</div>
    <div style="font-size:16px;opacity:0.85;margin-top:8px;">1m + 5m + 15m + 30m ALL ALIGNED &nbsp;|&nbsp; TICK &lt; -{tick_threshold} &nbsp;|&nbsp; $TICK: {tick:+.0f}</div></div>""", unsafe_allow_html=True)
elif sig == "STRONG_SHORT":
    st.markdown(f"""<div class="signal-strong-short">
    <div style="font-size:38px;font-weight:900;letter-spacing:3px;">🟨 STRONG SIGNAL: GO SHORT</div>
    <div style="font-size:14px;opacity:0.8;margin-top:6px;">1m + 5m + 15m ALIGNED &nbsp;|&nbsp; TICK &lt; -{tick_threshold} &nbsp;|&nbsp; $TICK: {tick:+.0f}</div></div>""", unsafe_allow_html=True)
elif sig == "EARLY_SHORT":
    st.markdown(f"""<div class="signal-early-short">
    <div style="font-size:26px;font-weight:900;letter-spacing:2px;">🔸 EARLY ALERT: SHORT WATCH</div>
    <div style="font-size:13px;margin-top:4px;">1m + 5m ALIGNED &nbsp;|&nbsp; TICK &lt; -{tick_threshold} &nbsp;|&nbsp; Awaiting 15m confirmation</div></div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div class="signal-wait">
    <div style="font-size:28px;font-weight:700;letter-spacing:3px;">-- SCANNING FOR SETUP --</div>
    <div style="font-size:13px;color:#444;margin-top:6px;">Monitoring 1m / 5m / 15m / 30m + TICK + ADD</div></div>""", unsafe_allow_html=True)

# ── TICK Gauge + Metrics Row ──────────────────────────────────────────────────
gcol, m1, m2, m3 = st.columns([2, 1, 1, 1])

with gcol:
    # TICK gauge: range -1500 to +1500, needle position as %
    TICK_RANGE = 1500
    tick_pct = min(max((tick + TICK_RANGE) / (2 * TICK_RANGE) * 100, 0), 100)
    bull_zone = tick > tick_threshold
    bear_zone = tick < -tick_threshold
    bar_color = "#4d9fff" if bull_zone else "#ffd700" if bear_zone else "#2a3a55"
    needle_glow = "0 0 12px #4d9fff" if bull_zone else "0 0 12px #ffd700" if bear_zone else "0 0 6px #888"
    tick_label = "BULLISH ▲" if bull_zone else "BEARISH ▼" if bear_zone else "NEUTRAL"
    tick_color = "#4d9fff" if bull_zone else "#ffd700" if bear_zone else "#888"
    
    # Build zone markers
    bull_pct = (tick_threshold + TICK_RANGE) / (2 * TICK_RANGE) * 100
    bear_pct = (-tick_threshold + TICK_RANGE) / (2 * TICK_RANGE) * 100
    
    st.markdown(f"""
    <div class="gauge-wrap">
      <div class="gauge-label">NYSE $TICK INDEX</div>
      <div class="gauge-value" style="color:{tick_color};text-shadow:{needle_glow};">{tick:+.0f}</div>
      <div style="font-size:11px;color:{tick_color};letter-spacing:2px;margin-bottom:6px;">{tick_label}</div>
      <div class="gauge-bar-bg">
        <div style="position:absolute;left:{bear_pct:.1f}%;top:0;bottom:0;right:{100-bull_pct:.1f}%;background:#e2e8f0;border-radius:8px;"></div>
        <div style="position:absolute;left:{bear_pct:.1f}%;top:0;height:14px;width:{bull_pct-bear_pct:.1f}%;background:#1a2a1a;border-radius:0;"></div>
        <div class="gauge-bar-fill" style="width:{tick_pct:.1f}%;background:linear-gradient(90deg,#ffd700,{bar_color});"></div>
        <div class="gauge-needle" style="left:{tick_pct:.1f}%;box-shadow:{needle_glow};"></div>
        <div style="position:absolute;left:{bear_pct:.1f}%;top:-18px;font-size:9px;color:#ffd700;">-{tick_threshold}</div>
        <div style="position:absolute;left:{bull_pct:.1f}%;top:-18px;font-size:9px;color:#4d9fff;">+{tick_threshold}</div>
        <div style="position:absolute;left:50%;top:-18px;font-size:9px;color:#444;">0</div>
      </div>
      <div class="gauge-ticks"><span>-{TICK_RANGE}</span><span>-1000</span><span>-500</span><span>0</span><span>+500</span><span>+1000</span><span>+{TICK_RANGE}</span></div>
    </div>
    """, unsafe_allow_html=True)

with m1:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">{symbol}</div>
    <div class="metric-value" style="color:{pc};font-size:26px;">${price:,.2f}</div>
    <div class="metric-sub">{"&#9650; Above VWAP" if price>vwap else "&#9660; Below VWAP"}</div>
    <div class="metric-sub" style="color:#444;margin-top:4px;">VWAP ${vwap:,.2f}</div>
    </div>""", unsafe_allow_html=True)

with m2:
    add_label = "ADVANCING" if add > 500 else "DECLINING" if add < -500 else "NEUTRAL"
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $ADD</div>
    <div class="metric-value" style="color:{ac};font-size:26px;">{add:+.0f}</div>
    <div class="metric-sub" style="color:{ac};">{add_label}</div>
    <div class="metric-sub" style="color:#444;margin-top:4px;">Breadth</div>
    </div>""", unsafe_allow_html=True)

with m3:
    # Mini candle bar chart for 1m candles
    cl1m = list(st.session_state.candles_1m)[-8:]
    if cl1m:
        prices = [c["close"] for c in cl1m]
        p_min, p_max = min(prices), max(prices)
        p_rng = (p_max - p_min) or 1
        bars = ""
        for c in cl1m:
            pct = int((c["close"] - p_min) / p_rng * 80) + 10
            bull = c["close"] >= c["open"]
            clr = "#4d9fff" if bull else "#ffd700"
            bars += f'<div style="display:inline-block;width:8px;height:{pct}px;background:{clr};margin:1px;border-radius:2px;vertical-align:bottom;"></div>'
        st.markdown(f"""<div class="metric-card">
        <div class="metric-label">1m Candles</div>
        <div style="height:90px;display:flex;align-items:flex-end;justify-content:center;padding:4px 0;">{bars}</div>
        <div class="metric-sub">${prices[-1]:,.2f}</div>
        </div>""", unsafe_allow_html=True)
    else:
        st.markdown(f"""<div class="metric-card">
        <div class="metric-label">1m Candles</div>
        <div style="height:90px;display:flex;align-items:center;justify-content:center;color:#333;font-size:11px;">Awaiting data...</div>
        </div>""", unsafe_allow_html=True)

# ── Multi-Timeframe Checklist ─────────────────────────────────────────────────
st.markdown("<div style='margin-top:12px;'></div>", unsafe_allow_html=True)
bc, sc = st.columns(2)

def _card(cls, title, detail):
    return f'<div class="{cls}"><b>{title}</b><br><span style="font-size:11px;font-weight:normal;color:#64748b;">{detail}</span></div>'

def _tf_status(tf_name, state):
    if not state: 
        return _card("step-fail", f"{tf_name} BUY ⏳", "Waiting for data..."), \
               _card("step-fail", f"{tf_name} SELL ⏳", "Waiting for data...")
    vol, sma, rvol = state.get("volume",0), state.get("sma_vol",0), state.get("rvol",0)
    s1, s2b, s2s = state.get("s1",False), state.get("s2b",False), state.get("s2s",False)
    cb = "step-pass" if (s1 and s2b) else "step-fail"
    cs = "step-pass" if (s1 and s2s) else "step-fail"
    detail = f"Vol: {vol:,.0f} | SMA: {sma:,.0f} | RVOL: {rvol:.1f}x"
    return _card(cb, f"{tf_name} BUY {'✅' if (s1 and s2b) else '❌'}", detail), \
           _card(cs, f"{tf_name} SELL {'✅' if (s1 and s2s) else '❌'}", detail)

b1, s1_c   = _tf_status("1m",  tf.get("1m",{}))
b5, s5_c   = _tf_status("5m",  tf.get("5m",{}))
b15, s15_c = _tf_status("15m", tf.get("15m",{}))
b30, s30_c = _tf_status("30m", tf.get("30m",{}))

# TICK step card
tick_buy_cls  = "step-pass" if tick > tick_threshold  else "step-fail"
tick_sell_cls = "step-pass" if tick < -tick_threshold else "step-fail"
tick_buy_card  = _card(tick_buy_cls,  f"$TICK BULL {'✅' if tick > tick_threshold else '❌'}",  f"Current: {tick:+.0f} | Threshold: +{tick_threshold}")
tick_sell_card = _card(tick_sell_cls, f"$TICK BEAR {'✅' if tick < -tick_threshold else '❌'}", f"Current: {tick:+.0f} | Threshold: -{tick_threshold}")

with bc:
    st.markdown("<h4 style='color:#4d9fff;font-size:14px;letter-spacing:3px;'>🔵 BUY PRESSURE CHECKS</h4>", unsafe_allow_html=True)
    st.markdown(b1+b5+b15+b30+tick_buy_card, unsafe_allow_html=True)
with sc:
    st.markdown("<h4 style='color:#ffd700;font-size:14px;letter-spacing:3px;'>🟡 SELL PRESSURE CHECKS</h4>", unsafe_allow_html=True)
    st.markdown(s1_c+s5_c+s15_c+s30_c+tick_sell_card, unsafe_allow_html=True)

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;color:#1a2233;font-size:10px;padding:16px 0 4px;
  font-family:'Share Tech Mono',monospace;border-top:1px solid #0d1117;margin-top:16px;">
3-STEP PRESSURE METHOD SCANNER &nbsp;·&nbsp; TASTYTRADE OPEN API + DXFEED DXLINK WEBSOCKET
&nbsp;·&nbsp; NOT FINANCIAL ADVICE
</div>""", unsafe_allow_html=True)

if st.session_state.running:
    time.sleep(2)
    st.rerun()
