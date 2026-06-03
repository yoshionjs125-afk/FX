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
        self.timeframe = "1 Hour"
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

@st.cache_resource
def get_global_state():
    return BotState()

bot_state = get_global_state()

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
    # Determine intervals based on timeframe setting
    yf_interval = "1m" if state.timeframe == "1 Minute" else "1h"
    yf_period = "1d" if state.timeframe == "1 Minute" else "30d"
    oanda_granularity = "M1" if state.timeframe == "1 Minute" else "H1"

    # Data Fetching Fallback: use yfinance if OANDA credentials are empty
    if not state.oanda_account_id or not state.oanda_api_token:
        try:
            df = yf.download("JPY=X", interval=yf_interval, period=yf_period, progress=False)
            if df.empty:
                return None

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [col[0] for col in df.columns]

            # Rename columns to match the OANDA logic
            df.reset_index(inplace=True)
            df.rename(columns={
                'Datetime': 'time',
                'Open': 'open',
                'High': 'high',
                'Low': 'low',
                'Close': 'close'
            }, inplace=True)
            return df
        except Exception as e:
            print(f"Error fetching yfinance data: {e}")
            return None

    # OANDA Data Fetching
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
    # Return False if Notification-Only Mode
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
    # Notification-Only Mode check
    if not state.oanda_account_id or not state.oanda_api_token:
        log_entry = {
            "Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Action": f"SIGNAL: {action}",
            "Price": round(price, 3),
            "SL": round(sl, 3),
            "TP": round(tp, 3),
            "Status": "Alert Sent"
        }
        state.logs.insert(0, log_entry)

        msg = f"🔥 [SIGNAL ALERT] USD/JPY {action} Signal triggered at {price:.3f} | SL: {sl:.3f} | TP: {tp:.3f}"
        send_webhook(state.webhook_url, msg)
        state.last_trade_time = candle_time
        return

    # Live Trading Mode
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

        # Log regardless of success for transparency
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
            state.last_trade_time = candle_time # Prevent duplicate trades on same candle
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

    # Pre-compute signals for charting
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
        # Always fetch data so the chart updates even if bot is OFF
        df = fetch_data(state)
        has_position = False

        if state.is_running:
            update_account_info(state)

            # Check for position closures (Only relevant if OANDA credentials exist)
            if state.oanda_account_id and state.oanda_api_token:
                has_position = check_open_positions(state)
                if state.has_position_last_check and not has_position:
                    # Position was closed since last check
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
            else:
                has_position = False

        if df is not None and not df.empty and len(df) >= state.ema_period + 1:
            # Ensure 'close' is numeric
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df['high'] = pd.to_numeric(df['high'], errors='coerce')
            df['low'] = pd.to_numeric(df['low'], errors='coerce')

            df.dropna(subset=['close', 'high', 'low'], inplace=True)

            if len(df) >= state.ema_period + 1:
                df = compute_indicators_and_signals(df, state)
                state.df_latest = df

                # Trading Logic (only execute if running)
                if state.is_running:
                    latest = df.iloc[-1]

                    if not (pd.isna(latest['EMA']) or pd.isna(latest['ATR']) or pd.isna(latest['RSI'])):
                        if not has_position and latest['time'] != state.last_trade_time:
                            if latest['BUY_SIGNAL']:
                                sl = latest['close'] - (state.atr_sl * latest['ATR'])
                                tp = latest['close'] + (state.atr_tp * latest['ATR'])
                                execute_trade(state, "BUY", latest['close'], sl, tp, latest['time'])

                            elif latest['SELL_SIGNAL']:
                                sl = latest['close'] + (state.atr_sl * latest['ATR'])
                                tp = latest['close'] - (state.atr_tp * latest['ATR'])
                                execute_trade(state, "SELL", latest['close'], sl, tp, latest['time'])

        time.sleep(10)

@st.cache_resource
def start_background_thread():
    # Because @st.cache_resource is called once per app lifetime,
    # the background thread will only be started exactly once.
    state = get_global_state()
    thread = threading.Thread(target=background_loop, args=(state,), daemon=True)
    thread.start()
    return thread

# Initialize background thread
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
# Add timeframe selection
timeframe_index = 0 if bot_state.timeframe == "1 Hour" else 1
bot_state.timeframe = st.sidebar.selectbox("Timeframe", ["1 Hour", "1 Minute"], index=timeframe_index)

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

# Active Parameter Dashboard (Static / Outer Scope)
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

# Dynamic Fragment for Data and Chart rendering
@st.fragment(run_every="10s")
def render_dynamic_dashboard(state):
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Account Balance", state.balance)
    with col2:
        st.metric("Active Positions", state.positions)
    with col3:
        st.metric("Last Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Chart Rendering
    st.subheader("Live USD/JPY Chart")
    if state.df_latest is not None and not state.df_latest.empty:
        df_plot = state.df_latest.tail(150) # Show last 150 candles

        fig = go.Figure()

        # Candlestick
        fig.add_trace(go.Candlestick(
            x=df_plot['time'],
            open=df_plot['open'],
            high=df_plot['high'],
            low=df_plot['low'],
            close=df_plot['close'],
            name="Price"
        ))

        # EMA
        if 'EMA' in df_plot.columns:
            fig.add_trace(go.Scatter(
                x=df_plot['time'],
                y=df_plot['EMA'],
                mode='lines',
                line=dict(color='blue', width=2),
                name=f'{state.ema_period} EMA'
            ))

        # Signals
        buy_signals = df_plot[df_plot['BUY_SIGNAL'] == True]
        if not buy_signals.empty:
            fig.add_trace(go.Scatter(
                x=buy_signals['time'],
                y=buy_signals['low'] - (buy_signals['ATR'] * 0.5), # Offset below candle
                mode='markers',
                marker=dict(symbol='triangle-up', color='green', size=15),
                name='BUY Signal'
            ))

        sell_signals = df_plot[df_plot['SELL_SIGNAL'] == True]
        if not sell_signals.empty:
            fig.add_trace(go.Scatter(
                x=sell_signals['time'],
                y=sell_signals['high'] + (sell_signals['ATR'] * 0.5), # Offset above candle
                mode='markers',
                marker=dict(symbol='triangle-down', color='red', size=15),
                name='SELL Signal'
            ))

        fig.update_layout(
            title="USD/JPY Price Action with Signals",
            yaxis_title="Price",
            xaxis_title="Time",
            template="plotly_dark",
            height=600,
            xaxis_rangeslider_visible=False
        )

        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Waiting for data to populate the chart. Make sure 'Bot Status' is ON (or wait for the first fetch).")


    st.subheader("Recent Trade Logs")
    log_df = pd.DataFrame(state.logs, columns=["Time", "Action", "Price", "SL", "TP", "Status"])
    if log_df.empty:
        st.write("No trades yet.")
    else:
        st.dataframe(log_df, use_container_width=True)

# Run the fragment
render_dynamic_dashboard(bot_state)
