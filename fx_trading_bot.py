import yfinance as yf
import pandas as pd
import pandas_ta as ta
import numpy as np
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
import warnings
warnings.filterwarnings("ignore")

def EMA(values, n):
    return ta.ema(pd.Series(values), length=n).to_numpy()

def MACD_LINE(values):
    macd_df = ta.macd(pd.Series(values))
    return macd_df.iloc[:, 0].to_numpy()

def MACD_SIGNAL(values):
    macd_df = ta.macd(pd.Series(values))
    return macd_df.iloc[:, 2].to_numpy()

def RSI(values, n):
    return ta.rsi(pd.Series(values), length=n).to_numpy()

def ATR(high, low, close, n):
    return ta.atr(pd.Series(high), pd.Series(low), pd.Series(close), length=n).to_numpy()


class TrendMomentumStrategy(Strategy):
    def init(self):
        self.ema_200 = self.I(EMA, self.data.Close, 200)

        self.macd_line = self.I(MACD_LINE, self.data.Close)
        self.macd_signal = self.I(MACD_SIGNAL, self.data.Close)

        self.rsi = self.I(RSI, self.data.Close, 14)

        self.atr = self.I(ATR, self.data.High, self.data.Low, self.data.Close, 14)

    def next(self):
        if len(self.data.Close) < 200:
            return

        current_price = self.data.Close[-1]
        current_ema = self.ema_200[-1]
        current_rsi = self.rsi[-1]
        current_atr = self.atr[-1]

        if pd.isna(current_atr) or pd.isna(current_ema) or pd.isna(current_rsi):
            return

        # Check if we're in a position
        if self.position:
            return

        # Entry Signal (BUY)
        if current_price > current_ema and current_rsi < 60:
            if crossover(self.macd_line, self.macd_signal):
                sl_price = current_price - (1.5 * current_atr)
                tp_price = current_price + (3.0 * current_atr)
                # Using 95% of equity to avoid margin issues
                self.buy(sl=sl_price, tp=tp_price, size=0.95)

        # Entry Signal (SELL)
        elif current_price < current_ema and current_rsi > 40:
            if crossover(self.macd_signal, self.macd_line):
                sl_price = current_price + (1.5 * current_atr)
                tp_price = current_price - (3.0 * current_atr)
                self.sell(sl=sl_price, tp=tp_price, size=0.95)


if __name__ == "__main__":
    print("Fetching historical USD/JPY data...")
    df = yf.download("JPY=X", interval="1h", period="700d", progress=False)

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    df.dropna(inplace=True)

    print("Running backtest...")
    bt = Backtest(
        df,
        TrendMomentumStrategy,
        cash=10000,
        margin=1/100, # 1:100 leverage typical for forex
        trade_on_close=False,
        exclusive_orders=True
    )

    stats = bt.run()

    print("\n=== Final Performance Metrics ===")

    win_rate = stats['Win Rate [%]']
    total_return = stats['Return [%]']
    profit_factor = stats['Profit Factor']
    max_drawdown = stats['Max. Drawdown [%]']

    print(f"Win Rate (%):      {win_rate:.2f}%" if pd.notna(win_rate) else "Win Rate (%):      N/A")
    print(f"Total Return (%):  {total_return:.2f}%" if pd.notna(total_return) else "Total Return (%):  N/A")
    print(f"Profit Factor:     {profit_factor:.2f}" if pd.notna(profit_factor) else "Profit Factor:     N/A")
    print(f"Max Drawdown (%):  {max_drawdown:.2f}%" if pd.notna(max_drawdown) else "Max Drawdown (%):  N/A")

    plot_filename = "usdjpy_backtest.html"
    print(f"\nSaving plot to '{plot_filename}'...")
    bt.plot(filename=plot_filename, open_browser=False, resample=False)
    print("Done!")
