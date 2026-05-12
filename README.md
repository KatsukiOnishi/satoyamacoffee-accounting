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
```

デフォルトは dry-run。本番実行は `--no-dry-run` を明示する。

## テスト

```bash
pytest
```
