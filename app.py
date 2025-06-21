import os
from datetime import datetime
import json
import logging
from flask import Flask, request, abort

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




configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN') )
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# --- 倒數日期與事件儲存設定 ---
# 用於儲存設定檔的檔案路徑
CONFIG_FILE = "config.json"

def load_config():
    """載入設定檔 (包含日期和群組ID)。"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {} # 檔案內容無效，返回空字典
    return {}

def save_config(config):
    """儲存設定檔。"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

def get_countdown_message():
    """
    計算距離日期剩餘天數並生成訊息。
    """
    config = load_config()
    exam_date_str = config.get("exam_date")

    if not exam_date_str:
        return "很抱歉，尚未設定考試日期。請輸入'設定考試日期 YYYY-MM-DD'來設定。"

    try:
        exam_date = datetime.strptime(exam_date_str, "%Y-%m-%d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        time_left = exam_date - today
        days_left = time_left.days

        if days_left > 0: 
            if days_left > 51:
                if days_left == 100:
                    message = f"⌛時光飛逝，你只剩下{days_left} 天，趕快拿起書本來"
                elif days_left == 90:
                    message = f"沒想到已經剩下{days_left} 天\n｡ﾟヽ(ﾟ´Д`)ﾉﾟ｡時間都在我的睡夢中流失了！"
            elif days_left < 50:
                if days_left == 30:
                    message = f"距離考試只剩下 {days_left} 天！\n祝你考試像打遊戲一樣，一路都是暴擊，分數直接爆表！🔥🔥"
                elif days_left ==10:
                    message = f"距離考試只剩下 {days_left} 天！\n祝你考試像吃雞腿一樣，輕鬆又美味，分數高高🍗"
            else:
                message = f"你今天讀書了嗎？\n距離考試只剩下 {days_left} 天！加油！💪"
        elif days_left == 0:
            message = f"你今天讀書了嗎？\n今天是考試的日子🏆金榜題名🏆"
        else:
            message = f"考試 ({exam_date_str}) 已經在 {abs(days_left)} 天前結束了。期待你下次的挑戰！"
        return message
    except ValueError:
        return "考試日期格式錯誤，請檢查設定或重新設定。正確格式為 YYYY-MM-DD。"

def set_exam_date(date_str):
    """設定考試日期並儲存。"""
    config = load_config()
    config["exam_date"] = date_str
    save_config(config)

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        text = event.message.text

        if text.startswith("設定考試日期"):
            parts = text.split()
            if len(parts) == 2:
                date_str = parts[1]
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                    set_exam_date(date_str)
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=f"考試日期已設定為：{date_str}")]
                        )
                    )
                except ValueError:
                    line_bot_api.reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text="日期格式不正確，請使用 YYYY-MM-DD，例如：設定考試日期 2025-10-26")]
                        )
                    )
            else:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入正確的指令格式：設定考試日期 YYYY-MM-DD")]
                    )
                )

        elif text == "查詢剩餘天數":
            messages = get_countdown_message()
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=messages)]
                )
            )


@line_handler.add(FollowEvent)
def handle_follow(event):
    """處理使用者加入好友事件"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_id = event.source.user_id

        # 你可以在這裡儲存 user_id 到你的 config.json 或資料庫，以便後續推播
        config = load_config()
        config["last_active_user_id"] = user_id
        save_config(config)

        # 回覆歡迎訊息
        welcome_message = "哈囉！謝謝你加入這個倒數計時小幫手😎！\n\n你可以輸入: \n'設定考試日期 YYYY-MM-DD' 來設定你的重要日期，例如：'設定考試日期 2025-10-26'\n\n隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n準備好了嗎？我們一起努力！d(`･∀･)b"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=welcome_message)]
            )
        )
        logger.info(f"User {user_id} followed the bot.")

@line_handler.add(JoinEvent)
def handle_join(event):
    """處理 Bot 加入群組或聊天室事件"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        if event.source.type == "group":
            group_id = event.source.group_id
            # 儲存 group_id，以便後續推播到群組
            config = load_config()
            config["last_active_group_id"] = group_id
            save_config(config)
            logger.info(f"Bot joined Group ID: {group_id}")
            # 發送群組歡迎訊息
            welcome_message = "哈囉！大家好！\n我是你們的倒數計時小幫手😎，很高興加入這個群組！\n\n群組裡面的任何一位成員都可以輸入 '設定考試日期 YYYY-MM-DD' 來設定共同的考試日期，例如：'設定考試日期 2025-10-26'\n\n隨時輸入 '查詢剩餘天數' 就能知道距離考試還有多久喔！\n\n讓我們一起為目標衝刺吧！d(`･∀･)b"
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=welcome_message)]
                )
            )

if __name__ == "__main__":
    app.run(debug=True)