# LINE ETF 推播機器人

這是一個可直接部署的 LINE Bot，功能包含：

- 每天早上 8:00 推播
  - 當天恐慌指數
  - 0056 收盤價
  - 近 30 天平均價
  - 近 300 天平均價
  - 是否建議加碼
- 使用者可新增 / 刪除自選股票
- 試用天數控制
- 付費序號兌換延長使用時間
- 管理者可產生序號

## 1. 安裝

```bash
python -m venv .venv
source .venv/bin/activate   # Windows 改用 .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. 設定環境變數

至少要設定：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `ADMIN_USER_IDS`

## 3. 本機執行

```bash
uvicorn app:app --reload --port 8000
```

Webhook URL：

```text
https://你的網域/callback
```

## 4. LINE Developers 設定流程

1. 到 LINE Developers 建立 Messaging API channel。
2. 取得 Channel secret 與 Channel access token。
3. 將 Webhook URL 指向 `/callback`。
4. 開啟 Webhook。
5. 加 bot 為好友後，傳送 `help` 測試。

LINE 會把使用者訊息透過 webhook 傳到你的伺服器，而且官方文件要求你驗證 `x-line-signature`，避免惡意請求。citeturn235634search0turn235634search3

## 5. 目前可用指令

### 一般使用者

- `今日報告`
- `新增股票 2330`
- `刪除股票 2330`
- `我的股票`
- `狀態`
- `兌換 ABCD123456`

### 管理者

- `產生序號 30`
- `產生序號 30 5`
- `試推播`

## 6. 付費設計方式

這個版本採用「序號制」：

- 你先用管理者帳號產生序號
- 使用者付款後，你手動把序號給他
- 使用者在 LINE 輸入 `兌換 序號`
- 到期時間自動往後延長

這個方式最容易先上線。

### 若你要自動收費

你之後可以再串：

- 綠界 ECPay
- 藍新 NewebPay
- TapPay

做法是：付款成功後，由金流 webhook 自動呼叫你的系統建立序號或直接延長天數。

## 7. 排程說明

這份程式使用 APScheduler 在伺服器端定時執行，因為 LINE Messaging API 本身沒有內建每天固定時間自動排程推播，你需要由自己的伺服器在指定時間呼叫推播 API。citeturn235634search3turn235634search0

## 8. 重要注意

- 恐慌指數抓取使用 CNN/備援來源解析，第三方頁面版型若變動，可能需要調整。
- 股票資料使用 Yahoo Finance。
- 免費主機若會休眠，早上 8:00 推播可能失敗，建議至少用不休眠方案。
- SQLite 適合初期，小量用戶夠用；若使用者多，建議改 PostgreSQL。

## 9. Render 部署

專案已附 `render.yaml`，可直接用 Render 建立服務。

部署後請把 Render 網址填回 LINE Developers 的 Webhook URL。
