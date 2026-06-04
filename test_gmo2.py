import requests
import json
try:
    response = requests.get("https://forex-api.coin.zcom.jp/public/v1/ticker?symbol=USD_JPY")
    print(response.json())
except Exception as e:
    print(e)
