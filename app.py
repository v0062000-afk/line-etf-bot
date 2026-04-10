import os
import re
import sqlite3
import random
import string
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any

import requests
from dotenv import load_dotenv
import yfinance as yf
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from zoneinfo import ZoneInfo

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

load_dotenv()

TAIWAN_TZ = ZoneInfo("Asia/Taipei")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_USER_IDS = {x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}
DEFAULT_TRIAL_DAYS = int(os.getenv("DEFAULT_TRIAL_DAYS", "7"))
BASE_WATCHLIST = [x.strip() for x in os.getenv("BASE_WATCHLIST", "0056").split(",") if x.strip()]

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("請先設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LINE ETF / 台股混合穩定版")
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

HTTP_TIMEOUT = 15
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

THEME_MAP: Dict[str, List[str]] = {
    "0056": ["高股息", "ETF配息", "台股ETF"],
    "00878": ["高股息", "ETF配息", "台股ETF"],
    "0050": ["市值型ETF", "權值股", "台股ETF"],
    "006208": ["市值型ETF", "權值股", "台股ETF"],
    "1815": ["不鏽鋼", "原物料", "傳產"],
    "2330": ["AI", "半導體", "CoWoS"],
    "2317": ["AI伺服器", "組裝", "電動車"],
    "2382": ["AI伺服器", "筆電", "電子代工"],
    "3017": ["AI伺服器", "散熱", "機殼"],
    "3231": ["BBU", "AI伺服器電源", "電池備援"],
    "6669": ["半導體設備", "先進封裝", "CoWoS"],
}


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            access_expires_at TEXT,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            plan_name TEXT NOT NULL DEFAULT 'trial'
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, symbol)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            days INTEGER NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT,
            note TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id TEXT NOT NULL,
            redeemed_at TEXT NOT NULL,
            days INTEGER NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def now_str() -> str:
    return datetime.now(TAIWAN_TZ).isoformat()


def parse_dt(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text)


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".TW", "").replace(".TWO", "")


def ensure_user(user_id: str) -> None:
    conn = db_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()

    if row is None:
        expires_at = (datetime.now(TAIWAN_TZ) + timedelta(days=DEFAULT_TRIAL_DAYS)).isoformat()
        cur.execute(
            """
            INSERT INTO users (user_id, created_at, last_seen_at, access_expires_at, plan_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, now_str(), now_str(), expires_at, "trial"),
        )

        for symbol in BASE_WATCHLIST:
            cur.execute(
                """
                INSERT OR IGNORE INTO subscriptions (user_id, symbol, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, normalize_symbol(symbol), now_str()),
            )
    else:
        cur.execute(
            "UPDATE users SET last_seen_at = ?, is_blocked = 0 WHERE user_id = ?",
            (now_str(), user_id),
        )

    conn.commit()
    conn.close()


def user_status(user_id: str) -> Tuple[bool, Optional[datetime], str]:
    conn = db_conn()
    row = conn.execute(
        "SELECT access_expires_at, plan_name FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()

    if not row:
        return False, None, "unknown"

    expires_at = parse_dt(row["access_expires_at"])
    active = bool(expires_at and expires_at >= datetime.now(TAIWAN_TZ))
    return active, expires_at, row["plan_name"]


def add_subscription(user_id: str, symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    conn = db_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO subscriptions (user_id, symbol, created_at)
        VALUES (?, ?, ?)
        """,
        (user_id, symbol, now_str()),
    )
    conn.commit()
    conn.close()
    return symbol


def remove_subscription(user_id: str, symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    conn = db_conn()
    conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND symbol = ?", (user_id, symbol))
    conn.commit()
    conn.close()
    return symbol


def get_subscriptions(user_id: str) -> List[str]:
    conn = db_conn()
    rows = conn.execute(
        "SELECT symbol FROM subscriptions WHERE user_id = ? ORDER BY symbol ASC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [row["symbol"] for row in rows]


def generate_code(
    days: int,
    note: str = "",
    max_uses: int = 1,
    expires_days: Optional[int] = None,
    created_by: str = "",
) -> str:
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    expires_at = None

    if expires_days:
        expires_at = (datetime.now(TAIWAN_TZ) + timedelta(days=expires_days)).isoformat()

    conn = db_conn()
    conn.execute(
        """
        INSERT INTO redeem_codes
        (code, days, max_uses, used_count, expires_at, created_at, created_by, note)
        VALUES (?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (code, days, max_uses, expires_at, now_str(), created_by, note),
    )
    conn.commit()
    conn.close()
    return code


def redeem_code(user_id: str, code: str) -> Tuple[bool, str]:
    conn = db_conn()
    cur = conn.cursor()
    code = code.strip().upper()

    row = cur.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,)).fetchone()
    if not row:
        conn.close()
        return False, "查無此序號。"

    expires_at = parse_dt(row["expires_at"])
    if expires_at and expires_at < datetime.now(TAIWAN_TZ):
        conn.close()
        return False, "此序號已過期。"

    if row["used_count"] >= row["max_uses"]:
        conn.close()
        return False, "此序號已被使用完畢。"

    user_row = cur.execute(
        "SELECT access_expires_at FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    base = datetime.now(TAIWAN_TZ)
    if user_row and user_row["access_expires_at"]:
        current_expire = parse_dt(user_row["access_expires_at"])
        if current_expire and current_expire > base:
            base = current_expire

    new_expire = base + timedelta(days=row["days"])

    cur.execute(
        "UPDATE users SET access_expires_at = ?, plan_name = ? WHERE user_id = ?",
        (new_expire.isoformat(), f"paid_{row['days']}d", user_id),
    )
    cur.execute(
        "UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?",
        (code,),
    )
    cur.execute(
        """
        INSERT INTO redemptions (code, user_id, redeemed_at, days)
        VALUES (?, ?, ?, ?)
        """,
        (code, user_id, now_str(), row["days"]),
    )
    conn.commit()
    conn.close()

    return True, f"兌換成功，已延長 {row['days']} 天，到期日：{new_expire.strftime('%Y-%m-%d %H:%M')}"


def vix_to_fear_proxy(vix_value: float) -> int:
    if vix_value >= 40:
        return 5
    if vix_value >= 35:
        return 10
    if vix_value >= 30:
        return 20
    if vix_value >= 25:
        return 30
    if vix_value >= 20:
        return 45
    if vix_value >= 17:
        return 55
    if vix_value >= 14:
        return 70
    if vix_value >= 11:
        return 82
    return 90


def fear_label(value: Optional[int]) -> str:
    if value is None:
        return "無法取得"
    if value <= 24:
        return "極度恐慌"
    if value <= 44:
        return "恐慌"
    if value <= 54:
        return "中性"
    if value <= 74:
        return "貪婪"
    return "極度貪婪"


def get_fear_greed() -> Tuple[Optional[int], str]:
    try:
        vix_df = yf.Ticker("^VIX").history(period="10d", auto_adjust=False)
        if vix_df is not None and not vix_df.empty:
            close = vix_df["Close"].dropna()
            if not close.empty:
                latest_vix = float(close.iloc[-1])
                proxy_score = vix_to_fear_proxy(latest_vix)
                return proxy_score, f"VIX fallback ({latest_vix:.2f})"
    except Exception as e:
        logger.exception("VIX 抓取失敗: %s", e)

    return None, "unavailable"


def _safe_float(text: Any) -> Optional[float]:
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace(" ", "")
    if s in {"", "--", "---", "----", "X", "N/A"}:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _month_iter(count: int) -> List[datetime]:
    today = datetime.now(TAIWAN_TZ)
    y = today.year
    m = today.month
    out: List[datetime] = []
    for _ in range(count):
        out.append(datetime(y, m, 1))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def _fetch_twse_month(stock_id: str, month_dt: datetime) -> List[Tuple[str, float]]:
    date_str = month_dt.strftime("%Y%m01")
    url = (
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        f"?response=json&date={date_str}&stockNo={stock_id}"
    )
    r = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if data.get("stat") != "OK" or not data.get("data"):
        return []

    rows: List[Tuple[str, float]] = []
    for row in data["data"]:
        close_price = _safe_float(row[6])
        if close_price is None:
            continue

        roc_date = str(row[0]).strip()
        parts = roc_date.split("/")
        if len(parts) == 3:
            year = int(parts[0]) + 1911
            month = int(parts[1])
            day = int(parts[2])
            gdate = f"{year:04d}-{month:02d}-{day:02d}"
        else:
            gdate = roc_date

        rows.append((gdate, close_price))
    return rows


def _fetch_tpex_month(stock_id: str, month_dt: datetime) -> List[Tuple[str, float]]:
    roc_year = month_dt.year - 1911
    roc_month = month_dt.month
    url = (
        "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
        f"?l=zh-tw&d={roc_year}/{roc_month:02d}&stkno={stock_id}"
    )
    r = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if not data.get("aaData"):
        return []

    rows: List[Tuple[str, float]] = []
    for row in data["aaData"]:
        close_price = _safe_float(row[2])
        if close_price is None:
            continue

        roc_date = str(row[0]).strip()
        parts = roc_date.split("/")
        if len(parts) == 3:
            year = int(parts[0]) + 1911
            month = int(parts[1])
            day = int(parts[2])
            gdate = f"{year:04d}-{month:02d}-{day:02d}"
        else:
            gdate = roc_date

        rows.append((gdate, close_price))
    return rows


def get_official_price_data(symbol: str, max_months: int = 18) -> Dict[str, Any]:
    sym = normalize_symbol(symbol)
    months = _month_iter(max_months)

    rows: List[Tuple[str, float]] = []
    market = None

    for month_dt in months:
        try:
            month_rows = _fetch_twse_month(sym, month_dt)
            if month_rows:
                rows.extend(month_rows)
                market = "TWSE"
        except Exception:
            pass

    if not rows:
        for month_dt in months:
            try:
                month_rows = _fetch_tpex_month(sym, month_dt)
                if month_rows:
                    rows.extend(month_rows)
                    market = "TPEx"
            except Exception:
                pass

    if not rows:
        raise ValueError(f"官方抓不到 {sym} 資料")

    dedup: Dict[str, float] = {}
    for d, c in rows:
        dedup[d] = c

    sorted_rows = sorted(dedup.items(), key=lambda x: x[0])
    closes = [c for _, c in sorted_rows]
    if not closes:
        raise ValueError(f"官方沒有 {sym} 有效收盤價")

    latest_date, latest_close = sorted_rows[-1]
    ma30_src = closes[-30:] if len(closes) >= 30 else closes
    ma300_src = closes[-300:] if len(closes) >= 300 else closes

    ma30 = sum(ma30_src) / len(ma30_src)
    ma300 = sum(ma300_src) / len(ma300_src)

    return {
        "symbol": sym,
        "source_symbol": market,
        "close": round(latest_close, 2),
        "ma30": round(ma30, 2),
        "ma300": round(ma300, 2),
        "vs_ma30_pct": round((latest_close - ma30) / ma30 * 100, 2),
        "vs_ma300_pct": round((latest_close - ma300) / ma300 * 100, 2),
        "data_date": latest_date,
    }


def get_yahoo_price_data(symbol: str, lookback_days: int = 450) -> Dict[str, Any]:
    sym = normalize_symbol(symbol)
    tried = [f"{sym}.TW", f"{sym}.TWO"]
    last_error = None

    for ticker in tried:
        try:
            df = yf.Ticker(ticker).history(period=f"{lookback_days}d", auto_adjust=False)
            if df is None or df.empty:
                last_error = "empty dataframe"
                continue

            close = df["Close"].dropna()
            if close.empty:
                last_error = "empty close"
                continue

            latest_dt = close.index[-1]
            latest_price = float(close.iloc[-1])

            ma30 = float(close.tail(30).mean()) if len(close) >= 30 else float(close.mean())
            ma300 = float(close.tail(300).mean()) if len(close) >= 300 else float(close.mean())

            try:
                latest_date = latest_dt.tz_localize(None).strftime("%Y-%m-%d")
            except Exception:
                latest_date = latest_dt.strftime("%Y-%m-%d")

            return {
                "symbol": sym,
                "source_symbol": ticker,
                "close": round(latest_price, 2),
                "ma30": round(ma30, 2),
                "ma300": round(ma300, 2),
                "vs_ma30_pct": round((latest_price - ma30) / ma30 * 100, 2),
                "vs_ma300_pct": round((latest_price - ma300) / ma300 * 100, 2),
                "data_date": latest_date,
            }

        except Exception as e:
            last_error = str(e)
            continue

    raise ValueError(f"Yahoo 抓不到 {sym} 資料：{last_error or 'unknown error'}")


def get_hybrid_price_data(symbol: str) -> Dict[str, Any]:
    official_error = None
    yahoo_error = None

    try:
        return get_official_price_data(symbol)
    except Exception as e:
        official_error = str(e)

    try:
        return get_yahoo_price_data(symbol)
    except Exception as e:
        yahoo_error = str(e)

    raise ValueError(f"官方失敗：{official_error}；Yahoo失敗：{yahoo_error}")


def recommendation_for_stock(fear_value: Optional[int], data: Dict[str, Any], is_etf: bool = False) -> Tuple[str, str]:
    diff30 = data["vs_ma30_pct"]
    diff300 = data["vs_ma300_pct"]

    if fear_value is not None and fear_value < 25 and diff30 <= -3:
        return "🔥 強烈加碼", "市場偏恐慌，且價格低於近30日均線 3% 以上。"

    if (fear_value is not None and fear_value < 35 and diff30 <= 0) or diff300 <= -5:
        return "🟡 可分批加碼", "情緒偏弱或價格回到均線附近，可分批布局。"

    if diff30 > 6 and diff300 > 15 and not is_etf:
        return "⚠️ 先觀望", "股價已明顯高於短中期均線，追價風險較高。"

    if diff30 > 3 and diff300 > 8 and is_etf:
        return "⚠️ 先觀望", "價格高於短中期均線較多，先保留資金。"

    return "🟢 可小量布局", "價格接近均線附近，可小量定期投入。"


def get_recent_theme(symbol: str) -> str:
    sym = normalize_symbol(symbol)
    themes = THEME_MAP.get(sym)
    if themes:
        return "、".join(themes)
    return "暫無預設題材"


def estimate_major_holder_cost_zone(data: Dict[str, Any]) -> Tuple[str, str]:
    ma30 = data["ma30"]
    ma300 = data["ma300"]
    close = data["close"]

    center = ma30 * 0.7 + ma300 * 0.3
    low = center * 0.97
    high = center * 1.03

    if close < low:
        desc = "股價低於成本區附近"
    elif close > high:
        desc = "股價高於成本區附近"
    else:
        desc = "股價接近成本區"

    return f"{low:.2f} ~ {high:.2f}", desc


def general_stock_summary(data: Dict[str, Any], fear_value: Optional[int]) -> str:
    rec, reason = recommendation_for_stock(fear_value, data, is_etf=False)
    themes = get_recent_theme(data["symbol"])
    cost_zone, cost_desc = estimate_major_holder_cost_zone(data)

    return (
        f"📌 {data['symbol']} 重點：\n"
        f"收盤價：{data['close']}\n"
        f"近30天平均價：{data['ma30']}\n"
        f"近300天平均價：{data['ma300']}\n"
        f"與30日均差距：{data['vs_ma30_pct']}%\n"
        f"與300日均差距：{data['vs_ma300_pct']}%\n"
        f"是否建議加碼：{rec}\n"
        f"原因：{reason}\n"
        f"近期題材：{themes}\n"
        f"大戶成本區推估：{cost_zone}\n"
        f"成本區判斷：{cost_desc}"
    )


def build_daily_report_for_user(user_id: str) -> str:
    fear_value, fg_source = get_fear_greed()

    lines = [
        f"📊 每日投資提醒（{datetime.now(TAIWAN_TZ).strftime('%Y/%m/%d %H:%M')}）",
        "",
    ]

    if fg_source.startswith("VIX fallback"):
        lines.extend(
            [
                f"😱 市場情緒代理值：{fear_value if fear_value is not None else 'N/A'}（{fear_label(fear_value)}）",
                f"資料來源：{fg_source}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"😱 恐慌指數：{fear_value if fear_value is not None else 'N/A'}（{fear_label(fear_value)}）",
                f"資料來源：{fg_source}",
                "",
            ]
        )

    symbols = get_subscriptions(user_id)
    if "0056" not in symbols:
        symbols = ["0056"] + symbols

    extra_lines: List[str] = []
    for symbol in symbols:
        try:
            data = get_hybrid_price_data(symbol)
        except Exception as e:
            extra_lines.append(f"📌 {symbol} 抓取失敗：{e}")
            continue

        if symbol == "0056":
            rec, reason = recommendation_for_stock(fear_value, data, is_etf=True)
            themes = get_recent_theme(symbol)
            cost_zone, cost_desc = estimate_major_holder_cost_zone(data)

            lines.extend(
                [
                    "📈 0056 重點：",
                    f"收盤價：{data['close']}",
                    f"近30天平均價：{data['ma30']}",
                    f"近300天平均價：{data['ma300']}",
                    f"與30日均差距：{data['vs_ma30_pct']}%",
                    f"與300日均差距：{data['vs_ma300_pct']}%",
                    f"是否建議加碼：{rec}",
                    f"原因：{reason}",
                    f"近期題材：{themes}",
                    f"大戶成本區推估：{cost_zone}",
                    f"成本區判斷：{cost_desc}",
                    "",
                ]
            )
        else:
            extra_lines.append(general_stock_summary(data, fear_value))

    if extra_lines:
        lines.append("👀 你的自選股票：")
        lines.extend(extra_lines)
        lines.append("")

    lines.extend(
        [
            "可用指令：",
            "今日報告",
            "新增股票 2330",
            "刪除股票 2330",
            "我的股票",
            "狀態",
            "兌換 ABCD123456",
        ]
    )

    return "\n".join(lines)


def reply_message(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def push_text(user_id: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=text)]))


def help_text() -> str:
    return (
        "可用指令：\n"
        "1) 今日報告\n"
        "2) 新增股票 2330\n"
        "3) 刪除股票 2330\n"
        "4) 我的股票\n"
        "5) 狀態\n"
        "6) 兌換 ABCD123456\n"
        "\n"
        "管理者指令：\n"
        "7) 產生序號 30\n"
        "8) 產生序號 30 5\n"
        "9) 試推播\n"
        "10) 查ID"
    )


def handle_text_command(user_id: str, text: str) -> str:
    cmd = text.strip()
    active, expires_at, plan_name = user_status(user_id)

    if cmd in {"help", "幫助", "說明", "menu", "選單"}:
        return help_text()

    if cmd == "查ID":
        return f"你的 LINE USER ID：\n{user_id}"

    if cmd in {"狀態", "我的狀態"}:
        return (
            f"方案：{plan_name}\n"
            f"到期：{expires_at.strftime('%Y-%m-%d %H:%M') if expires_at else '未設定'}\n"
            f"目前狀態：{'可使用' if active else '已過期，請兌換序號'}"
        )

    if cmd == "我的股票":
        symbols = get_subscriptions(user_id)
        return "你的股票：" + ("、".join(symbols) if symbols else "尚未設定")

    if cmd in {"今日報告", "今日", "report"}:
        if not active:
            return "你的使用期限已到，請先輸入：兌換 序號"
        return build_daily_report_for_user(user_id)

    m = re.match(r"^新增股票\s+([0-9A-Za-z]{2,10})$", cmd)
    if m:
        symbol = add_subscription(user_id, m.group(1))
        return f"已加入 {symbol} 到查詢清單。"

    m = re.match(r"^刪除股票\s+([0-9A-Za-z]{2,10})$", cmd)
    if m:
        symbol = remove_subscription(user_id, m.group(1))
        return f"已刪除 {symbol}。"

    m = re.match(r"^兌換\s+([A-Za-z0-9]{6,20})$", cmd)
    if m:
        ok, msg = redeem_code(user_id, m.group(1))
        return msg

    if user_id in ADMIN_USER_IDS:
        m = re.match(r"^產生序號\s+(\d{1,4})(?:\s+(\d{1,3}))?$", cmd)
        if m:
            days = int(m.group(1))
            qty = int(m.group(2) or 1)
            codes = [generate_code(days=days, created_by=user_id) for _ in range(qty)]
            return "以下為新序號：\n" + "\n".join(codes)

        if cmd == "試推播":
            report = build_daily_report_for_user(user_id)
            try:
                push_text(user_id, report)
                return "已推播測試訊息。"
            except Exception as e:
                return f"推播失敗：{e}"

    return "看不懂你的指令。\n\n" + help_text()


@handler.add(FollowEvent)
def handle_follow(event: FollowEvent):
    user_id = event.source.user_id
    ensure_user(user_id)
    reply_message(
        event.reply_token,
        (
            f"歡迎加入！你已獲得 {DEFAULT_TRIAL_DAYS} 天試用。\n"
            f"目前採手動查詢模式，請直接輸入「今日報告」。\n\n"
            + help_text()
        ),
    )


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    ensure_user(user_id)
    text = event.message.text
    response = handle_text_command(user_id, text)
    reply_message(event.reply_token, response)


@app.get("/")
def root():
    return {"ok": True, "message": "LINE ETF bot running"}


@app.get("/health")
def health():
    return {"ok": True, "time": now_str()}


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    body_text = body.decode("utf-8")

    try:
        if not x_line_signature:
            raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

        handler.handle(body_text, x_line_signature)
        return JSONResponse({"ok": True})

    except InvalidSignatureError:
        logger.exception("invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    except Exception as e:
        logger.exception("webhook error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("startup")
def startup_event():
    init_db()
    logger.info("Application startup complete")