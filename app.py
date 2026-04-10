import os
import json
import logging
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# =========================
# 基本設定
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.warning("LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET 尚未設定")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DATA_FILE = Path("user_stocks.json")


# =========================
# 資料存取
# =========================
def load_data() -> Dict[str, List[str]]:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception as e:
        logger.error(f"讀取 user_stocks.json 失敗: {e}")
        return {}


def save_data(data: Dict[str, List[str]]) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入 user_stocks.json 失敗: {e}")


def get_user_stocks(user_id: str) -> List[str]:
    data = load_data()
    return data.get(user_id, [])


def add_user_stock(user_id: str, stock_no: str) -> str:
    data = load_data()
    stocks = data.get(user_id, [])

    if stock_no in stocks:
        return f"股票 {stock_no} 已經在你的自選清單裡了"

    stocks.append(stock_no)
    data[user_id] = stocks
    save_data(data)
    return f"已新增股票 {stock_no}"


def remove_user_stock(user_id: str, stock_no: str) -> str:
    data = load_data()
    stocks = data.get(user_id, [])

    if stock_no not in stocks:
        return f"你的自選清單沒有 {stock_no}"

    stocks.remove(stock_no)
    data[user_id] = stocks
    save_data(data)
    return f"已刪除股票 {stock_no}"


# =========================
# 假資料報告
# 先讓機器人能正常回覆，之後再接你的股票分析邏輯
# =========================
def build_today_report(user_id: str) -> str:
    stocks = get_user_stocks(user_id)

    if not stocks:
        return (
            "📊 今日報告\n\n"
            "你目前還沒有自選股票。\n"
            "請輸入：\n"
            "新增股票 2330\n"
            "來加入你的自選股。"
        )

    lines = []
    lines.append("📊 今日報告")
    lines.append("")
    lines.append("👀 你的自選股票：")

    for s in stocks:
        lines.append(f"📌 {s} 重點：")
        lines.append("收盤價：暫無資料")
        lines.append("近30天平均價：暫無資料")
        lines.append("近300天平均價：暫無資料")
        lines.append("與30日均差距：暫無資料")
        lines.append("與300日均差距：暫無資料")
        lines.append("")

    lines.append("可用指令：")
    lines.append("新增股票 2330")
    lines.append("刪除股票 2330")
    lines.append("我的股票")
    lines.append("今日報告")
    lines.append("查ID")
    lines.append("狀態")

    return "\n".join(lines)


def build_stock_list_text(user_id: str) -> str:
    stocks = get_user_stocks(user_id)
    if not stocks:
        return "你目前沒有自選股票，可輸入：新增股票 2330"

    text = "📋 你的自選股票：\n"
    text += "\n".join([f"- {s}" for s in stocks])
    return text


# =========================
# 健康檢查
# =========================
@app.get("/")
async def home():
    return {"status": "ok", "message": "AI Stock Bot is running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


# =========================
# Webhook
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        raise HTTPException(status_code=500, detail="LINE 環境變數未設定")

    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")

    logger.info(f"Webhook body: {body_text}")

    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.exception(f"Webhook 發生錯誤: {e}")
        raise HTTPException(status_code=500, detail="Webhook error")

    return JSONResponse(content={"status": "ok"})


# =========================
# LINE 訊息處理
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    try:
        user_id = event.source.user_id if event.source and event.source.user_id else "unknown_user"
        text = event.message.text.strip()

        logger.info(f"收到訊息 user_id={user_id}, text={text}")

        reply_text = ""

        if text == "查ID":
            reply_text = f"你的 User ID：\n{user_id}"

        elif text == "狀態":
            reply_text = "✅ 機器人目前正常運作中"

        elif text == "我的股票":
            reply_text = build_stock_list_text(user_id)

        elif text == "今日報告":
            reply_text = build_today_report(user_id)

        elif text.startswith("新增股票"):
            parts = text.split()
            if len(parts) != 2:
                reply_text = "格式錯誤，請輸入：新增股票 2330"
            else:
                stock_no = parts[1].strip()
                if not stock_no.isdigit():
                    reply_text = "股票代碼格式錯誤，請輸入數字，例如：新增股票 2330"
                else:
                    reply_text = add_user_stock(user_id, stock_no)

        elif text.startswith("刪除股票"):
            parts = text.split()
            if len(parts) != 2:
                reply_text = "格式錯誤，請輸入：刪除股票 2330"
            else:
                stock_no = parts[1].strip()
                if not stock_no.isdigit():
                    reply_text = "股票代碼格式錯誤，請輸入數字，例如：刪除股票 2330"
                else:
                    reply_text = remove_user_stock(user_id, stock_no)

        else:
            reply_text = (
                "你可以使用以下指令：\n"
                "新增股票 2330\n"
                "刪除股票 2330\n"
                "我的股票\n"
                "今日報告\n"
                "查ID\n"
                "狀態"
            )

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )

    except Exception as e:
        logger.exception(f"handle_message 發生錯誤: {e}")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="系統暫時發生錯誤，請稍後再試")
            )
        except Exception as inner_e:
            logger.exception(f"回覆錯誤訊息也失敗: {inner_e}")