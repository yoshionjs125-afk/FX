import streamlit as st
import pandas as pd
import pandas_ta as ta
import requests
import yfinance as yf
import time
import threading
from datetime import datetime
import plotly.graph_objects as go

# ==========================================
# State Management
# ==========================================
class BotState:
    def __init__(self):
        self.is_running = False
        self.oanda_account_id = ""
        self.oanda_api_token = ""
        self.env = "Practice"
        self.webhook_url = ""
        self.timeframe = "1 Minute (Free Data)"
        self.ema_period = 200
        self.rsi_buy = 60
        self.rsi_sell = 40
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        self.atr_sl = 1.5
        self.atr_tp = 3.0
        self.logs = []
        self.balance = "---"
        self.positions = "---"
        self.last_trade_time = None
        self.has_position_last_check = False
        self.df_latest = None
        self.current_signal = "⚪ [MONITORING] - Waiting for setup..."

@st.cache_resource
def get_global_state():
    return BotState()

bot_state = get_global_state()

if "tick_history" not in st.session_state:
    st.session_state.tick_history = []

# ==========================================
# Core Logic & API Connections
# ==========================================
def get_oanda_url(env):
    if env == "Live":
        return "https://api-fxtrade.oanda.com/v3"
    return "https://api-fxpractice.oanda.com/v3"

def send_webhook(webhook_url, message):
    if webhook_url:
        try:
            requests.post(webhook_url, json={"content": message})
        except Exception as e:
            print(f"Webhook error: {e}")

def update_account_info(state):
    if not state.oanda_account_id or not state.oanda_api_token:
        state.balance = "N/A (Notification Mode)"
        state.positions = "N/A"
        return
    url = f"{get_oanda_url(state.env)}/accounts/{state.oanda_account_id}"
    headers = {"Authorization": f"Bearer {state.oanda_api_token}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            acc = response.json().get('account', {})
            state.balance = f"${float(acc.get('balance', 0)):.2f}"
            state.positions = str(acc.get('openPositionCount', 0))
    except Exception as e:
        print(f"Error fetching account info: {e}")

def fetch_data(state):
    # Only fetch actual OANDA candlestick data here. yfinance ticks are handled in fragment.
    if not state.oanda_account_id or not state.oanda_api_token:
        return None

    oanda_granularity = "S5" if state.timeframe == "5 Seconds (OANDA Live API Mode)" else "M1"

    url = f"{get_oanda_url(state.env)}/instruments/USD_JPY/candles?count=300&price=M&granularity={oanda_granularity}"
    headers = {"Authorization": f"Bearer {state.oanda_api_token}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            candles = data.get('candles', [])
            records = []
            for c in candles:
                if c['complete']:
                    records.append({
                        'time': pd.to_datetime(c['time']),
                        'open': float(c['mid']['o']),
                        'high': float(c['mid']['h']),
                        'low': float(c['mid']['l']),
                        'close': float(c['mid']['c'])
                    })
            return pd.DataFrame(records)
    except Exception as e:
        print(f"Error fetching data from OANDA: {e}")
    return None

def check_open_positions(state):
    if not state.oanda_account_id or not state.oanda_api_token:
        return False

    url = f"{get_oanda_url(state.env)}/accounts/{state.oanda_account_id}/positions/USD_JPY"
    headers = {"Authorization": f"Bearer {state.oanda_api_token}"}
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            pos = response.json().get('position', {})
            long_units = int(pos.get('long', {}).get('units', 0))
            short_units = int(pos.get('short', {}).get('units', 0))
            return long_units != 0 or short_units != 0
        elif response.status_code == 404:
            return False
    except Exception as e:
        print(f"Position check error: {e}")
    return False

def execute_trade(state, action, price, sl, tp, candle_time):
    if not state.oanda_account_id or not state.oanda_api_token:
        # Should not be reached in new logic since API checks are separate, but safe to keep
        return

    units = 10000 if action == "BUY" else -10000

    order_payload = {
        "order": {
            "units": str(units),
            "instrument": "USD_JPY",
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{sl:.3f}"
            },
            "takeProfitOnFill": {
                "price": f"{tp:.3f}"
            }
        }
    }

    url = f"{get_oanda_url(state.env)}/accounts/{state.oanda_account_id}/orders"
    headers = {
        "Authorization": f"Bearer {state.oanda_api_token}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json=order_payload)

        log_entry = {
            "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Action": action,
            "Price": round(price, 3),
            "SL": round(sl, 3),
            "TP": round(tp, 3),
            "Status": response.status_code
        }
        state.logs.insert(0, log_entry)

        if response.status_code == 201:
            msg = f"EXECUTED {action} USD/JPY @ {price:.3f} | SL: {sl:.3f} | TP: {tp:.3f}"
            send_webhook(state.webhook_url, msg)
            state.last_trade_time = candle_time
        else:
            msg = f"FAILED {action} USD/JPY | Code: {response.status_code} | Msg: {response.text}"
            send_webhook(state.webhook_url, msg)
            print(f"Order failed: {response.text}")

    except Exception as e:
        print(f"Execution error: {e}")

def compute_indicators_and_signals(df, state):
    df['EMA'] = ta.ema(df['close'], length=state.ema_period)
    macd = ta.macd(df['close'], fast=state.macd_fast, slow=state.macd_slow, signal=state.macd_signal)
    if macd is not None and not macd.empty:
        df['MACD'] = macd.iloc[:, 0]
        df['MACD_Signal'] = macd.iloc[:, 2]

    df['RSI'] = ta.rsi(df['close'], length=14)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

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
# Background Loop Setup
# ==========================================
def background_loop(state):
    while True:
        if state.oanda_account_id and state.oanda_api_token:
            update_account_info(state)

            has_position = check_open_positions(state)
            if state.has_position_last_check and not has_position:
                send_webhook(state.webhook_url, "POSITION CLOSED for USD/JPY.")
                log_entry = {
                    "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "Action": "CLOSED",
                    "Price": 0.0,
                    "SL": 0.0,
                    "TP": 0.0,
                    "Status": "N/A"
                }
                state.logs.insert(0, log_entry)

            state.has_position_last_check = has_position

            df = fetch_data(state)
            state.current_signal = "⚪ [MONITORING] - Waiting for setup..."

            if df is not None and not df.empty and len(df) >= state.ema_period + 1:
                df['close'] = pd.to_numeric(df['close'], errors='coerce')
                df['high'] = pd.to_numeric(df['high'], errors='coerce')
                df['low'] = pd.to_numeric(df['low'], errors='coerce')
                df.dropna(subset=['close', 'high', 'low'], inplace=True)

                if len(df) >= state.ema_period + 1:
                    df = compute_indicators_and_signals(df, state)
                    state.df_latest = df

                    if state.is_running:
                        latest = df.iloc[-1]

                        if not (pd.isna(latest['EMA']) or pd.isna(latest['ATR']) or pd.isna(latest['RSI'])):
                            if latest['BUY_SIGNAL']:
                                state.current_signal = "🟢 [SIGNAL ACTIVE] - BUY Alert Triggered!"
                                if not has_position and latest['time'] != state.last_trade_time:
                                    sl = latest['close'] - (state.atr_sl * latest['ATR'])
                                    tp = latest['close'] + (state.atr_tp * latest['ATR'])
                                    execute_trade(state, "BUY", latest['close'], sl, tp, latest['time'])

                            elif latest['SELL_SIGNAL']:
                                state.current_signal = "🔴 [SIGNAL ACTIVE] - SELL Alert Triggered!"
                                if not has_position and latest['time'] != state.last_trade_time:
                                    sl = latest['close'] + (state.atr_sl * latest['ATR'])
                                    tp = latest['close'] - (state.atr_tp * latest['ATR'])
                                    execute_trade(state, "SELL", latest['close'], sl, tp, latest['time'])

        # Fast 5-second background loop for ultra-short S5 mode
        time.sleep(5)

@st.cache_resource
def start_background_thread():
    state = get_global_state()
    thread = threading.Thread(target=background_loop, args=(state,), daemon=True)
    thread.start()
    return thread

start_background_thread()

# ==========================================
# Streamlit UI
# ==========================================
st.set_page_config(page_title="FX Trading Bot Dashboard", layout="wide")

st.sidebar.header("API Configuration")
bot_state.oanda_account_id = st.sidebar.text_input("OANDA Account ID (Optional)", value=bot_state.oanda_account_id)
bot_state.oanda_api_token = st.sidebar.text_input("OANDA API Token (Optional)", type="password", value=bot_state.oanda_api_token)
bot_state.env = st.sidebar.radio("Environment", ["Practice", "Live"], index=0 if bot_state.env=="Practice" else 1)
bot_state.webhook_url = st.sidebar.text_input("Webhook URL (Discord/Slack/LINE)", value=bot_state.webhook_url)

st.sidebar.header("Strategy Parameters")
timeframe_opts = ["1 Minute (Free Data)", "5 Seconds (OANDA Live API Mode)"]
timeframe_index = 0 if bot_state.timeframe == "1 Minute (Free Data)" else 1
bot_state.timeframe = st.sidebar.selectbox("Timeframe", timeframe_opts, index=timeframe_index)

bot_state.ema_period = st.sidebar.slider("EMA Period", min_value=50, max_value=300, value=bot_state.ema_period)
bot_state.rsi_buy = st.sidebar.slider("RSI Buy Max Level", 0, 100, bot_state.rsi_buy)
bot_state.rsi_sell = st.sidebar.slider("RSI Sell Min Level", 0, 100, bot_state.rsi_sell)

st.sidebar.subheader("MACD Settings")
bot_state.macd_fast = st.sidebar.number_input("MACD Fast Period", min_value=1, max_value=100, value=bot_state.macd_fast)
bot_state.macd_slow = st.sidebar.number_input("MACD Slow Period", min_value=1, max_value=200, value=bot_state.macd_slow)
bot_state.macd_signal = st.sidebar.number_input("MACD Signal Period", min_value=1, max_value=100, value=bot_state.macd_signal)

st.sidebar.subheader("Risk Management")
bot_state.atr_sl = st.sidebar.slider("ATR Stop Loss Multiplier", 0.5, 5.0, bot_state.atr_sl, step=0.1)
bot_state.atr_tp = st.sidebar.slider("ATR Take Profit Multiplier", 1.0, 10.0, bot_state.atr_tp, step=0.1)

st.title("FX Trading Bot - USD/JPY")

mode_text = "Live Trading Mode" if bot_state.oanda_account_id and bot_state.oanda_api_token else "Notification-Only Mode"
st.write(f"**Current Mode:** {mode_text} | **Timeframe:** {bot_state.timeframe}")

is_on = st.toggle("Bot Status (ON/OFF)", value=bot_state.is_running)
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

@st.fragment(run_every="5s")
def render_dynamic_dashboard(state):
    # Immediate Alert Box
    if "BUY" in state.current_signal:
        st.success(state.current_signal)
    elif "SELL" in state.current_signal:
        st.error(state.current_signal)
    else:
        st.info(state.current_signal)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Account Balance", state.balance)
    with col2:
        st.metric("Active Positions", state.positions)
    with col3:
        st.metric("Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    st.subheader("Live USD/JPY Chart")

    if state.oanda_account_id and state.oanda_api_token:
        # API Mode: Render High-Visibility OANDA Candlestick Chart
        if state.df_latest is not None and not state.df_latest.empty:
            df_plot = state.df_latest.tail(80).copy() # Limit to 80 bars for performance

            # Convert time to string format to remove gaps on the x-axis
            df_plot['time_str'] = df_plot['time'].dt.strftime('%H:%M:%S')

            fig = go.Figure()

            fig.add_trace(go.Candlestick(
                x=df_plot['time_str'],
                open=df_plot['open'],
                high=df_plot['high'],
                low=df_plot['low'],
                close=df_plot['close'],
                name="Price"
            ))

            if 'EMA' in df_plot.columns:
                fig.add_trace(go.Scatter(
                    x=df_plot['time_str'],
                    y=df_plot['EMA'],
                    mode='lines',
                    line=dict(color='blue', width=2),
                    name=f'{state.ema_period} EMA'
                ))

            buy_signals = df_plot[df_plot['BUY_SIGNAL'] == True]
            if not buy_signals.empty:
                fig.add_trace(go.Scatter(
                    x=buy_signals['time_str'],
                    y=buy_signals['low'] - (buy_signals['ATR'] * 0.5),
                    mode='markers+text',
                    marker=dict(symbol='triangle-up', color='green', size=18),
                    text=["BUY"] * len(buy_signals),
                    textposition="bottom center",
                    textfont=dict(color="green", size=14),
                    name='BUY Signal'
                ))

            sell_signals = df_plot[df_plot['SELL_SIGNAL'] == True]
            if not sell_signals.empty:
                fig.add_trace(go.Scatter(
                    x=sell_signals['time_str'],
                    y=sell_signals['high'] + (sell_signals['ATR'] * 0.5),
                    mode='markers+text',
                    marker=dict(symbol='triangle-down', color='red', size=18),
                    text=["SELL"] * len(sell_signals),
                    textposition="top center",
                    textfont=dict(color="red", size=14),
                    name='SELL Signal'
                ))

            fig.update_layout(
                title="OANDA Live Chart",
                yaxis_title="Price",
                xaxis_title="Time",
                template="plotly_dark",
                height=600,
                xaxis_rangeslider_visible=False,
                xaxis=dict(type='category', tickangle=-45) # Remove awkward time gaps
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Waiting for data from OANDA...")

    else:
        # Notification-Only Mode: Live Price Accumulator (yfinance spot)
        try:
            ticker = yf.Ticker("JPY=X")
            spot = ticker.fast_info.last_price
            current_time = datetime.now().strftime('%H:%M:%S')

            # Simple mock strategy for sub-minute ticks to trigger notifications
            st.session_state.tick_history.append({"time": current_time, "price": spot})

            # Keep only the last 60 ticks (5 minutes)
            if len(st.session_state.tick_history) > 60:
                st.session_state.tick_history.pop(0)

            df_ticks = pd.DataFrame(st.session_state.tick_history)

            # Trigger logic for accumulated data (Moving Average crossover on sub-minute ticks)
            if len(df_ticks) > 10:
                df_ticks['SMA_Short'] = ta.sma(df_ticks['price'], length=5)
                df_ticks['SMA_Long'] = ta.sma(df_ticks['price'], length=10)

                latest = df_ticks.iloc[-1]
                prev = df_ticks.iloc[-2]

                state.current_signal = "⚪ [MONITORING] - Waiting for setup..."

                if not pd.isna(latest['SMA_Long']):
                    if prev['SMA_Short'] <= prev['SMA_Long'] and latest['SMA_Short'] > latest['SMA_Long']:
                        state.current_signal = f"🟢 [SIGNAL ACTIVE] - BUY Alert Triggered at {latest['price']:.3f}!"
                        if state.is_running and latest['time'] != state.last_trade_time:
                            msg = f"🔥 [SIGNAL ALERT] USD/JPY BUY Signal triggered at {latest['price']:.3f}"
                            send_webhook(state.webhook_url, msg)

                            log_entry = {
                                "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Action": "SIGNAL: BUY",
                                "Price": round(latest['price'], 3),
                                "SL": "---",
                                "TP": "---",
                                "Status": "Alert Sent"
                            }
                            state.logs.insert(0, log_entry)
                            state.last_trade_time = latest['time']

                    elif prev['SMA_Short'] >= prev['SMA_Long'] and latest['SMA_Short'] < latest['SMA_Long']:
                        state.current_signal = f"🔴 [SIGNAL ACTIVE] - SELL Alert Triggered at {latest['price']:.3f}!"
                        if state.is_running and latest['time'] != state.last_trade_time:
                            msg = f"🔥 [SIGNAL ALERT] USD/JPY SELL Signal triggered at {latest['price']:.3f}"
                            send_webhook(state.webhook_url, msg)

                            log_entry = {
                                "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "Action": "SIGNAL: SELL",
                                "Price": round(latest['price'], 3),
                                "SL": "---",
                                "TP": "---",
                                "Status": "Alert Sent"
                            }
                            state.logs.insert(0, log_entry)
                            state.last_trade_time = latest['time']

            # Render the Smooth Line Chart
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df_ticks['time'],
                y=df_ticks['price'],
                mode='lines+markers',
                line=dict(color='cyan', width=2),
                name="Live USD/JPY"
            ))

            fig.update_layout(
                title="Live Tick Chart (Notification Mode)",
                yaxis_title="Price",
                xaxis_title="Time",
                template="plotly_dark",
                height=600,
                xaxis_rangeslider_visible=False
            )
            st.plotly_chart(fig, use_container_width=True)

        except Exception as e:
            st.error(f"Error fetching live tick: {e}")

    st.subheader("Recent Trade Logs")
    log_df = pd.DataFrame(state.logs, columns=["Time", "Action", "Price", "SL", "TP", "Status"])
    if log_df.empty:
        st.write("No trades yet.")
    else:
        st.dataframe(log_df, use_container_width=True)

render_dynamic_dashboard(bot_state)
