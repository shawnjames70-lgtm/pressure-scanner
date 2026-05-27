"""
3-Step Pressure Method Scanner
================================
Live Tastytrade / dxFeed WebSocket-powered Streamlit dashboard.

Deployment: Railway (or any cloud platform)
Auth: Tastytrade username/password entered in the UI sidebar.
      Credentials are NEVER stored server-side — only held in Streamlit session state.

3-Step Pressure Method:
  Step 1 – Volume Surge  : Current 5-min candle volume > 20-period Volume SMA
  Step 2 – Shaved Candle :
      Buy  → close in top 10% of candle range (shaved top)
      Sell → close in bottom 10% of candle range (shaved bottom)
  Step 3 – NYSE TICK     :
      Buy  → $TICK > +800
      Sell → $TICK < -800
"""

import streamlit as st
import pandas as pd
import asyncio
import threading
import time
import json
import os
import requests as _req
from collections import deque

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="3-Step Pressure Method",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html,body,[data-testid="stAppViewContainer"]{
  font-family:'Courier New',monospace;background:#0d1117;}
[data-testid="stSidebar"]{background:#0d1117;color:#c9d1d9;}
[data-testid="stSidebar"] label{color:#c9d1d9 !important;}
h1,h2,h3,h4{color:#e6edf3;}
.signal-long{
  background:linear-gradient(135deg,#00ff00 0%,#00cc00 100%);
  color:#000;padding:50px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 80px #00ff0099;animation:pg 1s infinite alternate;margin:10px 0;}
.signal-short{
  background:linear-gradient(135deg,#ff0000 0%,#cc0000 100%);
  color:#fff;padding:50px 20px;border-radius:20px;text-align:center;
  box-shadow:0 0 80px #ff000099;animation:pr 1s infinite alternate;margin:10px 0;}
.signal-wait{
  background:#1c1c2e;color:#888;padding:50px 20px;
  border-radius:20px;text-align:center;border:2px solid #333;margin:10px 0;}
@keyframes pg{from{box-shadow:0 0 40px #00ff0066}to{box-shadow:0 0 100px #00ff00cc}}
@keyframes pr{from{box-shadow:0 0 40px #ff000066}to{box-shadow:0 0 100px #ff0000cc}}
.step-pass{
  background:#0d2b0d;border-left:6px solid #00ff00;
  padding:14px 18px;border-radius:8px;margin:6px 0;
  color:#00ff00;font-weight:bold;font-family:'Courier New',monospace;}
.step-fail{
  background:#2b0d0d;border-left:6px solid #ff4444;
  padding:14px 18px;border-radius:8px;margin:6px 0;
  color:#ff4444;font-weight:bold;font-family:'Courier New',monospace;}
.metric-card{
  background:#161b22;border:1px solid #30363d;border-radius:14px;
  padding:22px;text-align:center;margin:4px;}
.metric-label{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:2px;}
.metric-value{font-size:38px;font-weight:bold;color:#e6edf3;margin:10px 0 4px;}
.dpos{color:#3fb950;font-size:13px;}
.dneg{color:#f85149;font-size:13px;}
.login-box{
  background:#161b22;border:1px solid #30363d;border-radius:16px;
  padding:40px;max-width:480px;margin:60px auto;}
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
        # Auth state
        authenticated=False,
        session_token="",
        dxlink_url="",
        auth_token="",
        otp_needed=False,
        challenge_token="",
        login_error="",
        # Stream state
        running=False,
        status_msg="Enter your Tastytrade credentials to start the scanner.",
        status_type="info",
        candles=deque(maxlen=100),
        tick_val=0.0, price=0.0, volume=0.0, sma_vol=0.0,
        shaved_top=False, shaved_bottom=False,
        step1=False, step2_buy=False, step2_sell=False,
        step3_buy=False, step3_sell=False,
        signal="WAIT", last_update=None,
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
    if challenge_token:
        hdrs["X-Tastyworks-Challenge-Token"] = challenge_token
    if otp:
        hdrs["X-Tastyworks-OTP"] = otp
    r = _req.post(f"{base}/sessions",
                  headers=hdrs,
                  json={"login": username, "password": password, "remember-me": True},
                  timeout=15)
    return r

def _get_quote_token(session_token, is_test):
    base = TT_CERT if is_test else TT_API
    r = _req.get(f"{base}/api-quote-tokens",
                 headers={"Authorization": session_token},
                 timeout=15)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Quote token error {r.status_code}: {r.text}")
    d = r.json().get("data", r.json())
    return d.get("dxlink-url"), d.get("token")

# ─────────────────────────────────────────────────────────────────────────────
# 3-Step logic
# ─────────────────────────────────────────────────────────────────────────────
def _compute(candles_list, tick_val, sma_period, tick_thr):
    if len(candles_list) < 2:
        return {}
    df = pd.DataFrame(candles_list)
    df["vol_sma"] = df["volume"].rolling(min(sma_period, len(df))).mean()
    c = df.iloc[-1]
    price  = float(c["close"])
    volume = float(c["volume"])
    sma_v  = float(c["vol_sma"]) if not pd.isna(c["vol_sma"]) else 0.0
    rng    = float(c["high"]) - float(c["low"])
    st_    = (price >= float(c["high"]) - rng * 0.10) if rng > 0 else False
    sb_    = (price <= float(c["low"])  + rng * 0.10) if rng > 0 else False
    s1     = volume > sma_v and sma_v > 0
    s2b    = st_; s2s = sb_
    s3b    = tick_val > tick_thr; s3s = tick_val < -tick_thr
    sig    = "LONG"  if (s1 and s2b and s3b) else \
             "SHORT" if (s1 and s2s and s3s) else "WAIT"
    return dict(price=price, volume=volume, sma_vol=sma_v,
                shaved_top=st_, shaved_bottom=sb_,
                step1=s1, step2_buy=s2b, step2_sell=s2s,
                step3_buy=s3b, step3_sell=s3s, signal=sig)

# ─────────────────────────────────────────────────────────────────────────────
# FEED_DATA parser — dxFeed LIST format (list of dicts)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_feed(channel, data, candle_ch, tick_ch, sma_period, tick_thr):
    if not isinstance(data, list):
        return
    for evt in data:
        if not isinstance(evt, dict):
            continue
        etype = evt.get("eventType", "")

        if etype == "Candle" and channel == candle_ch:
            try:
                close  = evt.get("close",  0)
                high   = evt.get("high",   0)
                low    = evt.get("low",    0)
                volume = evt.get("volume", 0)
                if str(close) in ("NaN","nan","0","") or float(close) <= 0:
                    continue
                row = dict(
                    time  =evt.get("time", 0),
                    open  =float(evt.get("open", close) or close),
                    high  =float(high  or close),
                    low   =float(low   or close),
                    close =float(close),
                    volume=float(volume or 0),
                )
                cl = list(st.session_state.candles)
                if cl and cl[-1]["time"] == row["time"]:
                    cl[-1] = row
                    st.session_state.candles = deque(cl, maxlen=100)
                else:
                    st.session_state.candles.append(row)
                st.session_state.candle_count += 1
                res = _compute(list(st.session_state.candles),
                               st.session_state.tick_val, sma_period, tick_thr)
                if res:
                    for k, v in res.items():
                        st.session_state[k] = v
                    st.session_state.last_update = time.strftime("%H:%M:%S")
            except Exception:
                pass

        elif etype == "Trade" and channel == tick_ch:
            try:
                price = evt.get("price")
                if price is None or str(price) in ("NaN","nan"):
                    continue
                tv = float(price)
                st.session_state.tick_val   = tv
                st.session_state.tick_count += 1
                s3b = tv > tick_thr; s3s = tv < -tick_thr
                st.session_state.step3_buy  = s3b
                st.session_state.step3_sell = s3s
                s1  = st.session_state.step1
                s2b = st.session_state.step2_buy
                s2s = st.session_state.step2_sell
                if s1 and s2b and s3b:   st.session_state.signal = "LONG"
                elif s1 and s2s and s3s: st.session_state.signal = "SHORT"
                else:                    st.session_state.signal = "WAIT"
                st.session_state.last_update = time.strftime("%H:%M:%S")
            except Exception:
                pass

        elif etype == "Quote" and channel == tick_ch:
            try:
                bid = evt.get("bidPrice")
                if bid is not None and str(bid) not in ("NaN","nan","0"):
                    st.session_state.tick_val   = float(bid)
                    st.session_state.tick_count += 1
                    st.session_state.last_update = time.strftime("%H:%M:%S")
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# Async WebSocket stream
# ─────────────────────────────────────────────────────────────────────────────
async def _stream(dxlink_url, auth_token, symbol, tick_symbol, sma_period, tick_thr):
    from httpx import AsyncClient
    from httpx_ws import aconnect_ws
    from ssl import create_default_context
    from datetime import datetime, timezone

    ssl_ctx   = create_default_context()
    CANDLE_CH = 1
    TICK_CH   = 3

    async with AsyncClient(verify=ssl_ctx) as client:
        async with aconnect_ws(
            dxlink_url, client=client,
            keepalive_ping_interval_seconds=None
        ) as ws:

            async def send(msg):
                await ws.send_text(json.dumps(msg))

            await send({"type":"SETUP","channel":0,
                        "version":"0.1-DXF-JS/0.3.0",
                        "minVersion":"0.1-DXF-JS/0.3.0",
                        "keepaliveTimeout":60,"acceptKeepaliveTimeout":60})
            await send({"type":"AUTH","channel":0,"token":auth_token})

            for _ in range(10):
                raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("type") == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                    break

            await send({"type":"CHANNEL_REQUEST","channel":CANDLE_CH,
                        "service":"FEED","parameters":{"contract":"AUTO"}})
            await send({"type":"CHANNEL_REQUEST","channel":TICK_CH,
                        "service":"FEED","parameters":{"contract":"AUTO"}})

            opened = set()
            for _ in range(10):
                raw = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("type") == "CHANNEL_OPENED":
                    opened.add(msg.get("channel"))
                if CANDLE_CH in opened and TICK_CH in opened:
                    break

            from_time = int(datetime(2024,1,1,tzinfo=timezone.utc).timestamp()*1000)
            await send({"type":"FEED_SUBSCRIPTION","channel":CANDLE_CH,
                        "add":[{"type":"Candle",
                                "symbol":f"{symbol}{{=5m}}",
                                "fromTime":from_time}]})
            await send({"type":"FEED_SUBSCRIPTION","channel":TICK_CH,
                        "add":[{"type":"Trade","symbol":tick_symbol},
                               {"type":"Quote","symbol":tick_symbol}]})

            st.session_state.status_msg  = (
                f"🔴 LIVE — {symbol} 5-min candles | {tick_symbol} TICK | "
                f"dxFeed WebSocket connected"
            )
            st.session_state.status_type = "success"

            while st.session_state.running:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=3.0)
                except asyncio.TimeoutError:
                    await send({"type":"KEEPALIVE","channel":0})
                    continue
                msg   = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "FEED_DATA":
                    _parse_feed(msg.get("channel"), msg.get("data",[]),
                                CANDLE_CH, TICK_CH, sma_period, tick_thr)
                elif mtype == "KEEPALIVE":
                    await send({"type":"KEEPALIVE","channel":0})
                elif mtype == "AUTH_STATE" and msg.get("state") == "UNAUTHORIZED":
                    raise RuntimeError("dxFeed auth rejected — token expired.")


def _thread(dxlink_url, auth_token, symbol, tick_symbol, sma_period, tick_thr):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _stream(dxlink_url, auth_token, symbol, tick_symbol,
                    sma_period, tick_thr)
        )
    except Exception as e:
        st.session_state.status_msg  = f"❌ Stream error: {e}"
        st.session_state.status_type = "error"
        st.session_state.running     = False
    finally:
        loop.close()

def _start_stream(dxlink_url, auth_token, symbol, tick_symbol, sma_period, tick_thr):
    st.session_state.running      = True
    st.session_state.candle_count = 0
    st.session_state.tick_count   = 0
    t = threading.Thread(
        target=_thread,
        args=(dxlink_url, auth_token, symbol, tick_symbol, sma_period, tick_thr),
        daemon=True,
    )
    t.start()

# ─────────────────────────────────────────────────────────────────────────────
# LOGIN SCREEN (shown when not authenticated)
# ─────────────────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    st.markdown("# 🚀 3-Step Pressure Method Scanner")
    st.markdown("---")

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("""<div class="login-box">
        <h2 style="text-align:center;color:#e6edf3;margin-bottom:24px;">
          🔐 Tastytrade Login</h2>
        </div>""", unsafe_allow_html=True)

        with st.form("login_form"):
            username  = st.text_input("Username / Email", placeholder="your@email.com")
            password  = st.text_input("Password", type="password")
            is_test   = st.checkbox("Use Sandbox / Cert Environment", value=False)
            submitted = st.form_submit_button("Connect to Live Stream", type="primary",
                                              use_container_width=True)

        if st.session_state.otp_needed:
            st.warning("📱 2FA required — a code was sent to your phone.")
            with st.form("otp_form"):
                otp_code = st.text_input("Enter OTP Code", max_chars=8)
                otp_sub  = st.form_submit_button("Verify & Connect", type="primary",
                                                  use_container_width=True)
            if otp_sub and otp_code:
                r = _do_login(
                    st.session_state._login_user,
                    st.session_state._login_pass,
                    st.session_state._login_test,
                    otp=otp_code,
                    challenge_token=st.session_state.challenge_token
                )
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    session_token = d.get("session-token","")
                    dxlink_url, auth_token = _get_quote_token(
                        session_token, st.session_state._login_test)
                    st.session_state.session_token  = session_token
                    st.session_state.dxlink_url     = dxlink_url
                    st.session_state.auth_token     = auth_token
                    st.session_state.authenticated  = True
                    st.session_state.otp_needed     = False
                    st.rerun()
                else:
                    st.error(f"OTP failed: {r.json().get('error',{}).get('message','Unknown error')}")
        else:
            otp_sub = False

        if submitted and username and password:
            with st.spinner("Connecting to Tastytrade..."):
                r = _do_login(username, password, is_test)
                if r.status_code in (200, 201):
                    d = r.json().get("data", r.json())
                    session_token = d.get("session-token","")
                    try:
                        dxlink_url, auth_token = _get_quote_token(session_token, is_test)
                        st.session_state.session_token = session_token
                        st.session_state.dxlink_url    = dxlink_url
                        st.session_state.auth_token    = auth_token
                        st.session_state.authenticated = True
                        st.rerun()
                    except Exception as e:
                        st.error(f"Stream token error: {e}")
                elif r.status_code == 403:
                    err = r.json().get("error", {}).get("code","")
                    if err == "device_challenge_required":
                        st.session_state.otp_needed      = True
                        st.session_state.challenge_token = r.headers.get(
                            "X-Tastyworks-Challenge-Token","")
                        st.session_state._login_user = username
                        st.session_state._login_pass = password
                        st.session_state._login_test = is_test
                        st.rerun()
                    else:
                        st.error(f"Login error: {r.json().get('error',{}).get('message','Unknown error')}")
                else:
                    st.error(f"Login failed ({r.status_code}): check your credentials.")

        elif submitted:
            st.warning("Please enter both username and password.")

    st.markdown("""
    <div style="text-align:center;color:#444;font-size:12px;margin-top:40px;">
    3-Step Pressure Method Scanner · Tastytrade Open API + dxFeed DXLink WebSocket<br>
    Credentials are used only to authenticate with Tastytrade and are never stored.
    <br><b>Not financial advice. For educational purposes only.</b>
    </div>""", unsafe_allow_html=True)
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN DASHBOARD (shown after authentication)
# ─────────────────────────────────────────────────────────────────────────────

# Sidebar
with st.sidebar:
    st.markdown("## ⚙️ Scanner Settings")
    st.divider()
    symbol         = st.text_input("Symbol to Scan",    value="SPY").upper()
    tick_symbol    = st.text_input("TICK Index Symbol", value="$TICK").upper()
    vol_sma_period = st.slider("Volume SMA Period",  5,  50, 20)
    tick_threshold = st.slider("TICK Threshold (±)", 500, 1200, 800)
    st.divider()
    cs, cx = st.columns(2)
    start_btn = cs.button("▶ Start", type="primary",   use_container_width=True)
    stop_btn  = cx.button("⏹ Stop",  type="secondary", use_container_width=True)
    logout_btn = st.button("🚪 Logout", use_container_width=True)
    st.divider()
    st.markdown(f"""
**3-Step Pressure Method**

`Step 1` · Volume > {vol_sma_period}-period SMA

`Step 2` · Shaved top/bottom (10%)

`Step 3` · NYSE TICK ±{tick_threshold} confirm
""")
    if st.session_state.running:
        st.success("🔴 Stream Active")
        st.caption(f"Candles: {st.session_state.candle_count}")
        st.caption(f"TICK updates: {st.session_state.tick_count}")

# Button actions
if start_btn and not st.session_state.running:
    _start_stream(
        st.session_state.dxlink_url,
        st.session_state.auth_token,
        symbol, tick_symbol, vol_sma_period, tick_threshold
    )

if stop_btn:
    st.session_state.running     = False
    st.session_state.status_msg  = "⏹ Scanner stopped. Press ▶ Start to resume."
    st.session_state.status_type = "warning"

if logout_btn:
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()

# Auto-start on first load after auth
if (not st.session_state.running and
        not st.session_state.get("_auto_started") and
        st.session_state.dxlink_url and
        st.session_state.auth_token):
    st.session_state._auto_started = True
    _start_stream(
        st.session_state.dxlink_url,
        st.session_state.auth_token,
        symbol, tick_symbol, vol_sma_period, tick_threshold
    )

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("# 🚀 3-Step Pressure Method — Live Scanner")

stype = st.session_state.status_type
if   stype == "success": st.success(st.session_state.status_msg)
elif stype == "error":   st.error(st.session_state.status_msg)
elif stype == "warning": st.warning(st.session_state.status_msg)
else:                    st.info(st.session_state.status_msg)

st.divider()

# ── Signal Banner ─────────────────────────────────────────────────────────────
sig = st.session_state.signal
if sig == "LONG":
    st.markdown(f"""<div class="signal-long">
    <div style="font-size:72px;font-weight:900;">🟢 ALL-GREEN GO LONG 🟢</div>
    <div style="font-size:20px;margin-top:12px;opacity:0.85;">
      Volume Surge ✅ &nbsp;·&nbsp; Shaved Top ✅ &nbsp;·&nbsp;
      TICK &gt; +{tick_threshold} ✅
    </div></div>""", unsafe_allow_html=True)
elif sig == "SHORT":
    st.markdown(f"""<div class="signal-short">
    <div style="font-size:72px;font-weight:900;">🔴 ALL-RED GO SHORT 🔴</div>
    <div style="font-size:20px;margin-top:12px;opacity:0.85;">
      Volume Surge ✅ &nbsp;·&nbsp; Shaved Bottom ✅ &nbsp;·&nbsp;
      TICK &lt; -{tick_threshold} ✅
    </div></div>""", unsafe_allow_html=True)
else:
    st.markdown("""<div class="signal-wait">
    <div style="font-size:44px;font-weight:700;">⏳ Waiting for Setup...</div>
    <div style="font-size:16px;margin-top:10px;color:#666;">
      No confirmed signal yet. Monitor the 3-Step checklist below.
    </div></div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Live Metrics ──────────────────────────────────────────────────────────────
price  = st.session_state.price
volume = st.session_state.volume
sma_v  = st.session_state.sma_vol
tick   = st.session_state.tick_val
vd     = volume - sma_v
vdc    = "dpos" if vd >= 0 else "dneg"
vds    = f"{'▲' if vd>=0 else '▼'} {abs(vd):,.0f} vs SMA"
tc     = ("#3fb950" if tick > tick_threshold
          else "#f85149" if tick < -tick_threshold
          else "#e6edf3")

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">{symbol} Last Price</div>
    <div class="metric-value">${price:,.2f}</div>
    </div>""", unsafe_allow_html=True)
with c2:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">5-Min Volume</div>
    <div class="metric-value">{volume:,.0f}</div>
    <div class="{vdc}">{vds}</div>
    </div>""", unsafe_allow_html=True)
with c3:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">Vol SMA ({vol_sma_period})</div>
    <div class="metric-value">{sma_v:,.0f}</div>
    </div>""", unsafe_allow_html=True)
with c4:
    st.markdown(f"""<div class="metric-card">
    <div class="metric-label">NYSE $TICK</div>
    <div class="metric-value" style="color:{tc};">{tick:+.0f}</div>
    <div style="font-size:12px;color:#555;">
      {"🟢 BULLISH" if tick > tick_threshold else "🔴 BEARISH" if tick < -tick_threshold else "⚪ NEUTRAL"}
    </div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── 3-Step Checklist ──────────────────────────────────────────────────────────
st.markdown("### 📋 3-Step Pressure Checklist")
bc, sc = st.columns(2)

def _card(cls, icon, title, detail):
    return (f'<div class="{cls}">{icon} <b>{title}</b><br>'
            f'<span style="font-weight:normal;font-size:13px;">{detail}</span></div>')

i1  = "✅" if st.session_state.step1      else "❌"
i2b = "✅" if st.session_state.step2_buy  else "❌"
i2s = "✅" if st.session_state.step2_sell else "❌"
i3b = "✅" if st.session_state.step3_buy  else "❌"
i3s = "✅" if st.session_state.step3_sell else "❌"
c1_ = "step-pass" if st.session_state.step1      else "step-fail"
c2b = "step-pass" if st.session_state.step2_buy  else "step-fail"
c2s = "step-pass" if st.session_state.step2_sell else "step-fail"
c3b = "step-pass" if st.session_state.step3_buy  else "step-fail"
c3s = "step-pass" if st.session_state.step3_sell else "step-fail"

with bc:
    st.markdown("#### 🟢 BUY Conditions")
    st.markdown(_card(c1_, i1, "Step 1 — Volume Surge",
        f"Current vol: {volume:,.0f} &nbsp;|&nbsp; SMA({vol_sma_period}): {sma_v:,.0f}"),
        unsafe_allow_html=True)
    st.markdown(_card(c2b, i2b, "Step 2 — Shaved Top (Buy Pressure)",
        "Close in top 10% of 5-min candle range"), unsafe_allow_html=True)
    st.markdown(_card(c3b, i3b, f"Step 3 — TICK Bullish (> +{tick_threshold})",
        f"Current $TICK: {tick:+.0f}"), unsafe_allow_html=True)

with sc:
    st.markdown("#### 🔴 SELL Conditions")
    st.markdown(_card(c1_, i1, "Step 1 — Volume Surge",
        f"Current vol: {volume:,.0f} &nbsp;|&nbsp; SMA({vol_sma_period}): {sma_v:,.0f}"),
        unsafe_allow_html=True)
    st.markdown(_card(c2s, i2s, "Step 2 — Shaved Bottom (Sell Pressure)",
        "Close in bottom 10% of 5-min candle range"), unsafe_allow_html=True)
    st.markdown(_card(c3s, i3s, f"Step 3 — TICK Bearish (< -{tick_threshold})",
        f"Current $TICK: {tick:+.0f}"), unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Candle History Table ──────────────────────────────────────────────────────
cl = list(st.session_state.candles)
if cl:
    st.markdown("### 📊 Recent 5-Minute Candles")
    df = pd.DataFrame(cl).tail(15).copy()
    df["Time"] = pd.to_datetime(df["time"], unit="ms").dt.strftime("%H:%M")
    df = df[["Time","open","high","low","close","volume"]].copy()
    df.columns = ["Time","Open","High","Low","Close","Volume"]
    df["Volume"] = df["Volume"].apply(lambda x: f"{x:,.0f}")
    for col in ["Open","High","Low","Close"]:
        df[col] = df[col].apply(lambda x: f"${x:.2f}")
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("Waiting for candle data... ($TICK streams only during NYSE market hours 9:30–16:00 ET)")

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
lu = st.session_state.last_update
if lu:
    st.caption(f"Last update: {lu} · Candles: {st.session_state.candle_count} · "
               f"TICK updates: {st.session_state.tick_count}")

if st.session_state.running:
    st.caption("🔴 **LIVE** — auto-refreshing every 2 seconds")
    time.sleep(2)
    st.rerun()
else:
    st.caption("⚫ Stopped — press ▶ Start to begin streaming")

st.markdown("""<div style="text-align:center;color:#444;font-size:11px;padding:10px 0;">
3-Step Pressure Method Scanner · Tastytrade Open API + dxFeed DXLink WebSocket<br>
<b>Not financial advice. For educational purposes only.</b>
</div>""", unsafe_allow_html=True)
