import yfinance as yf
df_seed = yf.download("JPY=X", interval="1m", period="5d", progress=False)
print(len(df_seed))
