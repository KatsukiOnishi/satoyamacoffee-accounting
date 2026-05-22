# satoyamacoffee-accounting

合同会社秋田里山デザイン（さとやまコーヒー）の月次決算自動化ハブ。
各業務システム（在庫管理 / 勤怠 / Shopify / 百貨店明細PDF / 銀行CSV 等）から数字を取得し、freee API へ仕訳・請求書として登録する。

詳細は [CLAUDE.md](./CLAUDE.md) を参照。

## セットアップ

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# .env を編集（FREEE_API_KEY / FREEE_COMPANY_ID / RESEND_API_KEY 等）
```

## CLI コマンド

```bash
accounting --help              # コマンド一覧
accounting ping --dry-run      # 動作確認（共通基盤のヘルスチェック）
accounting list-accounts       # freee 勘定科目一覧
accounting sync-hrmos --month 2026-04 --dry-run    # HRMOS → shifts 勤怠取込（dry-run）
accounting shopify-sales preview --month 2026-04   # Shopify 月次売上 集計プレビュー
accounting shopify-sales run --month 2026-04 --no-dry-run  # freee に振替伝票登録
```

デフォルトは dry-run。本番実行は `--no-dry-run` を明示する。

### 月次定期実行（launchd）

`accounting sync-hrmos` は毎月20日（HRMOS 承認締切日）に走らせる想定。`samples/com.satoyamacoffee.sync-hrmos.plist` を `~/Library/LaunchAgents/` にコピーして `launchctl load` する（詳細は plist のコメント参照）。

## テスト

```bash
pytest
```
