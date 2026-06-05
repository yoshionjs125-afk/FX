from collections import deque
import yfinance as yf
import pandas as pd
dq = deque(maxlen=250)
df_seed = yf.download("JPY=X", interval="1m", period="1d", progress=False)
if isinstance(df_seed.columns, pd.MultiIndex):
    df_seed.columns = [col[0] for col in df_seed.columns]
df_seed.reset_index(inplace=True)
print(len(df_seed))
for _, row in df_seed.tail(200).iterrows():
    dq.append({
        'time': row['Datetime'].tz_localize(None),
        'open': float(row['Open']),
        'high': float(row['High']),
        'low': float(row['Low']),
        'close': float(row['Close']),
        'price': float(row['Close'])
    })
print(len(dq))
