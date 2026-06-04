import streamlit as st
import pandas as pd
import pandas_ta as ta
import requests
import time
import threading
from datetime import datetime
import json
import websocket
import plotly.graph_objects as go

# ==========================================
# State Management
# ==========================================
class BotState:
    def __init__(self):
        self.is_running = False
        self.webhook_url = ""
        self.ema_period = 100
        self.rsi_buy = 60
        self.rsi_sell = 40
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        self.atr_sl = 1.5
        self.atr_tp = 3.0
        self.logs = []
        self.last_trade_time = None
        self.current_signal = "⚪ [MONITORING] - Waiting for setup..."
        self.signal_expiry_time = None

@st.cache_resource
def get_global_state():
    return BotState()

bot_state = get_global_state()

# Global in-memory lists for threads to share (since st.session_state is tied to sessions)
@st.cache_resource
def get_tick_history():
    # Store up to 200 ticks
    return []

gmo_ticks = get_tick_history()

# ==========================================
# Core Logic & Notifications
# ==========================================
def send_webhook(webhook_url, message):
    if webhook_url:
        try:
            requests.post(webhook_url, json={"content": message})
        except Exception as e:
            print(f"Webhook error: {e}")

def compute_indicators_and_signals(df, state):
    if len(df) < state.ema_period + 1:
        df['BUY_SIGNAL'] = False
        df['SELL_SIGNAL'] = False
        return df

    df['EMA'] = ta.ema(df['close'], length=state.ema_period)
    macd = ta.macd(df['close'], fast=state.macd_fast, slow=state.macd_slow, signal=state.macd_signal)
    if macd is not None and not macd.empty:
        df['MACD'] = macd.iloc[:, 0]
        df['MACD_Signal'] = macd.iloc[:, 2]

    df['RSI'] = ta.rsi(df['close'], length=14)
    # Using close for high/low proxy in tick data to compute ATR approximation
    df['ATR'] = ta.atr(df['close'], df['close'], df['close'], length=14)

    df['BUY_SIGNAL'] = False
    df['SELL_SIGNAL'] = False

    for i in range(1, len(df)):
        prev = df.iloc[i-1]
        curr = df.iloc[i]

        if pd.isna(curr['EMA']) or pd.isna(curr['ATR']) or pd.isna(curr['RSI']) or 'MACD' not in df.columns:
            continue

        macd_crossed_above = prev['MACD'] <= prev['MACD_Signal'] and curr['MACD'] > curr['MACD_Signal']
        macd_crossed_below = prev['MACD'] >= prev['MACD_Signal'] and curr['MACD'] < curr['MACD_Signal']

        if curr['close'] > curr['EMA'] and curr['RSI'] < state.rsi_buy and macd_crossed_above:
            df.at[df.index[i], 'BUY_SIGNAL'] = True
        elif curr['close'] < curr['EMA'] and curr['RSI'] > state.rsi_sell and macd_crossed_below:
            df.at[df.index[i], 'SELL_SIGNAL'] = True

    return df

# ==========================================
# WebSocket Background Daemon
# ==========================================
def on_message(ws, message):
    try:
        data = json.loads(message)
        if "ask" in data and "bid" in data:
            # We use the mid price
            ask = float(data["ask"])
            bid = float(data["bid"])
            mid_price = (ask + bid) / 2.0

            # Timestamp from GMO or local time
            timestamp = data.get("timestamp")
            if timestamp:
                # GMO timestamp is ISO 8601 (e.g., 2023-01-01T12:00:00.000Z)
                # But we just use current local time for simplicity on chart
                t_val = pd.to_datetime(timestamp).tz_localize(None)
            else:
                t_val = datetime.now()

            tick_data = {
                'time': t_val,
                'open': mid_price,
                'high': mid_price,
                'low': mid_price,
                'close': mid_price,
                'price': mid_price
            }

            # We can aggregate into 5-second candles or just append ticks.
            # Here we append ticks directly.
            gmo_ticks.append(tick_data)

            if len(gmo_ticks) > 500:
                gmo_ticks.pop(0)

            # Signal check on new tick
            if bot_state.is_running and len(gmo_ticks) >= bot_state.ema_period + 1:
                df = pd.DataFrame(gmo_ticks)
                df = compute_indicators_and_signals(df, bot_state)
                latest = df.iloc[-1]

                if not (pd.isna(latest['EMA']) or pd.isna(latest['ATR']) or pd.isna(latest['RSI'])):
                    if latest['time'] != bot_state.last_trade_time:
                        if latest['BUY_SIGNAL']:
                            bot_state.current_signal = f"🟢 [SIGNAL ACTIVE] - BUY Alert Triggered @ {latest['price']:.3f}!"
                            bot_state.signal_expiry_time = datetime.now()
                            bot_state.last_trade_time = latest['time']

                            sl = latest['price'] - (bot_state.atr_sl * latest['ATR'])
                            tp = latest['price'] + (bot_state.atr_tp * latest['ATR'])

                            msg = f"🔥 [SIGNAL ALERT] USD/JPY BUY Signal triggered at {latest['price']:.3f} | SL: {sl:.3f} | TP: {tp:.3f}"
                            send_webhook(bot_state.webhook_url, msg)

                            log_entry = {
                                "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Action": "SIGNAL: BUY",
                                "Price": round(latest['price'], 3),
                                "SL": round(sl, 3),
                                "TP": round(tp, 3),
                                "Status": "Alert Sent"
                            }
                            bot_state.logs.insert(0, log_entry)

                        elif latest['SELL_SIGNAL']:
                            bot_state.current_signal = f"🔴 [SIGNAL ACTIVE] - SELL Alert Triggered @ {latest['price']:.3f}!"
                            bot_state.signal_expiry_time = datetime.now()
                            bot_state.last_trade_time = latest['time']

                            sl = latest['price'] + (bot_state.atr_sl * latest['ATR'])
                            tp = latest['price'] - (bot_state.atr_tp * latest['ATR'])

                            msg = f"🔥 [SIGNAL ALERT] USD/JPY SELL Signal triggered at {latest['price']:.3f} | SL: {sl:.3f} | TP: {tp:.3f}"
                            send_webhook(bot_state.webhook_url, msg)

                            log_entry = {
                                "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Action": "SIGNAL: SELL",
                                "Price": round(latest['price'], 3),
                                "SL": round(sl, 3),
                                "TP": round(tp, 3),
                                "Status": "Alert Sent"
                            }
                            bot_state.logs.insert(0, log_entry)
                        else:
                            # Only reset if 10 seconds have passed since the last signal
                            if bot_state.signal_expiry_time is None or (datetime.now() - bot_state.signal_expiry_time).total_seconds() > 10:
                                bot_state.current_signal = "⚪ [MONITORING] - Waiting for setup..."

    except Exception as e:
        print(f"WS Msg Error: {e}")

def on_error(ws, error):
    print(f"WebSocket Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print("WebSocket Closed. Reconnecting...")
    time.sleep(5)
    start_ws()

def on_open(ws):
    print("WebSocket Opened")
    req = {
        "command": "subscribe",
        "channel": "ticker",
        "symbol": "USD_JPY"
    }
    ws.send(json.dumps(req))

def run_ws():
    ws = websocket.WebSocketApp("wss://api.coin.zcom.jp/ws/public/v1",
                              on_open=on_open,
                              on_message=on_message,
                              on_error=on_error,
                              on_close=on_close)
    ws.run_forever()

def start_ws():
    wst = threading.Thread(target=run_ws, daemon=True)
    wst.start()

@st.cache_resource
def init_background_threads():
    start_ws()
    return True

init_background_threads()

# ==========================================
# Streamlit UI
# ==========================================
st.set_page_config(page_title="FX Trading Bot Dashboard", layout="wide")

st.sidebar.header("Configuration")
st.sidebar.info("GMO Coin Public WebSocket (Registration-Free)")
bot_state.webhook_url = st.sidebar.text_input("Webhook URL (Discord/Slack/LINE)", value=bot_state.webhook_url)

st.sidebar.header("Strategy Parameters")
bot_state.ema_period = st.sidebar.slider("EMA Period", min_value=10, max_value=150, value=bot_state.ema_period) # Reduced max for ticks
bot_state.rsi_buy = st.sidebar.slider("RSI Buy Max Level", 0, 100, bot_state.rsi_buy)
bot_state.rsi_sell = st.sidebar.slider("RSI Sell Min Level", 0, 100, bot_state.rsi_sell)

st.sidebar.subheader("MACD Settings")
bot_state.macd_fast = st.sidebar.number_input("MACD Fast Period", min_value=1, max_value=100, value=bot_state.macd_fast)
bot_state.macd_slow = st.sidebar.number_input("MACD Slow Period", min_value=1, max_value=200, value=bot_state.macd_slow)
bot_state.macd_signal = st.sidebar.number_input("MACD Signal Period", min_value=1, max_value=100, value=bot_state.macd_signal)

st.sidebar.subheader("Risk Management")
bot_state.atr_sl = st.sidebar.slider("ATR Stop Loss Multiplier", 0.5, 5.0, bot_state.atr_sl, step=0.1)
bot_state.atr_tp = st.sidebar.slider("ATR Take Profit Multiplier", 1.0, 10.0, bot_state.atr_tp, step=0.1)

st.title("FX Trading Bot - USD/JPY (Zero-Latency)")

is_on = st.toggle("Bot Status (ON/OFF) - Start Strategy Calculations", value=bot_state.is_running)
bot_state.is_running = is_on

st.subheader("Active Parameter Dashboard")
pcol1, pcol2, pcol3, pcol4 = st.columns(4)
with pcol1:
    st.metric("EMA Period", bot_state.ema_period)
with pcol2:
    st.metric("RSI Buy / Sell", f"{bot_state.rsi_buy} / {bot_state.rsi_sell}")
with pcol3:
    st.metric("MACD (F/S/Sig)", f"{bot_state.macd_fast}/{bot_state.macd_slow}/{bot_state.macd_signal}")
with pcol4:
    st.metric("ATR (SL / TP)", f"{bot_state.atr_sl}x / {bot_state.atr_tp}x")

st.markdown("---")

# Dynamic Fragment for Data and Chart rendering running every 2 seconds
@st.fragment(run_every="2s")
def render_dynamic_dashboard():
    # Full-width colored alert box
    if "BUY" in bot_state.current_signal:
        st.success(bot_state.current_signal)
    elif "SELL" in bot_state.current_signal:
        st.error(bot_state.current_signal)
    else:
        st.info(bot_state.current_signal)

    st.subheader("Live USD/JPY Chart")

    if len(gmo_ticks) > 0:
        # Create DataFrame from global list
        df_plot = pd.DataFrame(list(gmo_ticks))

        # Calculate indicators for chart if enough data
        if len(df_plot) >= bot_state.ema_period + 1:
            df_plot = compute_indicators_and_signals(df_plot, bot_state)

        # Convert time to string format to remove gaps on the x-axis
        df_plot['time_str'] = df_plot['time'].dt.strftime('%H:%M:%S')

        fig = go.Figure()

        # Line chart for sub-second ticks
        fig.add_trace(go.Scatter(
            x=df_plot['time_str'],
            y=df_plot['price'],
            mode='lines',
            line=dict(color='cyan', width=2),
            name="Spot Price"
        ))

        if 'EMA' in df_plot.columns:
            fig.add_trace(go.Scatter(
                x=df_plot['time_str'],
                y=df_plot['EMA'],
                mode='lines',
                line=dict(color='orange', width=2),
                name=f'{bot_state.ema_period} EMA'
            ))

        # Large Signals
        if 'BUY_SIGNAL' in df_plot.columns:
            buy_signals = df_plot[df_plot['BUY_SIGNAL'] == True]
            if not buy_signals.empty:
                # Add text annotations next to the markers
                fig.add_trace(go.Scatter(
                    x=buy_signals['time_str'],
                    y=buy_signals['price'], # Directly on line for ticks
                    mode='markers+text',
                    marker=dict(symbol='triangle-up', color='green', size=20),
                    text=["BUY"] * len(buy_signals),
                    textposition="bottom center",
                    textfont=dict(color="green", size=16, weight="bold"),
                    name='BUY Signal'
                ))

            sell_signals = df_plot[df_plot['SELL_SIGNAL'] == True]
            if not sell_signals.empty:
                fig.add_trace(go.Scatter(
                    x=sell_signals['time_str'],
                    y=sell_signals['price'],
                    mode='markers+text',
                    marker=dict(symbol='triangle-down', color='red', size=20),
                    text=["SELL"] * len(sell_signals),
                    textposition="top center",
                    textfont=dict(color="red", size=16, weight="bold"),
                    name='SELL Signal'
                ))

        fig.update_layout(
            title=f"GMO Coin Live Feed - {len(df_plot)} Ticks",
            yaxis_title="Price",
            xaxis_title="Time",
            template="plotly_dark",
            height=600,
            xaxis_rangeslider_visible=False,
            xaxis=dict(type='category', tickangle=-45) # Remove awkward time gaps
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Connecting to GMO Coin WebSocket and waiting for data... (This may take a few seconds)")

    st.subheader("Recent Trade Logs")
    log_df = pd.DataFrame(bot_state.logs, columns=["Time", "Action", "Price", "SL", "TP", "Status"])
    if log_df.empty:
        st.write("No trades yet.")
    else:
        st.dataframe(log_df, use_container_width=True)

# Run the fragment
render_dynamic_dashboard()
