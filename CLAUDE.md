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
- `accounting sync-hrmos [--month YYYY-MM] [--no-dry-run] [-u USER_ID ...]` — HRMOS から月次勤怠 CSV を取得 → shifts.satoyamacoffee.com の `/api/admin/import-hrmos` に投入
- `accounting ar-reconcile run [--days 14] [--no-dry-run]` — 未消込入金を freee 未決済請求書に引き当てて消込（仕様書 §5-1、詳細は `README_auto_keiri.md`）
- `accounting auto-classify run [--days 14] [--no-dry-run] [--limit 50]` — 信頼度付き自動仕訳。shadow（記録のみ）/ production（>0.85 で自動登録）で動作
- `accounting auto-classify set-mode --mode {shadow|production} [--reason ...]` — モード切替（system_settings に永続化、切替通知メール送信）
- `accounting auto-classify get-mode` — 現在のモード・しきい値表示
- `accounting auto-classify set-threshold --high X --low Y` — 信頼度しきい値の手動チューニング
- `accounting auto-classify list [--week YYYY-Www] [--limit N]` — 候補一覧
- `accounting email-digest send [--week YYYY-Www] [--no-dry-run] [--print-html]` — 週次1通ダイジェスト（Resend）
- `./scripts/run_weekly_auto_keiri.sh` — 上記3本を順に走らせるランナー（scheduled-tasks 用）
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

## 8b. sync-hrmos タスク（HRMOS → shifts 月次勤怠取込）

毎月20日（HRMOS 承認締切日）に、前月の社員別勤怠 CSV を HRMOS から取得して
shifts.satoyamacoffee.com（attendance-system）に投入するタスク。

- パッケージ: `accounting/tasks/sync_hrmos_to_shifts.py`
- コネクタ: `accounting/connectors/hrmos.py`（スクレイピング） / `accounting/connectors/shifts.py`（書き込み API）
- 冪等性キー: `(task='sync-hrmos', external_id=YYYY-MM)` — 全件モードのみ
  - `--user-ids` 指定時はチェックも記録もスキップ（限定再実行用）
  - shifts 側は `(staffId, date)` で upsert するため、CSV 再送は安全
- 失敗時: Resend で `katsuki.onishi@gmail.com` に通知

### フロー
1. HRMOS にログイン（CSRF 抽出 → form POST）
2. `/staffs` をスクレイプして在籍中の全社員 user_id を抽出
3. `HRMOS_EXCLUDE_USER_IDS`（カンマ区切り）に該当する user_id を除外
4. 各 user_id について `/works/csv_download` から Shift-JIS の CSV bytes を取得（勤怠ゼロの月も空欄CSVが返る）
5. shifts の `POST /api/admin/import-hrmos` に multipart で一括 POST（Bearer 認証）
6. レスポンスの saved / skipped / errors をログ＋RunReport に集約

`--user-ids` で限定実行する場合は手順 2, 3 をスキップ。`/bulk_approvals` ベースの
`HrmosClient.list_user_ids_for_month()` も残してあるが、現在のタスクでは未使用。

### 起動方法
- 手動: `accounting sync-hrmos --month 2026-04 --no-dry-run`
- 自動: `samples/com.satoyamacoffee.sync-hrmos.plist` を `~/Library/LaunchAgents/` に配置 → `launchctl load`（毎月20日 09:00）

### ユーザー側準備
- `.env` に `HRMOS_USER`, `HRMOS_PASS`, `SHIFTS_ADMIN_API_KEY` を設定
- `HRMOS_EXCLUDE_USER_IDS` にテストアカウント・退職者の user_id をカンマ区切りで設定（現状: `5,6`）
- shifts 側 `.env`（Vercel 環境変数）にも同値の `ADMIN_API_KEY` を設定
- shifts のスタッフマスタに HRMOS の社員番号が登録されていることが前提（未登録なら skipped に出る）

## 8c. auto-keiri 週次バッチ（ar-reconcile / auto-classify / email-digest）

freee「自動で経理 > まとめて入力 > 未処理」の wallet_txn を可能な限り自動で
処理する 3 タスク。詳細は `README_auto_keiri.md` および
`/Users/katsuki/Documents/Claude/Projects/月次決算自動化プロジェクト/auto-keiri_仕様書_for_Claude_Code.md`。

- **ar-reconcile** (`accounting/tasks/ar_reconcile/`)
  - 未紐付の入金 wallet_txn を、freee 未決済売上 deal に金額＋取引先一致で引き当てて消込
  - 消込 API: `POST /api/1/deals/{id}/payments`（`accounting/connectors/freee.py:create_payment_for_deal`）
  - 冪等性キー: `ar-reconcile:wallet_txn:{wallet_txn_id}`
  - 結果テーブル: `ar_reconcile_candidates`

- **auto-classify** (`accounting/tasks/auto_classify/`)
  - 未紐付 wallet_txn 全件を Claude（`claude-sonnet-4-6`）で勘定科目・税区分・取引先判定
  - 構造化出力は `accounting/connectors/anthropic_classifier.py`（tools 強制呼び出し）
  - モード: `system_settings.auto_classify_mode` で `shadow` / `production` 切替
  - 信頼度しきい値: `auto_classify_threshold_high` (既定 0.85) / `_low` (既定 0.6)
  - 冪等性キー: `auto-classify:wallet_txn:{wallet_txn_id}`
  - 結果テーブル: `auto_classify_candidates`
  - 除外: 個人名カナ振込・百貨店・公庫・振込手数料・銀行利息

- **email-digest** (`accounting/tasks/email_digest/`)
  - 直近 7 日の ar-reconcile + auto-classify 結果を Jinja2 HTML テンプレ
    （`templates/digest.html.j2`）で 1 通にまとめて Resend 送信
  - 件名プレフィックス: `[さとやま経理]`
  - 送信ログ: `notification_log` テーブル

DB スキーマは SQLAlchemy 側で完結（`accounting/core/auto_keiri.py`）。初期値投入は
`init_db()` 内の `ensure_initial_settings()` が冪等に行う。

ランナー: `./scripts/run_weekly_auto_keiri.sh`（scheduled-tasks から毎週月曜 09:00 JST 想定）。

## 8d. shopify-sales タスク（Shopify EC 月次売上 → freee 振替伝票）

毎月1日に前月の Shopify Orders を集計して freee に振替伝票1本として登録するタスク。
仕訳パターンB（手数料を月次同時計上、決済方法別に partner で識別）。

- パッケージ: `accounting/tasks/shopify_sales/`
  - `aggregator.py` … Order → MonthlySummary（JST 月境界、返金純額、Shopify Payments は API fees、KOMOJU は固定率）
  - `partner_map.py` … paymentGatewayNames → freee partner（env `SHOPIFY_SALES_PARTNER_<SLUG>` から ID 解決）
  - `freee_writer.py` … MonthlySummary → manual_journal payload。借方=貸方の整合チェック
  - `service.py` … preview / run のオーケストレーション + idempotency
  - `cli.py` … `run` / `preview` / `status`
  - `filters.py` … `exclude-from-accounting` タグ / cancelled / unpaid を除外
  - `env.py` … 本タスク固有の env リーダ（config.py を肥大化させない方針）
- コネクタ: `accounting/connectors/shopify.py`（Admin GraphQL `orders` クエリ、カーソル paginate）
- 冪等性: `(task='shopify-sales', external_id='shopify-sales:YYYY-MM')`
- 失敗時: Resend で `katsuki.onishi@gmail.com` に通知

### 仕訳イメージ（複合仕訳 = manual_journal）
```
2026-04-30 付け
  借方  売掛金   partner=Shopify Payments  132,931
  借方  売掛金   partner=KOMOJU              4,034
  借方  支払手数料 partner=Shopify Payments    4,833 (対象外)
  借方  支払手数料 partner=KOMOJU                151 (対象外)
    貸方 売上高   軽減税率8%(内税)          141,949
```

仕様書（§6）では `POST /api/1/deals` と記載されているが、deals API は単一 entry_side
のみ対応のため `POST /api/1/manual_journals`（振替伝票）を使う（payroll と同じ実装パターン）。

### CLI
```
accounting shopify-sales preview --month 2026-04             # dry-run 集計 + 仕訳プレビュー
accounting shopify-sales run --month 2026-04                 # dry-run（既定）
accounting shopify-sales run --month 2026-04 --no-dry-run    # 本番登録（y/N 確認あり）
accounting shopify-sales run --month 2026-04 --no-dry-run -y # 確認スキップ
accounting shopify-sales status --months 6                   # 過去6月の登録状況
```

### ユーザー側準備
- Shopify Admin で custom app を作成 → Admin API access token を発行
  - 必要 scope: `read_orders`（過去 60 日超を取るには `read_all_orders` も必要）
- `.env` に `SHOPIFY_ADMIN_API_TOKEN` と `SHOPIFY_SHOP_DOMAIN` を設定
- freee で partner『Shopify Payments』を新規作成 → ID を
  `SHOPIFY_SALES_PARTNER_SHOPIFY_PAYMENTS=<id>,Shopify Payments` に設定
- KOMOJU partner（既存 ID=102026938）が `available=False` の場合は freee 画面で再有効化

### 既知の落とし穴（仕様書 §11 から要旨）
- JST 月境界 → UTC は `jst_month_range_utc()` で。Shopify は UTC ベース
- 月初 0:03 JST 付近に定期便の自動課金が走る（実績で約 40 件秒単位生成）→ JST 月内売上
- Shopify Payments の手数料は 3.4/3.9/4.15% の 3 レンジ → API fees 合算で正確
- KOMOJU は API で fees=[]、固定率（既定 3.6%）で算出
- テスト注文は `exclude-from-accounting` タグで除外する運用ルール
- Shopify 税率設定が現状 10% でも、freee は軽減税率8%内税で記帳（顧客請求額は内税なので不変）

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
