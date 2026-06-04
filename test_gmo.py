import requests
import json
response = requests.get("https://api.coin.zcom.jp/public/v1/ticker?symbol=BTC_JPY")
print(response.json())
