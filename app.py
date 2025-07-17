import os
from datetime import datetime
import json
import logging
from flask import Flask, request, abort

# 引入 APScheduler 相關模組，用於排程任務
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit # 用於確保應用程式關閉時排程器也能安全停止

# 引入 Firebase Admin SDK 相關模組
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from firebase_admin import exceptions as firebase_exceptions # 引入 Firebase 相關例外處理

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

# 初始化 Flask 應用
app = Flask(__name__)

# 設定日誌記錄，方便開發時追蹤問題
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 從環境變數中取得 LINE Channel Access Token 和 Channel Secret
configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# --- Firebase 初始化 ---
db = None
cred = None

try:
    service_account_json_str = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')
    if service_account_json_str:
        service_account_info = json.loads(service_account_json_str)
        cred = credentials.Certificate(service_account_info)
    else:
        raise ValueError(
            "FIREBASE_SERVICE_ACCOUNT_KEY_JSON environment variable is not set. "
            "Firebase initialization requires this for production deployments. "
        )

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase app initialized and Firestore client created.")

except (ValueError, json.JSONDecodeError, firebase_exceptions.FirebaseError, Exception) as e:
    logger.critical(f"FATAL ERROR: Could not initialize Firebase or Firestore: {e}")
    db = None

# --- 訊息生成函數 ---
def get_countdown_message(exam_date_str):
    """
    根據傳入的考試日期計算剩餘天數並生成對應的訊息。
    """
    if not exam_date_str:
        return "很抱歉，尚未設定考試日期。請輸入'設定考試日期 YYYY-MM-DD'來設定。"

    try:
        exam_date = datetime.strptime(exam_date_str, "%Y-%m-%d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        time_left = exam_date - today
        days_left = time_left.days

        if days_left > 0:
            message = ""
            if days_left == 100:
                message = f"⌛時光飛逝，你只剩下{days_left} 天，趕快拿起書本來📚📚"
            elif days_left == 90:
                message = f"沒想到已經剩下{days_left} 天\n｡ﾟヽ(ﾟ´Д`)ﾉﾟ｡時間都在我的睡夢中流失了！"
            elif days_left == 30:
                message = f"距離考試只剩下 {days_left} 天！\n祝你考試像打遊戲一樣，一路都是暴擊，分數直接爆表！🔥🔥"
            elif days_left == 10:
                message = f"距離考試只剩下 {days_left} 天！\n祝你考試像吃雞腿一樣，輕鬆又美味，分數高高🍗"
            else:
                message = f"你今天讀書了嗎？💥\n距離考試只剩下 {days_left} 天！加油！💪💪💪"
        elif days_left == 0:
            message = f"你今天讀書了嗎？\n今天是考試的日子🏆金榜題名🏆"
        else:
            message = f"考試 ({exam_date_str}) 已經在 {abs(days_left)} 天前結束了。期待你下次的挑戰！"
        return message
    except ValueError:
        return "考試日期格式錯誤，請檢查設定或重新設定。正確格式為YYYY-MM-DD。"

# --- APScheduler 設定與排程任務 ---
scheduler = BackgroundScheduler(timezone="Asia/Taipei")

def send_daily_countdown_message_job():
    """
    【全新修改】排程任務：遍歷 Firestore 中所有 chats，並推送個人化的倒數訊息。
    """
    logger.info("Executing daily countdown message task...")
    if db is None:
        logger.error("Firestore client not available. Skipping scheduled job.")
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # 1. 從 Firestore 的 'chats' 集合中取得所有文件
        chats_ref = db.collection('chats')
        docs = chats_ref.stream()

        # 2. 遍歷每一個文件 (代表一個使用者或群組)
        for doc in docs:
            chat_id = doc.id  # 文件ID 就是 user_id 或 group_id
            chat_data = doc.to_dict()
            exam_date_str = chat_data.get('exam_date')

            # 3. 如果該聊天室有設定考試日期，就產生並發送訊息
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

scheduler_started = False

@app.before_request
def start_scheduler_if_not_started():
    global scheduler_started
    if not scheduler_started:
        if db is None:
            logger.error("Firestore client not available. Scheduler will not be started.")
            return

        # 添加每天早上 7 點 10 分發送訊息的任務
        scheduler.add_job(
            send_daily_countdown_message_job,
            CronTrigger(hour=13, minute=45, timezone="Asia/Taipei"),
            id='daily_countdown',
            replace_existing=True
        )
        scheduler.start()
        scheduler_started = True
        logger.info("Scheduler started and daily countdown job added for 7:10 AM Taipei time.")

atexit.register(lambda: scheduler.shutdown())

# --- LINE Bot Webhook 回調入口 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)
    return 'OK'

@app.route("/wakeup", methods=['GET'])
def wakeup():
    logger.info("Wakeup endpoint hit by external service.")
    return 'OK', 200

# --- LINE 訊息事件處理 ---
@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    【全新修改】處理文字訊息，讀寫每個聊天室自己的文件。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        text = event.message.text
        
        # 1. 確定訊息來源 ID (可能是使用者 ID 或群組 ID)
        source_id = event.source.group_id if event.source.type == 'group' else event.source.user_id
        if not source_id:
            logger.error("Could not determine source ID from event.")
            return

        # 2. 建立指向該聊天室專屬文件的引用
        doc_ref = db.collection('chats').document(source_id)

        if text.startswith("設定考試日期"):
            parts = text.split()
            if len(parts) == 2:
                date_str = parts[1]
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                    # 3. 將日期寫入該聊天室的文件，merge=True 可確保只更新該欄位
                    doc_ref.set({'exam_date': date_str}, merge=True)
                    
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"專屬於您的考試日期已設定為：{date_str}")]
                        )
                    )
                except ValueError:
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="日期格式不正確，請使用YYYY-MM-DD。")])
                    )
            else:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="請輸入正確指令：設定考試日期 YYYY-MM-DD")])
                )
        
        elif text == "查詢剩餘天數":
            # 4. 從該聊天室的文件中讀取資料
            doc = doc_ref.get()
            if doc.exists:
                exam_date_str = doc.to_dict().get('exam_date')
            else:
                exam_date_str = None # 如果文件不存在，代表還沒設定過

            countdown_message = get_countdown_message(exam_date_str)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=countdown_message)]
                )
            )

# --- LINE Follow 事件處理 ---
@line_handler.add(FollowEvent)
def handle_follow(event):
    """
    【全新修改】處理使用者加入好友，為其建立專屬文件。
    """
    user_id = event.source.user_id
    doc_ref = db.collection('chats').document(user_id)
    
    # 使用 set 搭配 merge=True，如果文件已存在則不會覆蓋
    # 這可以處理用戶封鎖後再解除封鎖的情況
    doc_ref.set({
        'type': 'user',
        'exam_date': None # 預設 exam_date 為空
    }, merge=True)
    logger.info(f"User document created/updated in Firestore for user: {user_id}")
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        messages = [
            TextMessage(text="哈囉！謝謝你加入這個倒數計時小幫手😎！\n\n🍊你可以輸入: \n【設定考試日期YYYY-MM-DD】來設定你的重要日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n準備好了嗎？我們一起努力！\nd(`･∀･)b"),
            StickerMessage(package_id='11538', sticker_id='51626494')
        ]
        try:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
            )
        except Exception as e:
            logger.error(f"Failed to send welcome message to user {user_id}: {e}")

# --- LINE Join 事件處理 ---
@line_handler.add(JoinEvent)
def handle_join(event):
    """
    【全新修改】處理 Bot 加入群組，為其建立專屬文件。
    """
    if event.source.type == "group":
        group_id = event.source.group_id
        doc_ref = db.collection('chats').document(group_id)
        
        doc_ref.set({
            'type': 'group',
            'exam_date': None # 預設 exam_date 為空
        }, merge=True)
        logger.info(f"Group document created in Firestore for group: {group_id}")
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            messages = [
                TextMessage(text = "哈囉！大家好！\n我是你們的倒數計時小幫手😎，很高興加入這個群組！\n\n🍊群組裡面的任何一位成員都可以輸入【設定考試日期YYYY-MM-DD】來設定日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入【查詢剩餘天數】就能知道距離考試還有多久喔！\n\n讓我們一起為目標衝刺吧！\nd(`･∀･)b"),
                StickerMessage(package_id='11538', sticker_id='51626494')
            ]
            try:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
                )
            except Exception as e:
                logger.error(f"Failed to send welcome message to group {group_id}: {e}")

# --- 程式的入口點 ---
if __name__ == "__main__":
    app.run(debug=True)
