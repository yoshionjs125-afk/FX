import requests
import json
try:
    response = requests.get("https://api.coin.zcom.jp/public/v1/status")
    print(response.json())
except Exception as e:
    print(e)
