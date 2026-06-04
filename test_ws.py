import websocket
import json

def on_message(ws, message):
    print("Received:", message)
    ws.close()

def on_error(ws, error):
    print("Error:", error)

def on_open(ws):
    print("Opened")
    req = {
        "command": "subscribe",
        "channel": "ticker",
        "symbol": "USD_JPY"
    }
    ws.send(json.dumps(req))

ws = websocket.WebSocketApp("wss://api.coin.zcom.jp/ws/public/v1", on_message=on_message, on_error=on_error, on_open=on_open)
ws.run_forever()
