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
# 這些變數需要在您的部署環境中設定 (例如 Vercel 的環境變數)
configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# --- Firebase 初始化 ---
db = None # 初始化 Firestore 客戶端為 None
cred = None # **新增**：初始化 cred 為 None，確保它在 try 塊之外也定義

try:
    # 嘗試從環境變數 FIREBASE_SERVICE_ACCOUNT_KEY_JSON 讀取服務帳戶金鑰的 JSON 字串
    # 這是部署到 Vercel 時最安全和推薦的方式
    service_account_json_str = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_JSON')

    if service_account_json_str:
        # 解析 JSON 字串為 Python 字典
        service_account_info = json.loads(service_account_json_str)
        cred = credentials.Certificate(service_account_info)
    else:
        # **修正**：如果環境變數未設定，明確拋出錯誤，避免 NameError。
        # 為了生產環境的清晰度，這裡移除了本地檔案路徑的備用邏輯。
        # 如果您需要在本地測試，建議在本地環境設定相同的環境變數。
        raise ValueError(
            "FIREBASE_SERVICE_ACCOUNT_KEY_JSON environment variable is not set. "
            "Firebase initialization requires this for production deployments. "
            "For local testing, please set this environment variable."
        )

    # 初始化 Firebase 應用程式
    firebase_admin.initialize_app(cred)
    # 取得 Firestore 客戶端實例
    db = firestore.client()
    logger.info("Firebase app initialized and Firestore client created.")

except (ValueError, json.JSONDecodeError, firebase_exceptions.FirebaseError, Exception) as e:
    # 捕獲所有可能的初始化錯誤，並記錄為關鍵錯誤
    logger.critical(f"FATAL ERROR: Could not initialize Firebase or Firestore: {e}")
    # 確保 db 在初始化失敗時是 None，這樣後續的資料庫操作會被跳過
    db = None

# --- 設定檔載入與儲存函數，現在與 Firestore 互動 ---

def load_config():
    """
    從 Firestore 載入設定檔。
    如果 Firestore 未成功初始化，或載入失敗，則返回一個帶有預設值的字典。
    """
    if db is None:
        logger.error("Firestore client is not initialized. Cannot load config from Firestore. Returning default empty config.")
        # 如果 Firestore 未初始化，返回一個預設的空配置
        return {"exam_date": None, "registered_users": [], "registered_groups": []}

    # 指定 Firestore 中的 collection 和 document 名稱
    config_ref = db.collection('line_bot_configs').document('main_config')
    try:
        doc = config_ref.get() # 嘗試從 Firestore 獲取文件
        if doc.exists:
            # 如果文件存在，將其轉換為字典
            config = doc.to_dict()
            # 確保 'registered_users' 和 'registered_groups' 鍵存在且是列表類型
            if "registered_users" not in config or not isinstance(config["registered_users"], list):
                config["registered_users"] = []
            if "registered_groups" not in config or not isinstance(config["registered_groups"], list):
                config["registered_groups"] = []
            logger.info("Config loaded from Firestore.")
            return config
        else:
            # 如果文件不存在，記錄警告並創建一個新的初始配置
            logger.warning("No 'main_config' document found in Firestore. Creating a new one.")
            initial_config = {"exam_date": None, "registered_users": [], "registered_groups": []}
            # 將初始配置儲存到 Firestore
            config_ref.set(initial_config)
            return initial_config
    except Exception as e:
        # 捕獲從 Firestore 載入時可能發生的任何異常
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
        # 將整個配置字典儲存到 Firestore
        config_ref.set(config)
        logger.info("Config saved to Firestore.")
    except Exception as e:
        logger.error(f"Error saving config to Firestore: {e}")

# --- 訊息生成函數 ---
def get_countdown_message():
    """
    計算距離設定的考試日期剩餘天數並生成對應的訊息。
    根據不同的天數範圍，返回不同的鼓勵或提醒訊息。
    """
    config = load_config() # 從 Firestore 載入最新配置
    exam_date_str = config.get("exam_date")

    # 如果沒有設定考試日期，返回提示訊息
    if not exam_date_str:
        return "很抱歉，尚未設定考試日期。請輸入'設定考試日期YYYY-MM-DD'來設定。"

    try:
        # 將儲存的日期字串轉換為 datetime 物件
        exam_date = datetime.strptime(exam_date_str, "%Y-%m-%d")
        # 取得今天的日期，並將時間部分歸零，確保天數計算的準確性
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # 計算日期差
        time_left = exam_date - today
        days_left = time_left.days

        # 根據剩餘天數生成不同的訊息
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
        # 如果日期格式錯誤，返回錯誤訊息
        return "考試日期格式錯誤，請檢查設定或重新設定。正確格式為YYYY-MM-DD。"

def set_exam_date(date_str):
    """
    設定考試日期並儲存到 Firestore。
    """
    config = load_config() # 從 Firestore 載入最新配置
    config["exam_date"] = date_str # 更新考試日期
    save_config(config) # 保存更新後的配置到 Firestore

# --- APScheduler 設定與排程任務 ---
scheduler = BackgroundScheduler(timezone="Asia/Taipei") # 設定排程器時區為台北

def send_daily_countdown_message_job():
    """
    排程任務：每天定時發送倒數訊息。
    此函數將讀取設定的考試日期，計算剩餘天數，
    並將訊息推播給儲存在 Firestore 中的所有註冊用戶和群組。
    """
    logger.info("Executing daily countdown message task...")
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        countdown_message_text = get_countdown_message() # 呼叫現有的函數來生成訊息

        config = load_config() # 從 Firestore 載入最新配置，確保獲取所有註冊用戶和群組
        registered_users = config.get("registered_users", [])
        registered_groups = config.get("registered_groups", [])

        messages_to_send = [TextMessage(text=countdown_message_text)]

        # 遍歷所有註冊的用戶並推播訊息
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

        # 遍歷所有註冊的群組並推播訊息
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

scheduler_started = False # 旗標，用於確保排程器只啟動一次

# 使用 @app.before_request 裝飾器，確保排程器在應用程式處理第一個請求前啟動
@app.before_request
def start_scheduler_if_not_started():
    global scheduler_started
    if not scheduler_started:
        # 在啟動排程器前，檢查 Firestore 客戶端是否已成功初始化
        if db is None:
            logger.error("Firestore client not available. Scheduler will not be started.")
            return

        # 添加每天早上 7 點 00 分發送訊息的任務
        scheduler.add_job(
            send_daily_countdown_message_job,
            CronTrigger(hour=21, minute=10, timezone="Asia/Taipei"), # 設定為台北時間早上 7 點
            id='daily_countdown', # 給任務一個唯一的 ID
            replace_existing=True # 如果任務已存在，則替換它
        )
        scheduler.start() # 啟動排程器
        scheduler_started = True # 將旗標設為 True，防止重複啟動
        logger.info("Scheduler started and daily countdown job added for 7:00 AM Taipei time.")

# 註冊一個在 Python 直譯器關閉時執行的函數，用於關閉排程器
# 這有助於應用程式的正常終止
atexit.register(lambda: scheduler.shutdown())

# --- LINE Bot Webhook 回調入口 ---
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

# 例如在檔案中的 @app.route("/callback") 附近新增
@app.route("/wakeup", methods=['GET'])
def wakeup():
    logger.info("Wakeup endpoint hit by external service.")
    return 'OK', 200

# --- LINE 訊息事件處理 ---
@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """
    處理接收到的文字訊息事件。
    根據用戶發送的指令進行不同的操作。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        text = event.message.text # 取得用戶發送的文字訊息

        # 處理 "設定考試日期YYYY-MM-DD" 指令
        if text.startswith("設定考試日期"):
            parts = text.split()
            if len(parts) == 2: # 檢查指令格式是否為 "設定考試日期 日期"
                date_str = parts[1]
                try:
                    datetime.strptime(date_str, "%Y-%m-%d") # 驗證日期格式
                    set_exam_date(date_str) # 設定考試日期到 Firestore
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
                            messages=[TextMessage(text="日期格式不正確，請使用YYYY-MM-DD，例如：設定考試日期 2025-10-26")]
                        )
                    )
            else:
                # 如果指令格式不正確，回覆正確的用法
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入正確的指令格式：設定考試日期YYYY-MM-DD")]
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

# --- LINE Follow 事件處理 ---
@line_handler.add(FollowEvent)
def handle_follow(event):
    """
    處理使用者加入好友事件。
    當 Bot 被用戶加為好友時觸發。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_id = event.source.user_id # 取得加好友的用戶 ID

        config = load_config() # 從 Firestore 載入最新配置
        # 確保將 user_id 添加到 registered_users 列表中，且避免重複
        if user_id not in config.get("registered_users", []): # 使用 .get() 確保鍵存在
            config["registered_users"].append(user_id)
            save_config(config) # 保存更新後的配置到 Firestore
            logger.info(f"User {user_id} added to registered_users in Firestore.")
        else:
            logger.info(f"User {user_id} already in registered_users.")

        # 回覆歡迎訊息給新加入的用戶，包含貼圖
        messages = [TextMessage(text="哈囉！謝謝你加入這個倒數計時小幫手😎！\n\n🍊你可以輸入: \n【設定考試日期YYYY-MM-DD】來設定你的重要日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n準備好了嗎？我們一起努力！\nd(`･∀･)b"),
                    StickerMessage(package_id='11538', sticker_id='51626494') # 貼圖 ID
                   ]

        try: # **新增**：將 reply_message_with_http_info 包裹在 try-except 塊中
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages
                )
            )
            logger.info(f"Welcome message sent to user {user_id}.")
        except Exception as e:
            logger.error(f"Failed to send welcome message to user {user_id}: {e}")

# --- LINE Join 事件處理 ---
@line_handler.add(JoinEvent)
def handle_join(event):
    """
    處理 Bot 加入群組或聊天室事件。
    """
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if event.source.type == "group":
            group_id = event.source.group_id # 取得 Bot 所在群組的 ID
            
            config = load_config() # 從 Firestore 載入最新配置
            # 確保將 group_id 添加到 registered_groups 列表中，且避免重複
            if group_id not in config.get("registered_groups", []): # 使用 .get() 確保鍵存在
                config["registered_groups"].append(group_id)
                save_config(config) # 保存更新後的配置到 Firestore
                logger.info(f"Bot joined Group ID: {group_id} and added to registered_groups in Firestore.")
            else:
                logger.info(f"Group ID: {group_id} already in registered_groups.")

            # 發送群組歡迎訊息，包含貼圖
            messages = [TextMessage(text = "哈囉！大家好！\n我是你們的倒數計時小幫手😎，很高興加入這個群組！\n\n🍊群組裡面的任何一位成員都可以輸入【設定考試日期YYYY-MM-DD】來設定日期\n\n例如：\n'設定考試日期 2025-10-26'\n\n🍊隨時輸入【查詢剩餘天數】就能知道距離考試還有多久喔！\n\n讓我們一起為目標衝刺吧！\nd(`･∀･)b"),
                        StickerMessage(package_id='11538', sticker_id='51626494') # 貼圖 ID
                       ]

            try: # **新增**：將 reply_message_with_http_info 包裹在 try-except 塊中
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=messages
                    )
                )
                logger.info(f"Welcome message sent to group {group_id}.")
            except Exception as e:
                logger.error(f"Failed to send welcome message to group {group_id}: {e}")

        # 您也可以在這裡處理 "room" 類型的 JoinEvent

# --- 程式的入口點 ---
if __name__ == "__main__":
    # 在開發環境中，可以使用 debug=True 來自動重新載入並提供詳細錯誤訊息
    # 在生產環境中，應將 debug 設定為 False，並使用 Gunicorn 等 WSGI 伺服器
    app.run(debug=True)
