"""
3-Step Pressure Method Scanner
================================
Core indicators: NYSE $TICK, NYSE $ADD, Volume Surge
Uses a thread-safe shared dict for background WebSocket -> UI data bridge.
"""

import streamlit as st
import pandas as pd
import asyncio
import threading
import time
import json
import requests as _req
from collections import deque
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Page config — MUST be first Streamlit command
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Pressure Scanner",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# THREAD-SAFE SHARED STATE (module-level — survives Streamlit reruns)
# ─────────────────────────────────────────────────────────────────────────────
import threading as _threading

_lock = _threading.Lock()
_data = {
    "tick_val": 0.0,
    "add_val": 0.0,
    "price": 0.0,
    "vol_val": 0.0,
    "sma_val": 0.0,
    "vol_surge": False,
    "signal": "WAIT",
    "last_update": None,
    "running": False,
    "candles": deque(maxlen=100),
    "error": None,
}

def _set(**kwargs):
    with _lock:
        _data.update(kwargs)

def _get(key, default=None):
    with _lock:
        return _data.get(key, default)

# ─────────────────────────────────────────────────────────────────────────────
# CSS — Light Blue Theme
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&display=swap');

html,body,[data-testid="stAppViewContainer"]{
  background:#eef4fb !important;
  color:#0f172a;}
[data-testid="stSidebar"]{background:#ffffff !important;border-right:2px solid #bfdbfe;}
h1,h2,h3,h4{font-family:'Orbitron',sans-serif;letter-spacing:2px;}

.metric-card{
  background:#ffffff;
  border:2px solid #bfdbfe;border-radius:16px;
  padding:28px 16px;text-align:center;
  box-shadow:0 4px 20px rgba(0,102,255,0.08);}
.metric-label{font-size:11px;color:#64748b;text-transform:uppercase;
  letter-spacing:3px;margin-bottom:12px;font-weight:700;}
.metric-value{font-size:52px;font-weight:900;font-family:'Orbitron',sans-serif;margin:8px 0;}
.metric-sub{font-size:13px;color:#475569;margin-top:6px;}

.signal-long{
  background:linear-gradient(135deg,#0033cc,#0066ff,#0033cc);
  background-size:200%;animation:pulse-b 0.9s infinite alternate;
  color:#fff;padding:44px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px rgba(0,102,255,0.6);margin:20px 0;
  font-family:'Orbitron',sans-serif;}
.signal-short{
  background:linear-gradient(135deg,#996600,#ffd700,#996600);
  background-size:200%;animation:pulse-y 0.9s infinite alternate;
  color:#111;padding:44px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 60px rgba(255,215,0,0.6);margin:20px 0;
  font-family:'Orbitron',sans-serif;}
.signal-wait{
  background:#ffffff;color:#64748b;padding:44px 20px;
  border-radius:20px;text-align:center;border:2px dashed #93c5fd;margin:20px 0;
  font-family:'Orbitron',sans-serif;}

@keyframes pulse-b{from{box-shadow:0 0 20px rgba(0,102,255,0.4)}to{box-shadow:0 0 70px rgba(0,102,255,0.9)}}
@keyframes pulse-y{from{box-shadow:0 0 20px rgba(255,215,0,0.4)}to{box-shadow:0 0 70px rgba(255,215,0,0.9)}}

.login-box{
  background:#ffffff;border:2px solid #bfdbfe;border-radius:20px;
  padding:48px;max-width:480px;margin:60px auto;
  box-shadow:0 0 60px rgba(0,102,255,0.12);}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
TT_API  = "https://api.tastyworks.com"
TT_CERT = "https://api.cert.tastyworks.com"

# ─────────────────────────────────────────────────────────────────────────────
# Session state init (UI state only — not stream data)
# ─────────────────────────────────────────────────────────────────────────────
def _init():
    defs = dict(
        authenticated=False, session_token="", dxlink_url="", auth_token="",
        otp_needed=False, challenge_token="",
        _login_user="", _login_pass="", _login_test=False,
        _auto_started=False,
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
    return _req.post(f"{base}/sessions", headers=hdrs,
                     json={"login": username, "password": password, "remember-me": True}, timeout=15)

def _trigger_device_challenge(challenge_token, is_test):
    base = TT_CERT if is_test else TT_API
    return _req.post(f"{base}/device-challenge", headers={
        "Content-Type": "application/json",
        "X-Tastyworks-Challenge-Token": challenge_token
    }, timeout=15)

def _get_quote_token(session_token, is_test):
    base = TT_CERT if is_test else TT_API
    r = _req.get(f"{base}/api-quote-tokens", headers={"Authorization": session_token}, timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Quote token error {r.status_code}: {r.text}")
    d = r.json().get("data", r.json())
    return d.get("dxlink-url"), d.get("token")

# ─────────────────────────────────────────────────────────────────────────────
# Signal logic (called from stream thread, writes to _data)
# ─────────────────────────────────────────────────────────────────────────────
def _compute_and_signal(candles_list, vol_sma_period, tick_thr, add_thr):
    if len(candles_list) < 2:
        return
    df = pd.DataFrame(candles_list)
    df["vol_sma"] = df["volume"].rolling(min(vol_sma_period, len(df))).mean()
    c = df.iloc[-1]
    
    price = float(c["close"]) if not pd.isna(c["close"]) else 0.0
    vol = float(c["volume"]) if not pd.isna(c["volume"]) else 0.0
    sma = float(c["vol_sma"]) if not pd.isna(c["vol_sma"]) else 0.0
    surge = vol > sma and sma > 0
    
    with _lock:
        tick = _data["tick_val"]
        add = _data["add_val"]
    
    if surge and tick > tick_thr and add > add_thr:
        sig = "LONG"
    elif surge and tick < -tick_thr and add < -add_thr:
        sig = "SHORT"
    else:
        sig = "WAIT"
    
    _set(price=price, vol_val=vol, sma_val=sma, vol_surge=surge, signal=sig, last_update=time.time())

# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Streamer (Background Thread — writes only to _data dict)
# ─────────────────────────────────────────────────────────────────────────────
def _stream_thread(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold):
    import websockets
    
    CH_5M, TICK_CH, ADD_CH = 3, 9, 11
    
    async def run_ws():
        _set(error=None)
        try:
            async with websockets.connect(dx_url, ping_interval=20, open_timeout=15) as ws:
                async def send(msg): await ws.send(json.dumps(msg))

                await send({"type":"SETUP","channel":0,"version":"0.1-DXF-JS/0.3.0","keepaliveTimeout":60})
                await send({"type":"AUTH","channel":0,"token":auth_token})

                # Wait for auth
                authorized = False
                for _ in range(15):
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                        if msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                            authorized = True
                            break
                    except asyncio.TimeoutError:
                        break
                
                if not authorized:
                    _set(error="WebSocket auth failed")
                    return

                # Open channels (odd numbers only — dxFeed requirement)
                for ch in (CH_5M, TICK_CH, ADD_CH):
                    await send({"type":"CHANNEL_REQUEST","channel":ch,"service":"FEED","parameters":{"contract":"AUTO"}})
                    await asyncio.sleep(0.4)

                opened = set()
                for _ in range(30):
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                        if msg.get("type") == "CHANNEL_OPENED":
                            opened.add(msg.get("channel"))
                        if {CH_5M, TICK_CH, ADD_CH}.issubset(opened):
                            break
                    except asyncio.TimeoutError:
                        break

                from_time = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
                
                if CH_5M in opened:
                    await send({"type":"FEED_SUBSCRIPTION","channel":CH_5M,
                                "add":[{"type":"Candle","symbol":f"{symbol}{{=5m}}","fromTime":from_time}]})
                if TICK_CH in opened:
                    await send({"type":"FEED_SUBSCRIPTION","channel":TICK_CH,
                                "add":[{"type":"Trade","symbol":tick_symbol},{"type":"Quote","symbol":tick_symbol}]})
                if ADD_CH in opened:
                    await send({"type":"FEED_SUBSCRIPTION","channel":ADD_CH,
                                "add":[{"type":"Trade","symbol":add_symbol},{"type":"Quote","symbol":add_symbol}]})

                while _get("running"):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        msg = json.loads(raw)
                        t = msg.get("type", "")
                        ch = msg.get("channel", 0)

                        if t == "FEED_DATA":
                            data = msg.get("data", [])
                            for ev in data:
                                evt = ev.get("eventType")
                                
                                if evt == "Candle" and ch == CH_5M:
                                    close_v = ev.get("close", 0)
                                    vol_v = ev.get("volume", 0)
                                    if close_v and not (isinstance(close_v, float) and close_v != close_v):
                                        with _lock:
                                            _data["candles"].append({
                                                "time": ev.get("time"),
                                                "open": ev.get("open", 0),
                                                "high": ev.get("high", 0),
                                                "low": ev.get("low", 0),
                                                "close": close_v,
                                                "volume": vol_v or 0,
                                            })
                                        _compute_and_signal(list(_data["candles"]), vol_sma_period, tick_threshold, add_threshold)
                                
                                elif evt in ("Trade", "Quote"):
                                    val = ev.get("price", 0)
                                    if val and val == val and val != 0:
                                        if ch == TICK_CH:
                                            _set(tick_val=float(val))
                                        elif ch == ADD_CH:
                                            _set(add_val=float(val))
                                        _set(last_update=time.time())

                        elif t == "KEEPALIVE":
                            await send({"type":"KEEPALIVE","channel":0})

                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break

        except Exception as e:
            _set(error=str(e))

    while _get("running"):
        try:
            asyncio.run(run_ws())
        except Exception as e:
            _set(error=str(e))
        if _get("running"):
            time.sleep(3)

def _start_stream(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold):
    _set(running=True)
    t = threading.Thread(
        target=_stream_thread,
        args=(dx_url, auth_token, symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold),
        daemon=True
    )
    t.start()

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN UI
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    st.markdown('<div class="login-box">', unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center;color:#0066ff;font-family:Orbitron,sans-serif;'>🚀 Pressure Scanner</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#64748b;'>Tastytrade Login</p>", unsafe_allow_html=True)
    
    if not st.session_state.otp_needed:
        with st.form("login_form"):
            username = st.text_input("Username / Email")
            password = st.text_input("Password", type="password")
            is_test  = st.checkbox("Use Certification (Test) Environment", value=False)
            submitted = st.form_submit_button("Connect to Live Stream", use_container_width=True)
            if submitted and username and password:
                with st.spinner("Authenticating..."):
                    r = _do_login(username, password, is_test)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    st.session_state.session_token = d.get("session-token")
                    dx_url, dx_token = _get_quote_token(st.session_state.session_token, is_test)
                    st.session_state.dxlink_url, st.session_state.auth_token = dx_url, dx_token
                    st.session_state.authenticated = True
                    st.rerun()
                elif r.status_code in (401, 403):
                    challenge_tok = r.headers.get("X-Tastyworks-Challenge-Token")
                    if challenge_tok:
                        _trigger_device_challenge(challenge_tok, is_test)
                        st.session_state.otp_needed = True
                        st.session_state.challenge_token = challenge_tok
                        st.session_state._login_user = username
                        st.session_state._login_pass = password
                        st.session_state._login_test = is_test
                        st.rerun()
                    else:
                        st.error(f"Login failed ({r.status_code}). Check credentials.")
                else:
                    st.error(f"Login failed ({r.status_code}).")
    else:
        st.info("📱 A 6-digit code was sent to your phone. Enter it below.")
        with st.form("otp_form"):
            otp_code = st.text_input("SMS Code", max_chars=6)
            otp_sub  = st.form_submit_button("Verify & Connect", use_container_width=True)
            if otp_sub and otp_code:
                with st.spinner("Verifying..."):
                    r = _do_login(
                        st.session_state._login_user, st.session_state._login_pass,
                        st.session_state._login_test, otp=otp_code,
                        challenge_token=st.session_state.challenge_token
                    )
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    st.session_state.session_token = d.get("session-token")
                    dx_url, dx_token = _get_quote_token(st.session_state.session_token, st.session_state._login_test)
                    st.session_state.dxlink_url, st.session_state.auth_token = dx_url, dx_token
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error(f"Invalid code ({r.status_code}). Try again.")
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
    
    running = _get("running")
    if running:
        st.success("🔴 Stream Active")
    
    err = _get("error")
    if err:
        st.error(f"Stream error: {err}")

if start_btn and not _get("running"):
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token,
                  symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold)
if stop_btn:
    _set(running=False)
if not _get("running") and not st.session_state._auto_started and st.session_state.dxlink_url:
    st.session_state._auto_started = True
    _start_stream(st.session_state.dxlink_url, st.session_state.auth_token,
                  symbol, tick_symbol, add_symbol, vol_sma_period, tick_threshold, add_threshold)

# ── Read live data from shared dict ──────────────────────────────────────────
with _lock:
    tick = _data["tick_val"]
    add  = _data["add_val"]
    vol  = _data["vol_val"]
    sma  = _data["sma_val"]
    surge = _data["vol_surge"]
    sig   = _data["signal"]
    price = _data["price"]
    last_upd = _data["last_update"]

# ── Header ───────────────────────────────────────────────────────────────────
now_et = datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York'))
live_dot = '<span style="color:#0066ff;font-size:16px;">&#9679; LIVE</span>' if _get("running") else '<span style="color:#94a3b8;">&#9679; OFFLINE</span>'
age = f" | Updated {int(time.time()-last_upd)}s ago" if last_upd else ""

st.markdown(f"""
<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0 20px;">
  <div style="font-family:'Orbitron',sans-serif;font-size:24px;font-weight:900;color:#0066ff;letter-spacing:3px;">
    🚀 CORE PRESSURE SCANNER
  </div>
  <div style="font-size:13px;color:#555;">
    {live_dot}&nbsp;&nbsp;{now_et.strftime('%I:%M:%S %p ET')}{age}
  </div>
</div>
""", unsafe_allow_html=True)

# ── Signal Banner ─────────────────────────────────────────────────────────────
if sig == "LONG":
    st.markdown(f"""<div class="signal-long">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🔵 GO LONG 🔵</div>
    <div style="font-size:18px;opacity:0.9;margin-top:10px;">
      Volume Surge ✅ &nbsp;|&nbsp; TICK {tick:+.0f} > +{tick_threshold} ✅ &nbsp;|&nbsp; ADD {add:+.0f} > +{add_threshold} ✅
    </div></div>""", unsafe_allow_html=True)
elif sig == "SHORT":
    st.markdown(f"""<div class="signal-short">
    <div style="font-size:52px;font-weight:900;letter-spacing:4px;">🟡 GO SHORT 🟡</div>
    <div style="font-size:18px;opacity:0.9;margin-top:10px;">
      Volume Surge ✅ &nbsp;|&nbsp; TICK {tick:+.0f} < -{tick_threshold} ✅ &nbsp;|&nbsp; ADD {add:+.0f} < -{add_threshold} ✅
    </div></div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div class="signal-wait">
    <div style="font-size:32px;font-weight:700;letter-spacing:3px;">WAITING FOR SETUP</div>
    <div style="font-size:14px;margin-top:8px;">Monitoring Volume Surge + TICK + ADD</div>
    </div>""", unsafe_allow_html=True)

# ── Three Core Metric Cards ───────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)

tc = "#0066ff" if tick > tick_threshold else "#ffd700" if tick < -tick_threshold else "#334155"
ac = "#0066ff" if add > add_threshold  else "#ffd700" if add < -add_threshold  else "#334155"
vc = "#0066ff" if surge else "#334155"

tick_lbl = "BULLISH ▲" if tick > tick_threshold else "BEARISH ▼" if tick < -tick_threshold else "NEUTRAL"
add_lbl  = "ADVANCING ▲" if add > add_threshold  else "DECLINING ▼" if add < -add_threshold  else "NEUTRAL"
vol_lbl  = "SURGE DETECTED ▲" if surge else f"Avg: {sma/1000:,.0f}k"

with m1:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $TICK — Momentum</div>
    <div class="metric-value" style="color:{tc};">{tick:+.0f}</div>
    <div class="metric-sub" style="color:{tc};font-weight:700;">{tick_lbl}</div>
    <div class="metric-sub">Threshold ±{tick_threshold}</div>
    </div>""", unsafe_allow_html=True)

with m2:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $ADD — Breadth</div>
    <div class="metric-value" style="color:{ac};">{add:+.0f}</div>
    <div class="metric-sub" style="color:{ac};font-weight:700;">{add_lbl}</div>
    <div class="metric-sub">Threshold ±{add_threshold}</div>
    </div>""", unsafe_allow_html=True)

with m3:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">{symbol} 5m Volume</div>
    <div class="metric-value" style="color:{vc};">{vol/1000:,.0f}k</div>
    <div class="metric-sub" style="color:{vc};font-weight:700;">{vol_lbl}</div>
    <div class="metric-sub">Price: ${price:,.2f}</div>
    </div>""", unsafe_allow_html=True)

# ── Step Checklist ────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
c1, c2, c3 = st.columns(3)

def _step(passed, label, detail):
    bg = "#dbeafe" if passed else "#fef9c3"
    border = "#3b82f6" if passed else "#fbbf24"
    icon = "✅" if passed else "❌"
    return f"""<div style="background:{bg};border-left:4px solid {border};
      border-radius:8px;padding:14px 16px;margin:6px 0;">
      <b style="font-size:14px;">{icon} {label}</b>
      <div style="font-size:11px;color:#475569;margin-top:4px;">{detail}</div>
    </div>"""

with c1:
    st.markdown("<h4 style='color:#0066ff;font-size:13px;letter-spacing:2px;'>STEP 1 — VOLUME SURGE</h4>", unsafe_allow_html=True)
    st.markdown(_step(surge, "Volume Above SMA", f"Current: {vol/1000:,.0f}k | SMA: {sma/1000:,.0f}k"), unsafe_allow_html=True)

with c2:
    st.markdown("<h4 style='color:#0066ff;font-size:13px;letter-spacing:2px;'>STEP 2 — NYSE $TICK</h4>", unsafe_allow_html=True)
    st.markdown(_step(tick > tick_threshold, f"TICK > +{tick_threshold} (Long)", f"Current: {tick:+.0f}"), unsafe_allow_html=True)
    st.markdown(_step(tick < -tick_threshold, f"TICK < -{tick_threshold} (Short)", f"Current: {tick:+.0f}"), unsafe_allow_html=True)

with c3:
    st.markdown("<h4 style='color:#0066ff;font-size:13px;letter-spacing:2px;'>STEP 3 — NYSE $ADD</h4>", unsafe_allow_html=True)
    st.markdown(_step(add > add_threshold, f"ADD > +{add_threshold} (Long)", f"Current: {add:+.0f}"), unsafe_allow_html=True)
    st.markdown(_step(add < -add_threshold, f"ADD < -{add_threshold} (Short)", f"Current: {add:+.0f}"), unsafe_allow_html=True)

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="text-align:center;color:#94a3b8;font-size:10px;padding:20px 0 4px;
  border-top:1px solid #e2e8f0;margin-top:20px;">
3-STEP PRESSURE METHOD SCANNER &nbsp;·&nbsp; TASTYTRADE OPEN API + DXFEED DXLINK WEBSOCKET
&nbsp;·&nbsp; NOT FINANCIAL ADVICE
</div>""", unsafe_allow_html=True)

# ── Auto-refresh every 1.5 seconds while stream is running ───────────────────
if _get("running"):
    time.sleep(1.5)
    st.rerun()
