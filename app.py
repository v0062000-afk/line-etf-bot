import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Request
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
# 使用者資料存取
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
# 股票資料
# =========================
def normalize_to_taipei_index(df: pd.DataFrame) -> pd.DataFrame:
    """
    將 yfinance 回傳的 index 統一轉成 Asia/Taipei，避免日期少一天。
    """
    if df.empty:
        return df

    idx = pd.to_datetime(df.index)

    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC").tz_convert("Asia/Taipei")
    else:
        idx = idx.tz_convert("Asia/Taipei")

    df = df.copy()
    df.index = idx
    return df


def pick_scalar(value) -> Optional[float]:
    """
    處理 yfinance 偶爾回傳 Series / ndarray / scalar 的情況，只取最後一個值。
    """
    try:
        if isinstance(value, pd.Series):
            value = value.iloc[-1]
        elif isinstance(value, pd.DataFrame):
            value = value.iloc[-1, -1]

        if hasattr(value, "item"):
            try:
                value = value.item()
            except Exception:
                pass

        if pd.isna(value):
            return None

        return float(value)
    except Exception:
        return None


def download_stock_df(symbol: str) -> pd.DataFrame:
    """
    下載日K資料。
    """
    df = yf.download(
        symbol,
        period="400d",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    if df is None or df.empty:
        return pd.DataFrame()

    df = normalize_to_taipei_index(df)
    return df


def fetch_stock_data(stock_no: str) -> Dict[str, Optional[float]]:
    """
    回傳股票最近交易日的資料。
    會先試 .TW，再試 .TWO
    """
    candidates = [f"{stock_no}.TW", f"{stock_no}.TWO"]
    last_error = None

    for symbol in candidates:
        try:
            df = download_stock_df(symbol)
            if df.empty:
                continue

            if "Close" not in df.columns:
                continue

            close_series = df["Close"].copy()
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]

            df = df.copy()
            df["Close_1D"] = close_series
            df = df.dropna(subset=["Close_1D"])

            if df.empty:
                continue

            df["MA30"] = df["Close_1D"].rolling(30).mean()
            df["MA300"] = df["Close_1D"].rolling(300).mean()

            last_row = df.iloc[-1]

            close_price = pick_scalar(last_row["Close_1D"])
            ma30 = pick_scalar(last_row["MA30"])
            ma300 = pick_scalar(last_row["MA300"])

            if close_price is None:
                continue

            diff30 = ((close_price - ma30) / ma30 * 100) if ma30 not in (None, 0) else None
            diff300 = ((close_price - ma300) / ma300 * 100) if ma300 not in (None, 0) else None

            trade_date = df.index[-1].strftime("%Y-%m-%d")

            logger.info(
                f"stock={stock_no}, symbol={symbol}, trade_date={trade_date}, "
                f"close={close_price}, ma30={ma30}, ma300={ma300}"
            )

            return {
                "stock_no": stock_no,
                "symbol": symbol,
                "trade_date": trade_date,
                "close": round(close_price, 2),
                "ma30": round(ma30, 2) if ma30 is not None else None,
                "ma300": round(ma300, 2) if ma300 is not None else None,
                "diff30": round(diff30, 2) if diff30 is not None else None,
                "diff300": round(diff300, 2) if diff300 is not None else None,
            }

        except Exception as e:
            last_error = e
            logger.exception(f"抓取 {symbol} 失敗: {e}")

    raise ValueError(f"抓不到股票 {stock_no} 的資料，最後錯誤：{last_error}")


def recommendation_text(diff30: Optional[float]) -> Tuple[str, str]:
    """
    依 30 日均線差距，做簡單提示
    """
    if diff30 is None:
        return "⚪ 暫無法判斷", "資料不足"

    if diff30 < -8:
        return "🟢 可留意", "價格低於30日均線較多，可觀察是否止跌"
    if -8 <= diff30 <= 3:
        return "🟢 可小量布局", "價格接近均線附近，可小量定期投入"
    if 3 < diff30 <= 10:
        return "🟡 不宜追高", "價格高於30日均線，建議等拉回"
    return "🔴 偏熱", "價格離30日均線過遠，短線不建議追價"


def format_stock_report(stock_no: str) -> str:
    try:
        data = fetch_stock_data(stock_no)
        suggest, reason = recommendation_text(data["diff30"])

        lines = []
        lines.append(f"📌 {stock_no} 重點：")
        lines.append(f"最近交易日收盤價：{data['close']}")
        lines.append(f"交易日：{data['trade_date']}")
        lines.append(f"近30天平均價：{data['ma30'] if data['ma30'] is not None else '暫無資料'}")
        lines.append(f"近300天平均價：{data['ma300'] if data['ma300'] is not None else '暫無資料'}")
        lines.append(f"與30日均差距：{str(data['diff30']) + '%' if data['diff30'] is not None else '暫無資料'}")
        lines.append(f"與300日均差距：{str(data['diff300']) + '%' if data['diff300'] is not None else '暫無資料'}")
        lines.append(f"是否建議加碼：{suggest}")
        lines.append(f"原因：{reason}")
        return "\n".join(lines)

    except Exception as e:
        logger.exception(f"format_stock_report 失敗 stock_no={stock_no}, error={e}")
        return f"📌 {stock_no} 重點：\n資料取得失敗：{e}"


def build_market_summary() -> str:
    """
    簡單市場摘要。
    這裡先保留固定文字，避免因額外資料源導致不穩。
    """
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

    return "📋 你的自選股票：\n" + "\n".join([f"- {s}" for s in stocks])


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