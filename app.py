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
db = None # 初始化 Firestore 客戶端為 None

try:
    # 嘗試從環境變數 FIREBASE_SERVICE_ACCOUNT_KEY_JSON 讀取服務帳戶金鑰的 JSON 字串
    # 這是部署到 Vercel 時最安全和推薦的方式
    service_account_json_str = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')

    if service_account_json_str:
        # 解析 JSON 字串為 Python 字典
        service_account_info = json.loads(service_account_json_str)
        cred = credentials.Certificate(service_account_info)


    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase app initialized and Firestore client created.")

except (ValueError, json.JSONDecodeError, firebase_exceptions.FirebaseError) as e:
    logger.critical(f"FATAL ERROR: Could not initialize Firebase or Firestore: {e}")
    # 在實際生產應用中，這裡可能需要更完善的錯誤處理，例如發送警報或讓應用程式終止

# --- 設定檔載入與儲存函數，現在與 Firestore 互動 ---

def load_config():
    """
    從 Firestore 載入設定檔。
    如果 Firestore 未成功初始化，或載入失敗，則返回一個帶有空列表的預設配置。
    """
    if db is None:
        logger.error("Firestore client is not initialized. Cannot load config from Firestore. Returning default.")
        return {"exam_date": None, "registered_users": [], "registered_groups": []}

    # 指定 Firestore 中的 collection 和 document 名稱
    config_ref = db.collection('line_bot_configs').document('main_config')
    try:
        doc = config_ref.get()
        if doc.exists:
            config = doc.to_dict()
            # 確保這些鍵存在且是列表類型
            if "registered_users" not in config or not isinstance(config["registered_users"], list):
                config["registered_users"] = []
            if "registered_groups" not in config or not isinstance(config["registered_groups"], list):
                config["registered_groups"] = []
            logger.info("Config loaded from Firestore.")
            return config
        else:
            logger.warning("No 'main_config' document found in Firestore. Creating a new one.")
            # 如果文件不存在，創建一個初始配置並儲存
            initial_config = {"exam_date": None, "registered_users": [], "registered_groups": []}
            config_ref.set(initial_config)
            return initial_config
    except Exception as e:
        logger.error(f"Error loading config from Firestore: {e}. Returning default config.")
        return {"exam_date": None, "registered_users": [], "registered_groups": []}

def save_config(config):
    """
    將設定儲存到 Firestore。
    """
    if db is None:
        logger.error("Firestore client is not initialized. Cannot save config to Firestore.")
        return

    config_ref = db.collection('line_bot_configs').document('main_config')
    try:
        config_ref.set(config)
        logger.info("Config saved to Firestore.")
    except Exception as e:
        logger.error(f"Error saving config to Firestore: {e}")

# --- 後續函數邏輯不變，因為它們會呼叫 load_config 和 save_config ---

def get_countdown_message():
    """
    計算距離設定的考試日期剩餘天數並生成對應的訊息。
    根據不同的天數範圍，返回不同的鼓勵或提醒訊息。
    """
    config = load_config()
    exam_date_str = config.get("exam_date")

    if not exam_date_str:
        return "很抱歉，尚未設定考試日期。請輸入'設定考試日期YYYY-MM-DD'來設定。"

    try:
        exam_date = datetime.strptime(exam_date_str, "%Y-%m-%d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        time_left = exam_date - today
        days_left = time_left.days

        if days_left > 0:
            message = ""
            if days_left == 100:
                message = f"⌛時光飛逝，你只剩下{days_left} 天，趕快拿起書本來�📚"
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

def set_exam_date(date_str):
    """
    設定考試日期並儲存到 Firestore。
    """
    config = load_config()
    config["exam_date"] = date_str
    save_config(config)

# --- APScheduler 設定與排程任務 ---
scheduler = BackgroundScheduler(timezone="Asia/Taipei")

def send_daily_countdown_message_job():
    """
    排程任務：每天定時發送倒數訊息。
    """
    logger.info("Executing daily countdown message task...")
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        countdown_message_text = get_countdown_message()

        config = load_config()
        registered_users = config.get("registered_users", [])
        registered_groups = config.get("registered_groups", [])

        messages_to_send = [TextMessage(text=countdown_message_text)]

        if registered_users:
            for user_id in registered_users:
                try:
                    line_bot_api.push_message(PushMessageRequest(
                        to=user_id,
                        messages=messages_to_send
                    ))
                    logger.info(f"Successfully pushed countdown message to user: {user_id}")
                except Exception as e:
                    logger.error(f"Failed to push countdown message to user {user_id}: {e}")
        else:
            logger.warning("No registered users found for scheduled push.")

        if registered_groups:
            for group_id in registered_groups:
                try:
                    line_bot_api.push_message(PushMessageRequest(
                        to=group_id,
                        messages=messages_to_send
                    ))
                    logger.info(f"Successfully pushed countdown message to group: {group_id}")
                except Exception as e:
                    logger.error(f"Failed to push countdown message to group {group_id}: {e}")
        else:
            logger.warning("No registered groups found for scheduled push.")

scheduler_started = False

@app.before_request
def start_scheduler_if_not_started():
    global scheduler_started
    if not scheduler_started:
        # 在這裡檢查 db 是否已成功初始化，如果沒有，則不啟動排程器
        if db is None:
            logger.error("Firestore client not available. Scheduler will not be started.")
            return

        # 每天早上 9 點 00 分發送訊息
        scheduler.add_job(
            send_daily_countdown_message_job,
            CronTrigger(hour=9, minute=0, timezone="Asia/Taipei"),
            id='daily_countdown',
            replace_existing=True
        )
        scheduler.start()
        scheduler_started = True
        logger.info("Scheduler started and daily countdown job added for 9:00 AM Taipei time.")

atexit.register(lambda: scheduler.shutdown())







@app.route("/callback", methods=['POST'])
def callback():
    """
    LINE Bot 的 Webhook 回調入口。
    接收來自 LINE 平台的事件，並交由 line_handler 處理。
    """
    # 取得 X-Line-Signature 標頭值，用於驗證請求的來源
    signature = request.headers['X-Line-Signature']

    # 取得請求主體（body）作為文字
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # 處理 Webhook 主體
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        # 如果簽名無效，表示請求不是來自 LINE 平台或 Channel Secret 不正確
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400) # 返回 400 Bad Request 錯誤

    return 'OK' # 成功處理後返回 'OK'

@line_handler.add(FollowEvent)
def handle_follow(event):
    """
    處理使用者加入好友事件。
    當 Bot 被用戶加為好友時觸發。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_id = event.source.user_id # 取得加好友的用戶 ID

        # 將用戶 ID 儲存起來，未來可以用於主動推播訊息
        config = load_config()
        config["last_active_user_id"] = user_id
        save_config(config)

        # 回覆歡迎訊息給新加入的用戶
        messages = [TextMessage(text="哈囉！謝謝你加入這個倒數計時小幫手😎！\n\n🍊你可以輸入: \n【設定考試日期 YYYY-MM-DD】來設定你的重要日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n準備好了嗎？我們一起努力！\nd(`･∀･)b"),
                    StickerMessage(package_id='11538', sticker_id='51626494')
                     ]

        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages
            )
        )
        logger.info(f"User {user_id} followed the bot.")

@line_handler.add(JoinEvent)
def handle_join(event):
    """
    處理 Bot 加入群組或聊天室事件。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if event.source.type == "group":
            group_id = event.source.group_id # 取得 Bot 所在群組的 ID
            # 將群組 ID 儲存起來，未來可以用於主動推播訊息到這個群組
            config = load_config()
            config["last_active_group_id"] = group_id
            save_config(config)
            logger.info(f"Bot joined Group ID: {group_id}")
            # 發送群組歡迎訊息
            messages = [TextMessage(text = "哈囉！大家好！\n我是你們的倒數計時小幫手😎，很高興加入這個群組！\n\n🍊群組裡面的任何一位成員都可以輸入【設定考試日期 YYYY-MM-DD】來設定日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入【查詢剩餘天數】就能知道距離考試還有多久喔！\n\n讓我們一起為目標衝刺吧！\nd(`･∀･)b"),
                        StickerMessage(package_id='11538', sticker_id='51626494')
            ]
            
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
        # 您也可以在這裡處理 "room" 類型的 JoinEvent，如果您的 Bot 會被加入聊天室

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    處理接收到的文字訊息事件。
    根據用戶發送的指令進行不同的操作。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        text = event.message.text # 取得用戶發送的文字訊息

        # 處理 "設定考試日期 YYYY-MM-DD" 指令
        if text.startswith("設定考試日期"):
            parts = text.split()
            if len(parts) == 2: # 檢查指令格式是否為 "設定考試日期 日期"
                date_str = parts[1]
                try:
                    # 嘗試解析日期字串，驗證格式是否正確
                    datetime.strptime(date_str, "%Y-%m-%d")
                    set_exam_date(date_str) # 設定考試日期
                    # 回覆用戶設定成功的訊息
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"考試日期已設定為：{date_str}")]
                        )
                    )
                except ValueError:
                    # 如果日期格式不正確，回覆錯誤訊息
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="日期格式不正確，請使用 YYYY-MM-DD，例如：設定考試日期 2025-10-26")]
                        )
                    )
            else:
                # 如果指令格式不正確，回覆正確的用法
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入正確的指令格式：設定考試日期 YYYY-MM-DD")]
                    )
                )

        # 處理 "查詢剩餘天數" 指令
        elif text == "查詢剩餘天數":
            messages = get_countdown_message() # 取得倒數訊息
            # 回覆用戶剩餘天數訊息
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=messages)]
                )
            )



# 程式的入口點，當直接執行此腳本時會啟動 Flask 應用
if __name__ == "__main__":
    # 在開發環境中，可以使用 debug=True 來自動重新載入並提供詳細錯誤訊息
    # 在生產環境中，應將 debug 設定為 False，並使用 Gunicorn 等 WSGI 伺服器
    app.run(debug=True)
