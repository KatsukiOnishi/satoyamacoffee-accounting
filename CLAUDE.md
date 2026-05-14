# satoyamacoffee-accounting — Claude Code 向け取扱説明書

## 1. 役割

合同会社秋田里山デザイン（さとやまコーヒー）の **月次決算自動化ハブ**。

各業務システム（在庫管理 `coffee_system` / 勤怠 `attendance-system` / Shopify / 百貨店明細PDF / 銀行CSV 等）から月次の数字を取得し、freee API 経由で **仕訳・請求書として登録する** ことが責務。

業務ロジック（仕入請求書のパース、給与計算、在庫評価など）は各業務システム側に置き、本リポジトリはそれらを「呼び出し → freee へ反映」する統合層に徹する。

実行形態は当面 **ローカルMacでの手動実行のみ**。将来的に Render Cron Job 等で自動化する想定。

## 2. 技術スタック

| レイヤ | 採用技術 |
|---|---|
| 言語 | Python 3.11 |
| 依存管理 | `pyproject.toml` + `pip`（`uv` でも可）、venv は `.venv/` |
| CLI | `typer` |
| HTTP | `httpx` |
| DB | SQLite（`accounting.db`、冪等性管理用） + SQLAlchemy 2.0 |
| ログ | `structlog`（JSON Lines を `logs/{YYYY-MM-DD}.jsonl` に追記 + stdout 色付き） |
| メール | `resend` SDK（失敗・サマリ通知） |
| 設定 | `python-dotenv` |
| データ検証 | `pydantic` v2 |
| PDF | `pdfplumber`（後続タスクで利用） |
| Excel | `openpyxl`（後続タスクで利用） |
| テスト | `pytest` |

## 3. ディレクトリ構造

```
satoyamacoffee-accounting/
├── pyproject.toml
├── .env.example / .env       # .env は gitignore
├── .gitignore
├── README.md
├── CLAUDE.md
├── accounting.db             # SQLite（冪等性管理、gitignore）
├── logs/                     # structlog 出力（gitignore）
├── accounting/               # メインパッケージ
│   ├── cli.py                # typer エントリ
│   ├── config.py             # 環境変数の一元管理（pydantic Settings）
│   ├── core/                 # 共通基盤
│   │   ├── logger.py         # structlog セットアップ
│   │   ├── notifier.py       # Resend 失敗・サマリ通知
│   │   ├── idempotency.py    # SQLite で external_id 重複防止
│   │   ├── dry_run.py        # dry-run コンテキストマネージャ
│   │   ├── report.py         # 実行サマリ（成功/失敗/要確認）
│   │   └── db.py             # SQLAlchemy engine
│   ├── connectors/           # 外部システムアダプタ
│   │   ├── freee.py          # freee API（仕訳・請求書、冪等性付き）
│   │   ├── coffee_system.py  # （後続）在庫スナップショット取得
│   │   ├── attendance.py     # （後続）月次給与取得
│   │   └── shopify.py        # （後続）売上・手数料取得
│   └── tasks/                # 月次タスク（1ファイル1タスク 〜 1ディレクトリ1タスク）
│       ├── ping.py           # 共通基盤の疎通確認用ダミー
│       ├── dept_store_invoice.py
│       ├── inventory_valuation.py
│       ├── journal_rules.py
│       └── vendor_invoice/   # ベンダー請求書メール取込（Gmail → Vision → freee）
└── tests/
    └── test_idempotency.py
```

## 4. 共通基盤7観点の実装方針

後続タスクが何度も再実装しないよう、以下7観点は `accounting/core/` に集約し、安定インターフェースを維持する。

| 観点 | 実装 | ポイント |
|---|---|---|
| **ログ** | `core/logger.py` | structlog JSON。フィールドは `timestamp/level/task/run_id/event/...`。`get_logger(task)` で task名付きロガー |
| **通知** | `core/notifier.py` | Resend SDK。`notify_failure(task, run_id, error, context)` は常に送信、`notify_summary` は `NOTIFY_ON_SUCCESS=true` のときだけ |
| **dry-run** | `core/dry_run.py` | `DryRunContext` で `is_dry_run` フラグを上書き。`config.DRY_RUN` がデフォルト、CLI `--no-dry-run` で本番 |
| **冪等性** | `core/idempotency.py` | SQLite `executed_operations` テーブル。`(task, external_id)` でユニーク制約。`is_executed / mark_executed / get_execution` |
| **レポート** | `core/report.py` | `RunReport` に `add_success/add_failure/add_warning`。`finalize()` でログ＋通知へ流す。`run_id = {task}-{YYYYMMDD-HHMMSS}-{shortuuid}` |
| **認証** | `connectors/freee.py` | freee API トークンは `config.FREEE_API_KEY` から。勘定科目IDは env で渡す（ハードコード禁止） |
| **仕様書** | この CLAUDE.md | 各タスクの責務・呼び出し方・冪等性キーの取り方は本ファイルと各タスクの docstring に集約 |

## 5. ローカル実行

```bash
cd /Users/katsuki/Claude/satoyamacoffee-accounting
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # 既存なら不要
accounting ping --dry-run
```

### 主なコマンド

- `accounting ping [--dry-run/--no-dry-run]` — 共通基盤の疎通確認
- `accounting list-accounts` — freee 勘定科目一覧（初期 env 設定の補助）
- `accounting vendor-invoice scan [--days 30] [--no-dry-run]` — ベンダー請求書メールを Gmail から取込み → freee 取引登録 + 未払金消し込み
- `accounting vendor-invoice list / apply <id> / reconcile` — 候補一覧・個別承認・振込後の消し込み再試行
- `accounting auth gmail-init` — Gmail OAuth 初回認可（詳細は `README_vendor_invoice.md`）
- `pytest` — テスト実行

## 6. 環境変数

詳細は `.env.example` を参照。主なグループ:

- **freee**: `FREEE_API_KEY`, `FREEE_COMPANY_ID`, `FREEE_ACCOUNT_ITEM_*`
- **他システム**: `COFFEE_SYSTEM_*`, `ATTENDANCE_SYSTEM_*`, `SHOPIFY_*`
- **通知**: `RESEND_API_KEY`, `NOTIFY_EMAIL`, `FROM_EMAIL`, `NOTIFY_ON_SUCCESS`
- **実行モード**: `DRY_RUN`, `LOG_LEVEL`, `DATABASE_PATH`

## 7. デプロイ

当面なし。ローカルでの手動 CLI 実行のみ。

将来 Render Cron Job に乗せる場合は、`accounting.db` を永続ディスクに置くか、外部の Postgres に移行する必要がある（冪等性記録の永続化のため）。

## 8. 規約・既知の癖

- **タスクの独立性**: `accounting/tasks/*.py` 同士で直接 import しない。共有処理は `core/` か `connectors/` に置く。
- **冪等性は必須**: freee へ書き込む全タスクは `external_id`（請求書番号・伝票ID・取引ハッシュ等）で `mark_executed` する。再実行で二重登録を起こさないこと。
- **dry-run がデフォルト**: `.env` の `DRY_RUN=true` を前提。本番実行は `--no-dry-run` フラグを明示する。
- **freee レスポンスを必ず保存**: `freee.register_journal()` の戻り値 `freee_journal_id` は `mark_executed` に渡す。後で freee 側を消す/直す時の手がかりになる。
- **run_id**: `{task}-{YYYYMMDD-HHMMSS}-{shortuuid}` 形式。ログ・通知・冪等性テーブル全てで同じものを使う。

## 8a. vendor-invoice タスク（ベンダー請求書メール取込）

ベンダーからメールで届く請求書PDFを Gmail から取込み → freee に Dr.費用 / Cr.未払金 の取引として登録し、振込が降りていれば未払金消し込みまで自動化するタスク。

- パッケージ: `accounting/tasks/vendor_invoice/`
- 候補テーブル: `vendor_invoice_candidates`（`accounting/core/vendor_invoice_candidates.py`）
- Gmail 認証: `accounting/core/gmail_auth.py`（scope=`gmail.readonly` のみ）
- 認証バックエンド: `secrets/gmail_credentials.json`（手動配置）+ `secrets/gmail_tokens.json`（auto refresh）
- 除外リスト: `accounting/tasks/vendor_invoice/blacklists.py`（クレカ自動引落・自社発行・物流スポット等）

### スコープ外
- クレジットカード自動引落系（Shopify / Anthropic / Slack / お名前.com / freeeカード等）— 「自動で経理」で計上済みのため二重計上回避
- 輸入・物流スポット（Falcon Coffees, DSV, FedEx 等）— 金額大・条件個別で手動運用
- 振込実行の自動化 — ユーザーが銀行アプリで手動振込

### ユーザー側準備
`README_vendor_invoice.md` を参照。Google Cloud Console での OAuth クライアント作成 → `accounting auth gmail-init` で完了。

## 9. 他リポジトリとの関係

```
┌────────────────────────────┐
│  satoyamacoffee-accounting │  ← 本リポジトリ（統合・freee登録）
└────┬───────────────────────┘
     │ HTTP API / CSV / PDF
     ▼
┌──────────────────┬───────────────────┬─────────┐
│  coffee_system   │  attendance-system │ Shopify │  ... + 銀行CSV、百貨店PDF
│  (在庫・原価)    │  (勤怠・給与)     │ (EC売上)│
└──────────────────┴───────────────────┴─────────┘
                                                       ↓
                                                   freee API
```

- **coffee_system** (`https://satoyamacoffee-inventory-system.onrender.com`)
  - 提供: 月末棚卸資産スナップショット、ロット別/商品別原価
  - 既存の `app/services/freee_api.py`（棚卸仕訳）は将来本ハブへ移管予定。重複実装にしない。
- **attendance-system** (`https://shifts.satoyamacoffee.com`)
  - 提供: 月次給与（基本給・所得税・住民税・社会保険・交通費）
  - freee 連携は持たない方針なので、本ハブが API から取得して仕訳化する。
- **coffee-reservation**
  - 会計連携は基本なし（売上は Shopify / 店頭 POS 側で計上）。必要が出たら追加検討。
- **Shopify**
  - 提供: EC 売上・決済手数料・送料収入
- **freee**
  - 本ハブが唯一の書き込み元となるよう統一する（他リポからの直接書き込みは段階的に廃止）。

## 10. やってはいけないこと

- **業務ロジックを本リポに持ち込まない**。給与計算ロジックや棚卸評価ロジックは各業務システム側に置く。本リポは取得・整形・freee登録に徹する。
- **冪等性なしで freee に書き込まない**。`external_id` を必ず決め、`is_executed` で事前チェックする。
- **`account_item_id` をコードにハードコードしない**。env から渡す。テスト環境と本番でID違いがあるため。
- **タスク同士の import 連鎖を作らない**。共通処理は必ず `core/` か `connectors/` に持ち上げる。
- **dry-run を黙ってスキップしない**。本番実行は `--no-dry-run` フラグを必須にし、ログにも明示する。
