import os
from datetime import datetime
import json
import logging
from flask import Flask, request, abort

# 引入 Firebase Admin SDK 相關模組
import firebase_admin
from firebase_admin import credentials, firestore
from firebase_admin import exceptions as firebase_exceptions

import pytz 
from datetime import datetime

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    PushMessageRequest,
    ImageMessage,
    StickerMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
    JoinEvent
)

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# --- Firebase 初始化 (維持不變) ---
db = None
cred = None
try:
    service_account_json_str = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')
    if service_account_json_str:
        service_account_info = json.loads(service_account_json_str)
        cred = credentials.Certificate(service_account_info)
    else:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_KEY_JSON environment variable is not set.")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase app initialized and Firestore client created.")
except (ValueError, json.JSONDecodeError, firebase_exceptions.FirebaseError, Exception) as e:
    logger.critical(f"FATAL ERROR: Could not initialize Firebase or Firestore: {e}")
    db = None

# --- 訊息生成函數 (維持不變) ---
def get_countdown_message(exam_date_str):
    if not exam_date_str:
        return "很抱歉，尚未設定考試日期。請輸入'設定考試日期 YYYY-MM-DD'來設定。"
    try:    
        # 1. 定義台北時區
        taipei_tz = pytz.timezone("Asia/Taipei")

        # 2. 處理考試日期，並使其具有時區資訊
        exam_date_naive = datetime.strptime(exam_date_str, "%Y-%m-%d")
        exam_date_aware = taipei_tz.localize(exam_date_naive)

        # 3. 取得當下台北時間，並標準化為當天零點
        today_aware = datetime.now(taipei_tz)
        
        # 4. 進行日期相減，變數名稱需保持一致
        #    為了確保天數計算精確，我們只比較日期，不比較時間
        time_left = exam_date_aware.date() - today_aware.date()
        
        # 5. 從結果中取得天數
        days_left = time_left.days
        
        if days_left > 0:
            message = ""
            if days_left == 100: message = f"⌛時光飛逝，你只剩下{days_left} 天，趕快拿起書本來📚📚"
            elif days_left == 90: message = f"沒想到已經剩下{days_left} 天\n｡ﾟヽ(ﾟ´Д`)ﾉﾟ｡時間都在我的睡夢中流失了！"
            elif days_left == 30: message = f"距離考試只剩下 {days_left} 天！\n祝你考試像打遊戲一樣，一路都是暴擊，分數直接爆表！🔥🔥"
            elif days_left == 10: message = f"距離考試只剩下 {days_left} 天！\n祝你考試像吃雞腿一樣，輕鬆又美味，分數高高🍗"
            else: message = f"你今天讀書了嗎？💥\n距離考試只剩下 {days_left} 天！加油！💪💪💪"
        elif days_left == 0: message = f"你今天讀書了嗎？\n今天是考試的日子🏆金榜題名🏆"
        else: message = f"考試 ({exam_date_str}) 已經在 {abs(days_left)} 天前結束了。期待你下次的挑戰！"
        return message
    except ValueError:
        return "考試日期格式錯誤，請檢查設定或重新設定。正確格式為YYYY-MM-DD。"

# --- LINE Bot Webhook 回調入口 (維持不變) ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... 此函數的程式碼完全不變 ...
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)
    return 'OK'

# --- 【已移除】/wakeup 端點，因為不再需要外部服務來喚醒 ---

# --- LINE 訊息與事件處理 (維持不變) ---
@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    # ... 此函數的程式碼完全不變 ...
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        text = event.message.text
        source_id = event.source.group_id if event.source.type == 'group' else event.source.user_id
        if not source_id:
            logger.error("Could not determine source ID from event.")
            return
        doc_ref = db.collection('chats').document(source_id)
        if text.startswith("設定考試日期"):
            parts = text.split()
            if len(parts) == 2:
                date_str = parts[1]
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                    doc_ref.set({'exam_date': date_str}, merge=True)
                    line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=f"專屬於您的考試日期已設定為：{date_str}")]))
                except ValueError:
                    line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="日期格式不正確，請使用YYYY-MM-DD。")]))
            else:
                line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="請輸入正確指令：設定考試日期 YYYY-MM-DD")]))
        elif text == "查詢剩餘天數":
            doc = doc_ref.get()
            exam_date_str = doc.to_dict().get('exam_date') if doc.exists else None
            countdown_message = get_countdown_message(exam_date_str)
            line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=countdown_message)]))

@line_handler.add(FollowEvent)
def handle_follow(event):
    # ... 此函數的程式碼完全不變 ...
    user_id = event.source.user_id
    doc_ref = db.collection('chats').document(user_id)
    doc_ref.set({'type': 'user', 'exam_date': None}, merge=True)
    logger.info(f"User document created/updated in Firestore for user: {user_id}")
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        messages = [
            TextMessage(text="哈囉！謝謝你加入這個倒數計時小幫手😎！\n\n🍊你可以輸入: \n【設定考試日期YYYY-MM-DD】來設定你的重要日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n準備好了嗎？我們一起努力！\nd(`･∀･)b"),
            StickerMessage(package_id='11538', sticker_id='51626494')
        ]
        try:
            line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))
        except Exception as e:
            logger.error(f"Failed to send welcome message to user {user_id}: {e}")

@line_handler.add(JoinEvent)
def handle_join(event):
    # ... 此函數的程式碼完全不變 ...
    if event.source.type == "group":
        group_id = event.source.group_id
        doc_ref = db.collection('chats').document(group_id)
        doc_ref.set({'type': 'group', 'exam_date': None}, merge=True)
        logger.info(f"Group document created in Firestore for group: {group_id}")
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            messages = [
                TextMessage(text="哈囉！大家好！\n我是你們的倒數計時小幫手😎，很高興加入這個群組！\n\n🍊群組裡面的任何一位成員都可以輸入【設定考試日期YYYY-MM-DD】來設定日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入【查詢剩餘天數】就能知道距離考試還有多久喔！\n\n讓我們一起為目標衝刺吧！\nd(`･∀･)b"),
                StickerMessage(package_id='11538', sticker_id='51626494')
            ]
            try:
                line_bot_api.reply_message_with_http_info(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))
            except Exception as e:
                logger.error(f"Failed to send welcome message to group {group_id}: {e}")

# --- 程式的入口點 (維持不變) ---
if __name__ == "__main__":
    app.run(debug=True)
