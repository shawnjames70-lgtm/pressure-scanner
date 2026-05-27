"""
3-Step Pressure Method Scanner (Minimal)
========================================
Clean dashboard focusing exclusively on:
- NYSE $ADD (Breadth)
- NYSE $TICK (Momentum)
- Volume Surge (Current Vol > 20-period SMA)
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
    page_title="Pressure Scanner",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# CSS — Clean Light Theme
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

/* Metric Cards */
.metric-card{
  background:#ffffff;
  border:1px solid #e2e8f0;border-radius:14px;
  padding:24px 16px;text-align:center;margin:8px;
  box-shadow:0 4px 15px rgba(0,0,0,0.04);}
.metric-label{font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:3px;margin-bottom:12px;}
.metric-value{font-size:48px;font-weight:900;font-family:'Orbitron',sans-serif;margin:8px 0;}
.metric-sub{font-size:14px;color:#555;}

/* Signal Banners */
.signal-long{
  background:linear-gradient(135deg,#0033aa 0%,#0066ff 50%,#0033aa 100%);
  background-size:200% 200%;animation:pg 0.8s infinite alternate,shimmer 3s linear infinite;
  color:#fff;padding:40px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px #0066ffaa;margin:20px 0;
  font-family:'Orbitron',sans-serif;}
.signal-short{
  background:linear-gradient(135deg,#aa7700 0%,#ffd700 50%,#aa7700 100%);
  background-size:200% 200%;animation:pr 0.8s infinite alternate,shimmer 3s linear infinite;
  color:#000;padding:40px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px #ffd700aa;margin:20px 0;
  font-family:'Orbitron',sans-serif;}
.signal-wait{
  background:#ffffff;color:#333;padding:40px 20px;
  border-radius:20px;text-align:center;border:2px dashed #cbd5e1;margin:20px 0;
  font-family:'Orbitron',sans-serif;}

@keyframes pg{from{box-shadow:0 0 20px #0066ff66}to{box-shadow:0 0 60px #0066ffcc}}
@keyframes pr{from{box-shadow:0 0 20px #ffd70066}to{box-shadow:0 0 60px #ffd700cc}}
@keyframes shimmer{0%{background-position:0% 50%}100%{background-position:200% 50%}}

.login-box{
  background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;
  padding:40px;max-width:480px;margin:60px auto;
  box-shadow:0 0 60px #0066ff22;}
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
        remember_token="",
        otp_needed=False,
        challenge_token="",
        running=False,
        
        candles_5m=deque(maxlen=100),
        tick_val=0.0, add_val=0.0, price=0.0,
        
        vol_surge=False, vol_val=0.0, sma_val=0.0,
        signal="WAIT",
        last_update=None,
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
def _compute_vol(candles_list, sma_period):
    if len(candles_list) < 2: return
    df = pd.DataFrame(candles_list)
    df["vol_sma"] = df["volume"].rolling(min(sma_period, len(df))).mean()
    c = df.iloc[-1]
    
    st.session_state.price = float(c["close"])
    st.session_state.vol_val = float(c["volume"])
    st.session_state.sma_val = float(c["vol_sma"]) if not pd.isna(c["vol_sma"]) else 0.0
    st.session_state.vol_surge = st.session_state.vol_val > st.session_state.sma_val and st.session_state.sma_val > 0

def _eval_signal(tick_thr, add_thr):
    tick = st.session_state.tick_val
    add = st.session_state.add_val
    surge = st.session_state.vol_surge
    
    if surge and tick > tick_thr and add > add_thr:
        st.session_state.signal = "LONG"
    elif surge and tick < -tick_thr and add < -add_thr:
        st.session_state.signal = "SHORT"
    else:
        st.session_state.signal = "WAIT"

# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Streamer (Background Thread)
# ─────────────────────────────────────────────────────────────────────────────
def _stream(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold):
    import websockets
    
    CH_5M, TICK_CH, ADD_CH = 3, 9, 11
    
    async def run_ws():
        async with websockets.connect(dx_url, ping_interval=20) as ws:
            async def send(msg): await ws.send(json.dumps(msg))

            await send({"type":"SETUP","channel":0,"version":"0.1-DXF-JS/0.3.0","keepaliveTimeout":60})
            await send({"type":"AUTH","channel":0,"token":auth_token})

            for _ in range(10):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED": break

            for ch in (CH_5M, TICK_CH, ADD_CH):
                await send({"type":"CHANNEL_REQUEST","channel":ch,"service":"FEED","parameters":{"contract":"AUTO"}})

            opened = set()
            for _ in range(20):
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                    if msg.get("type") == "CHANNEL_OPENED": opened.add(msg.get("channel"))
                    if {CH_5M, TICK_CH, ADD_CH}.issubset(opened): break
                except asyncio.TimeoutError: break

            from_time = int(datetime(2024,1,1,tzinfo=timezone.utc).timestamp()*1000)
            
            await send({"type":"FEED_SUBSCRIPTION","channel":CH_5M,
                        "add":[{"type":"Candle","symbol":f"{symbol}{{=5m}}","fromTime":from_time}]})
            await send({"type":"FEED_SUBSCRIPTION","channel":TICK_CH,
                        "add":[{"type":"Trade","symbol":tick_symbol},{"type":"Quote","symbol":tick_symbol}]})
            await send({"type":"FEED_SUBSCRIPTION","channel":ADD_CH,
                        "add":[{"type":"Trade","symbol":add_symbol},{"type":"Quote","symbol":add_symbol}]})

            while st.session_state.running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    t = msg.get("type","")
                    ch = msg.get("channel", 0)

                    if t == "FEED_DATA":
                        data = msg.get("data", [])
                        for ev in data:
                            evt = ev.get("eventType")
                            sym = ev.get("eventSymbol", "")
                            
                            if evt == "Candle" and ch == CH_5M:
                                st.session_state.candles_5m.append({
                                    "time": ev.get("time"),
                                    "open": ev.get("open", 0),
                                    "high": ev.get("high", 0),
                                    "low": ev.get("low", 0),
                                    "close": ev.get("close", 0),
                                    "volume": ev.get("volume", 0)
                                })
                                _compute_vol(list(st.session_state.candles_5m), vol_sma_period)
                                
                            elif evt in ("Trade", "Quote"):
                                val = ev.get("price", 0)
                                if val == 0: continue
                                if ch == TICK_CH: st.session_state.tick_val = val
                                elif ch == ADD_CH: st.session_state.add_val = val

                        _eval_signal(tick_threshold, add_threshold)
                        st.session_state.last_update = time.time()

                    elif t == "KEEPALIVE":
                        await send({"type":"KEEPALIVE","channel":0})

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    break

    while st.session_state.running:
        try:
            asyncio.run(run_ws())
        except Exception:
            time.sleep(2)

def _start_stream(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold):
    st.session_state.running = True
    t = threading.Thread(target=_stream, args=(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold), daemon=True)
    t.start()

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN UI
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    st.markdown('<div class="login-box">', unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center;color:#0066ff;'>Tastytrade Login</h2>", unsafe_allow_html=True)
    
    if not st.session_state.otp_needed:
        with st.form("login_form"):
            username = st.text_input("Username / Email")
            password = st.text_input("Password", type="password")
            is_test  = st.checkbox("Use Certification (Test) Environment", value=False)
            submitted = st.form_submit_button("Connect to Live Stream", use_container_width=True)
            if submitted and username and password:
                r = _do_login(username, password, is_test)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    st.session_state.session_token = d.get("session-token")
                    st.session_state.remember_token = d.get("remember-token")
                    dx_url, dx_token = _get_quote_token(st.session_state.session_token, is_test)
                    st.session_state.dxlink_url, st.session_state.auth_token = dx_url, dx_token
                    st.session_state.authenticated = True
                    st.rerun()
                elif r.status_code in (401, 403):
                    challenge_tok = r.headers.get("X-Tastyworks-Challenge-Token")
                    if challenge_tok:
                        _trigger_device_challenge(challenge_tok, is_test)
                        st.session_state.otp_needed, st.session_state.challenge_token = True, challenge_tok
                        st.session_state._login_user, st.session_state._login_pass, st.session_state._login_test = username, password, is_test
                        st.rerun()
                    else: st.error("Login failed.")
                else: st.error("Login failed.")
    else:
        st.warning("🔒 Enter the 6-digit code sent to your phone.")
        with st.form("otp_form"):
            otp_code = st.text_input("SMS Code")
            otp_sub  = st.form_submit_button("Verify & Connect", use_container_width=True)
            if otp_sub and otp_code:
                r = _do_login(st.session_state._login_user, st.session_state._login_pass, st.session_state._login_test,
                              otp=otp_code, challenge_token=st.session_state.challenge_token)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    st.session_state.session_token = d.get("session-token")
                    st.session_state.remember_token = d.get("remember-token")
                    dx_url, dx_token = _get_quote_token(st.session_state.session_token, st.session_state._login_test)
                    st.session_state.dxlink_url, st.session_state.auth_token = dx_url, dx_token
                    st.session_state.authenticated = True
                    st.rerun()
                else: st.error("Invalid code.")
    st.markdown('</div>', unsafe_allow_html=True)
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
    add_threshold  = st.slider("ADD Threshold", 200, 1000, 500)
    
    cs, cx = st.columns(2)
    start_btn = cs.button("▶ Start", type="primary", use_container_width=True)
    stop_btn  = cx.button("⏹ Stop", type="secondary", use_container_width=True)
    
    if st.session_state.running: st.success("🔴 Stream Active")

if start_btn and not st.session_state.running:
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold)
if stop_btn: st.session_state.running = False
if (not st.session_state.running and not st.session_state.get("_auto_started") and st.session_state.dxlink_url):
    st.session_state._auto_started = True
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold)

# ── Header ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0 20px;">
  <div style="font-family:'Orbitron',sans-serif;font-size:26px;font-weight:900;color:#0066ff;letter-spacing:4px;">
    🚀 CORE PRESSURE SCANNER
  </div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:14px;color:#555;">
    {'<span style="color:#0066ff;">&#9679; LIVE</span>' if st.session_state.running else '<span style="color:#333;">&#9679; OFFLINE</span>'}
    &nbsp;&nbsp;{__import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%I:%M:%S %p ET')}
  </div>
</div>
""", unsafe_allow_html=True)

# ── Signal Banner ─────────────────────────────────────────────────────────────
sig = st.session_state.signal

if sig == "LONG":
    st.markdown(f"""<div class="signal-long">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🔵 GO LONG 🔵</div>
    <div style="font-size:18px;opacity:0.9;margin-top:8px;">Volume Surge + TICK > {tick_threshold} + ADD > {add_threshold}</div></div>""", unsafe_allow_html=True)
elif sig == "SHORT":
    st.markdown(f"""<div class="signal-short">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🟡 GO SHORT 🟡</div>
    <div style="font-size:18px;opacity:0.9;margin-top:8px;">Volume Surge + TICK < -{tick_threshold} + ADD < -{add_threshold}</div></div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div class="signal-wait">
    <div style="font-size:32px;font-weight:700;letter-spacing:3px;color:#64748b;">WAITING FOR SETUP</div>
    <div style="font-size:14px;color:#94a3b8;margin-top:8px;">Monitoring Volume + TICK + ADD</div></div>""", unsafe_allow_html=True)

# ── Core Metrics Row ──────────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)

tick = st.session_state.tick_val
add = st.session_state.add_val
vol = st.session_state.vol_val
sma = st.session_state.sma_val
surge = st.session_state.vol_surge

tc = "#0066ff" if tick > tick_threshold else "#ffd700" if tick < -tick_threshold else "#475569"
ac = "#0066ff" if add > add_threshold else "#ffd700" if add < -add_threshold else "#475569"
vc = "#0066ff" if surge else "#475569"

with m1:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $TICK</div>
    <div class="metric-value" style="color:{tc};">{tick:+.0f}</div>
    <div class="metric-sub">Momentum</div>
    </div>""", unsafe_allow_html=True)

with m2:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $ADD</div>
    <div class="metric-value" style="color:{ac};">{add:+.0f}</div>
    <div class="metric-sub">Breadth</div>
    </div>""", unsafe_allow_html=True)

with m3:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">{symbol} 5m Volume</div>
    <div class="metric-value" style="color:{vc};">{vol/1000:,.0f}k</div>
    <div class="metric-sub">{"SURGE DETECTED" if surge else f"Avg: {sma/1000:,.0f}k"}</div>
    </div>""", unsafe_allow_html=True)

if st.session_state.running:
    time.sleep(1)
    st.rerun()
