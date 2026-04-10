import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
import twstock
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

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
# 使用者資料
# =========================
def load_data() -> Dict[str, List[str]]:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.exception(f"讀取 user_stocks.json 失敗: {e}")
        return {}


def save_data(data: Dict[str, List[str]]) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"寫入 user_stocks.json 失敗: {e}")


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
# 股票名稱
# =========================
def get_stock_name(stock_no: str) -> str:
    try:
        if stock_no in twstock.codes:
            return twstock.codes[stock_no].name
    except Exception:
        pass
    return stock_no


# =========================
# 股票資料
# =========================
def fetch_from_twstock(stock_no: str) -> Optional[dict]:
    """
    優先用 twstock 抓台股 / ETF
    """
    try:
        stock = twstock.Stock(stock_no)
        data = stock.fetch_from(2024, 1)  # 抓久一點，方便算200日均線

        if not data:
            return None

        closes = []
        dates = []

        for item in data:
            if item.close is not None:
                closes.append(float(item.close))
                dates.append(item.date)

        if not closes:
            return None

        close_series = pd.Series(closes, dtype="float64")
        close_price = float(close_series.iloc[-1])
        trade_date = dates[-1].strftime("%Y-%m-%d")

        ma30 = close_series.rolling(30).mean().iloc[-1] if len(close_series) >= 30 else None
        ma200 = close_series.rolling(200).mean().iloc[-1] if len(close_series) >= 200 else None

        diff30 = ((close_price - ma30) / ma30 * 100) if pd.notna(ma30) and ma30 != 0 else None
        diff200 = ((close_price - ma200) / ma200 * 100) if pd.notna(ma200) and ma200 != 0 else None

        return {
            "trade_date": trade_date,
            "close": round(close_price, 2),
            "ma30": round(float(ma30), 2) if pd.notna(ma30) else None,
            "ma200": round(float(ma200), 2) if pd.notna(ma200) else None,
            "diff30": round(float(diff30), 2) if diff30 is not None else None,
            "diff200": round(float(diff200), 2) if diff200 is not None else None,
            "source": "twstock",
        }

    except Exception as e:
        logger.exception(f"twstock 抓取失敗 {stock_no}: {e}")
        return None


def fetch_from_yfinance(stock_no: str) -> Optional[dict]:
    """
    twstock 抓不到時，再用 yfinance 補
    """
    candidates = [f"{stock_no}.TW", f"{stock_no}.TWO"]

    for symbol in candidates:
        try:
            df = yf.download(
                symbol,
                period="400d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )

            if df is None or df.empty:
                continue

            if "Close" not in df.columns:
                continue

            close_col = df["Close"]
            if isinstance(close_col, pd.DataFrame):
                close_col = close_col.iloc[:, 0]

            close_col = close_col.dropna()
            if close_col.empty:
                continue

            close_price = float(close_col.iloc[-1])

            ma30 = close_col.rolling(30).mean().iloc[-1] if len(close_col) >= 30 else None
            ma200 = close_col.rolling(200).mean().iloc[-1] if len(close_col) >= 200 else None

            diff30 = ((close_price - ma30) / ma30 * 100) if pd.notna(ma30) and ma30 != 0 else None
            diff200 = ((close_price - ma200) / ma200 * 100) if pd.notna(ma200) and ma200 != 0 else None

            trade_date = str(close_col.index[-1])[:10]

            return {
                "trade_date": trade_date,
                "close": round(close_price, 2),
                "ma30": round(float(ma30), 2) if pd.notna(ma30) else None,
                "ma200": round(float(ma200), 2) if pd.notna(ma200) else None,
                "diff30": round(float(diff30), 2) if diff30 is not None else None,
                "diff200": round(float(diff200), 2) if diff200 is not None else None,
                "source": "yfinance",
            }

        except Exception as e:
            logger.exception(f"yfinance 抓取失敗 {stock_no} {symbol}: {e}")

    return None


def fetch_stock_data(stock_no: str) -> dict:
    data = fetch_from_twstock(stock_no)
    if data:
        return data

    data = fetch_from_yfinance(stock_no)
    if data:
        return data

    raise ValueError(f"抓不到股票 {stock_no} 的資料")


# =========================
# 推薦邏輯
# =========================
def recommendation_text(diff30: Optional[float], diff200: Optional[float]) -> Tuple[str, str]:
    if diff30 is None:
        return "⚪ 暫無法判斷", "資料不足"

    if diff30 < -8:
        return "🟢 可留意", "價格低於30日均線較多，可觀察是否止跌"

    if -8 <= diff30 <= 3:
        if diff200 is not None and diff200 < 15:
            return "🟢 可小量布局", "價格接近均線附近，可小量定期投入"
        return "🟡 可觀察", "短線接近30日均線，但離長均線仍有距離"

    if 3 < diff30 <= 10:
        return "🟡 不宜追高", "價格高於30日均線，建議等拉回"

    return "🔴 偏熱", "價格離30日均線過遠，短線不建議追價"


def format_stock_report(stock_no: str) -> str:
    try:
        data = fetch_stock_data(stock_no)
        stock_name = get_stock_name(stock_no)
        suggest, reason = recommendation_text(data["diff30"], data["diff200"])

        lines = []
        lines.append(f"📌 {stock_no} {stock_name} 重點：")
        lines.append(f"最近交易日收盤價：{data['close']}")
        lines.append(f"交易日：{data['trade_date']}")
        lines.append(f"近30天平均價：{data['ma30'] if data['ma30'] is not None else '暫無資料'}")
        lines.append(f"近200天平均價：{data['ma200'] if data['ma200'] is not None else '暫無資料'}")
        lines.append(f"與30日均差距：{str(data['diff30']) + '%' if data['diff30'] is not None else '暫無資料'}")
        lines.append(f"與200日均差距：{str(data['diff200']) + '%' if data['diff200'] is not None else '暫無資料'}")
        lines.append(f"是否建議加碼：{suggest}")
        lines.append(f"原因：{reason}")
        lines.append(f"資料來源：{data['source']}")
        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"format_stock_report 失敗 stock_no={stock_no}, error={e}")
        return f"📌 {stock_no} 重點：\n資料取得失敗：{e}"


def build_market_summary() -> str:
    lines = []
    lines.append("📊 每日投資提醒")
    lines.append("")
    lines.append("⚠️ 提醒：以下價格為最近交易日收盤資料，不一定是當下即時價。")
    return "\n".join(lines)


def build_today_report(user_id: str) -> str:
    stocks = get_user_stocks(user_id)

    lines = []
    lines.append(build_market_summary())
    lines.append("")

    if stocks:
        lines.append("👀 你的自選股票：")
        for stock_no in stocks:
            lines.append(format_stock_report(stock_no))
            lines.append("")
    else:
        lines.append("你目前還沒有自選股票。")
        lines.append("可輸入：新增股票 2330")
        lines.append("")

    lines.append("可用指令：")
    lines.append("新增股票 2330")
    lines.append("刪除股票 2330")
    lines.append("我的股票")
    lines.append("今日報告")
    lines.append("查ID")
    lines.append("狀態")

    return "\n".join(lines).strip()


def build_stock_list_text(user_id: str) -> str:
    stocks = get_user_stocks(user_id)
    if not stocks:
        return "你目前沒有自選股票，可輸入：新增股票 2330"

    lines = ["📋 你的自選股票："]
    for s in stocks:
        lines.append(f"- {s} {get_stock_name(s)}")
    return "\n".join(lines)


# =========================
# FastAPI routes
# =========================
@app.get("/")
async def home():
    return {"status": "ok", "message": "AI Stock Bot is running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


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
# LINE message handler
# =========================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_id = "unknown_user"
    text = ""

    try:
        user_id = event.source.user_id if event.source and event.source.user_id else "unknown_user"
        text = event.message.text.strip()

        logger.info(f"收到訊息 user_id={user_id}, text={text}")

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

        logger.info(f"準備回覆: {reply_text[:1000]}")

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )

        logger.info("reply_message 成功")

    except Exception as e:
        logger.exception(f"handle_message 發生錯誤, text={text}, user_id={user_id}, error={e}")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="系統暫時發生錯誤，請稍後再試")
            )
        except Exception as inner_e:
            logger.exception(f"回覆錯誤訊息也失敗: {inner_e}")