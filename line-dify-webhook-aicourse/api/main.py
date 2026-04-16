from fastapi import FastAPI, Request, Header, HTTPException
import requests
import os
import hmac
import hashlib
import base64
import re
import json

app = FastAPI()

LINE_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.getenv('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.getenv('DIFY_API_KEY')

# 【新增】用來儲存每個使用者的對話 ID (記憶功能)
# 注意：若部署在 Vercel 免費版，一段時間沒人使用伺服器休眠時，這個記憶會被清空。
# 若需永久記憶，建議改存入資料庫 (如 Redis、Firebase 或 Vercel KV)
user_sessions = {}

@app.post("/webhook")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    body_str = body.decode("utf-8")

    # 1. 驗證這條訊息真的是 LINE 官方傳來的 (資安防護)
    if LINE_SECRET and x_line_signature:
        hash_val = hmac.new(LINE_SECRET.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).digest()
        signature = base64.b64encode(hash_val).decode('utf-8')
        if signature != x_line_signature:
            raise HTTPException(status_code=400, detail="Invalid signature")

    # 2. 解析訊息並處理
    data = await request.json()
    for event in data.get('events', []):
        if event.get('type') == 'message' and event.get('message', {}).get('type') == 'text':
            user_message = event['message']['text']
            reply_token = event['replyToken']
            user_id = event['source'].get('userId', 'unknown_user')

            # --- 步驟 A：準備呼叫 Dify 大腦的資料 ---
            dify_payload = {
                "inputs": {},
                "query": user_message,
                "response_mode": "streaming", # Agent 必須使用串流模式
                "user": user_id
            }
            
            # 【新增】如果這個使用者之前有對話過，就把 conversation_id 帶上
            if user_id in user_sessions:
                dify_payload["conversation_id"] = user_sessions[user_id]

            try:
                # 傳送請求給 Dify
                dify_res = requests.post(
                    "https://api.dify.ai/v1/chat-messages",
                    headers={"Authorization": f"Bearer {DIFY_API_KEY}"},
                    json=dify_payload,
                    stream=True
                )
                
                # 檢查連線狀態
                if dify_res.status_code != 200:
                    error_data = dify_res.json()
                    error_msg = error_data.get('message', str(error_data))
                    answer = f"⚠️ Dify 大腦回報錯誤：\n{error_msg}"
                else:
                    answer = ""
                    # 逐行讀取串流資料
                    for line in dify_res.iter_lines():
                        if line:
                            decoded_line = line.decode('utf-8')
                            if decoded_line.startswith('data:'):
                                data_str = decoded_line[5:].strip()
                                try:
                                    json_data = json.loads(data_str)
                                    
                                    # 【新增】抓取並儲存 conversation_id，讓下一次對話能接續
                                    if 'conversation_id' in json_data:
                                        user_sessions[user_id] = json_data['conversation_id']
                                        
                                    # 兼容 Agent 模式與 Chatflow 模式
                                    event_type = json_data.get('event')
                                    if event_type in ['message', 'agent_message']:
                                        answer += json_data.get('answer', '')
                                except json.JSONDecodeError:
                                    pass
                                    
                    if not answer:
                        answer = "Dify 處理完畢，但未產生文字回應 (可能只回傳了思考過程)。"
                        
            except Exception as e:
                answer = f"伺服器連線例外錯誤：{str(e)}"

            # --- 步驟 B：超連結轉按鈕邏輯 (Flex Message) ---
            links = re.findall(r'\[([^\]]+)\]\((https?://[^\)]+)\)', answer)
            
            if links:
                clean_text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\1 (請點擊下方按鈕)', answer)
                buttons = []
                for title, url in links[:4]:
                    buttons.append({
                        "type": "button",
                        "style": "primary",
                        "margin": "sm",
                        "action": {
                            "type": "uri",
                            "label": title[:20],
                            "uri": url
                        }
                    })
                
                messages = [{
                    "type": "flex",
                    "altText": "助教傳送了一個連結給您",
                    "contents": {
                        "type": "bubble",
                        "body": {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": clean_text,
                                    "wrap": True
                                }
                            ] + buttons
                        }
                    }
                }]
            else:
                messages = [{"type": "text", "text": answer}]

            # --- 步驟 C：傳回 LINE ---
            requests.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
                },
                json={
                    "replyToken": reply_token,
                    "messages": messages
                }
            )
    return 'OK'
