# vendor-invoice タスク セットアップ手順

ベンダーからメールで届く請求書PDFをGmailから自動取込みし、freeeに「Dr.費用 / Cr.未払金」の取引として登録 → 振込済みなら未払金消し込みまで自動で実行するタスク。

スコープ:
- 取込対象: 銀行振込ベンダー請求書のみ（クレカ自動引落系・自社発行・輸入物流のスポット請求は除外）
- 実行頻度: 月次（手動 CLI）
- 振込実行は対象外（ユーザーが銀行アプリで実施）

---

## 1. ユーザー側の事前準備（初回1回のみ）

### 1-1. Google Cloud Console で OAuth クライアントID作成

1. https://console.cloud.google.com/ にアクセス（既存または新規プロジェクト）
2. 「APIとサービス」→「ライブラリ」で **Gmail API** を有効化
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth クライアントID」
4. アプリケーションの種類: **デスクトップアプリ** を選択
5. クライアントIDが作成されたら「JSONをダウンロード」
6. ダウンロードしたファイルを次の場所に配置:
   ```
   secrets/gmail_credentials.json
   ```

### 1-2. OAuth 同意画面

「OAuth 同意画面」が「テスト中」のままなら、テストユーザーに `katsuki.onishi@gmail.com` を追加しておく（公開ステータスにする必要はない）。

スコープは `https://www.googleapis.com/auth/gmail.readonly`（読み取り専用）のみ。送信権限は持たない。

### 1-3. 初回OAuth実行

```bash
cd /Users/katsuki/Claude/satoyamacoffee-accounting
uv run accounting auth gmail-init
```

- ブラウザが開き Google ログイン画面 → `katsuki.onishi@gmail.com` で同意
- 認可コードはローカル http サーバが受け取る（手作業のコピペ不要）
- `secrets/gmail_tokens.json` が保存される（gitignore 配下）

確認:
```bash
uv run accounting auth gmail-status
```

以降はトークンの自動 refresh が走るため、再度認可する必要は基本的にない（ユーザーが Google アカウント権限を取り消すか、長期間アクセスがないと再認可が必要になることがある）。

---

## 2. 日常運用

### 2-1. 月末: dry-run スキャン

```bash
uv run accounting vendor-invoice scan --days 30
```

- 過去30日分のGmailをスキャン → 候補テーブル `vendor_invoice_candidates` に書き込み
- freee 読み取り（partner / account_item 引き当て）は行うが、書き込みはしない
- Resend メールで処理予定サマリを送信（NOTIFY_EMAIL 宛）

### 2-2. 本番登録

```bash
uv run accounting vendor-invoice scan --days 30 --no-dry-run
```

- 銀行振込ベンダー請求書を freee に Deal として登録
- 同 partner / 同金額の振込が既に降りていれば自動消し込み
- 結果は Resend メールで通知

### 2-3. 候補一覧確認

```bash
uv run accounting vendor-invoice list
uv run accounting vendor-invoice list --status manual_review
uv run accounting vendor-invoice list --status unpaid
```

### 2-4. 個別承認（要手動確認の候補を処理）

```bash
uv run accounting vendor-invoice apply 42 --no-dry-run
```

`manual_review` または `pending` の候補を freee に登録する。

### 2-5. 振込実行後の消し込み再試行

```bash
uv run accounting vendor-invoice reconcile --no-dry-run
```

`registered` / `unpaid` ステータスの候補を walk し、freee 側に対応する振込 Deal が
新しく現れていれば `reconciled` に更新する。

---

## 3. 候補テーブルの status 一覧

| status | 意味 |
|--------|------|
| `pending` | dry-run 検出済み、本番登録待ち |
| `registered` | freee に登録済み（未払計上中） |
| `reconciled` | 振込と消し込み完了 |
| `unpaid` | 登録済みだが振込未了 |
| `manual_review` | 自動処理不能（partner未登録・暗号化ZIP・抽出失敗等） |
| `excluded` | 除外確定（クレカ・自社発行・輸入物流など） |
| `failed` | 何らかのエラーで処理失敗 |

---

## 4. 既知の制限

- **暗号化ZIP**: 宮﨑会計事務所の請求書は ZIP がパスワード保護されているため、`encrypted_zip` として記録し通知だけ送る。中身は処理しない。
- **partner 自動作成は行わない**: freee に未登録のベンダーは `manual_review` 扱い。freee 画面で取引先を登録した後 `apply` で処理する。
- **税区分**: デフォルト「課対仕入10%」（code 136 想定）。違う事業所では `.env` の `FREEE_TAX_CODE_FEE` で上書き。
- **複数PDF添付**: 1メールに複数PDFがあれば各添付ごとに candidate レコードを作成。
- **冪等性**: `(gmail_message_id, gmail_attachment_id)` で UNIQUE 制約。同じメールを再スキャンしても二重登録されない。

---

## 5. トラブルシューティング

| 症状 | 対処 |
|------|------|
| `accounting auth gmail-status` で `credentials_file_missing` | `secrets/gmail_credentials.json` を配置する |
| `gmail-init` 中にブラウザが開かない | コンソールに表示される URL を手動でコピーしてブラウザで開く |
| トークン refresh で 401 | `accounting auth gmail-init` で再認可（trust を取り直す） |
| partner_not_found のまま | freee 画面で取引先を作成 → `apply <id>` |
| Vision抽出失敗 | `raw_pdf_path` のPDFを手動確認 → freeeで直接登録 |

---

## 6. 開発者向け補足

- パッケージ: `accounting/tasks/vendor_invoice/`
- DB モデル: `accounting/core/vendor_invoice_candidates.py`（`init_db()` で自動作成）
- Gmail OAuth: `accounting/core/gmail_auth.py` / `accounting/connectors/gmail.py`
- 送信者ブラックリスト: `accounting/tasks/vendor_invoice/blacklists.py`
- テスト: `tests/test_vendor_invoice.py`
