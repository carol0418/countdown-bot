import os
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 這裡需要複製 app.py 中的 Firebase 初始化和訊息產生邏輯
# 為了簡化，我們直接從 app.py 導入它們
# 注意：這需要在部署時確保 app.py 也在環境中
# (在 Vercel 中，這通常是自動處理的)
from app import db, configuration, get_countdown_message, logger

from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage
)

# 主要的排程任務邏輯
def execute_job():
    logger.info("Executing daily countdown message task via Vercel Cron...")
    if db is None:
        logger.error("Firestore client not available. Skipping scheduled job.")
        return False

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        chats_ref = db.collection('chats')
        docs = chats_ref.stream()

        for doc in docs:
            chat_id = doc.id
            chat_data = doc.to_dict()
            exam_date_str = chat_data.get('exam_date')

            if exam_date_str:
                try:
                    countdown_message_text = get_countdown_message(exam_date_str)
                    messages_to_send = [TextMessage(text=countdown_message_text)]
                    
                    line_bot_api.push_message(PushMessageRequest(
                        to=chat_id,
                        messages=messages_to_send
                    ))
                    logger.info(f"Successfully pushed countdown message to chat: {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to push countdown message to chat {chat_id}: {e}")
    return True

# Vercel Serverless Function 的標準入口
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # --- 安全驗證 (強烈建議) ---
        # 從 Vercel 環境變數中取得您設定的密鑰
        cron_secret = os.getenv('CRON_SECRET')
        
        # 從請求標頭中取得 Vercel Cron Job 發來的密鑰
        auth_header = self.headers.get('Authorization')
        
        # 如果環境變數有設定密鑰，就進行驗證
        if cron_secret and auth_header != f"Bearer {cron_secret}":
            self.send_response(401) # 401 Unauthorized
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode('utf-8'))
            return

        # 驗證通過，執行主要任務
        success = execute_job()
        
        if success:
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Job executed successfully."}).encode('utf-8'))
        else:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Job execution failed."}).encode('utf-8'))
        return
