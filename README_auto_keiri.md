# auto-keiri — 週次バッチ運用手順

freee「自動で経理 > まとめて入力 > 未処理」の wallet_txn を、可能な限り自動で
消込・登録するための週次バッチ CLI 群。仕様の詳細は
`/Users/katsuki/Documents/Claude/Projects/月次決算自動化プロジェクト/auto-keiri_仕様書_for_Claude_Code.md`
を参照。

## 3 タスク

| CLI | 役割 |
|---|---|
| `accounting ar-reconcile run` | 売掛金消込（法人入金 → freee 未決済請求書） |
| `accounting auto-classify run` | 信頼度付きの自動仕訳（shadow / production） |
| `accounting email-digest send` | 週次1通のダイジェストメール |

## 初期セットアップ

1. `.env` に以下が揃っていることを確認:
   - `FREEE_API_KEY` / `FREEE_COMPANY_ID`（既存）
   - `ANTHROPIC_API_KEY`（auto-classify が Claude 呼び出しに使う）
   - `RESEND_API_KEY`, `FROM_EMAIL`, `NOTIFY_EMAIL`（email-digest）
2. DB 初期化（`accounting auth status` や `accounting ping` 等を 1 回実行すれば
   `init_db()` が呼ばれて 4 テーブルが作成される）。
3. モードは初期値 `shadow` で `system_settings` に入る。確認:
   ```bash
   accounting auto-classify get-mode
   ```

## 週次バッチ実行

### 手動

```bash
accounting ar-reconcile run --no-dry-run --days 14
accounting auto-classify run --no-dry-run --days 14
accounting email-digest send --no-dry-run
```

### スクリプト経由

```bash
./scripts/run_weekly_auto_keiri.sh
```

`AUTO_KEIRI_DRY_RUN=1` を立てると全段 dry-run。

### scheduled-tasks / launchd 登録

scheduled-tasks 登録は本リポジトリには含めない。Cowork から
「毎週月曜 09:00 JST に `./scripts/run_weekly_auto_keiri.sh` を実行」を登録する。

`launchd` で組む場合は `samples/com.satoyamacoffee.sync-hrmos.plist` と同じ形式で
`StartCalendarInterval` を `Weekday=2, Hour=9, Minute=0` 等にすればよい。

## shadow → production 切替

```bash
# 状態確認
accounting auto-classify get-mode
# → mode='shadow', threshold_high=0.85, threshold_low=0.6

# 切替（system_settings に永続化、切替通知メールも飛ぶ）
accounting auto-classify set-mode --mode production --reason "2週間運用で精度97%確認"

# 不具合があれば戻す
accounting auto-classify set-mode --mode shadow --reason "誤分類多発"
```

しきい値の手動チューニング:

```bash
accounting auto-classify set-threshold --high 0.9 --low 0.7
```

## 除外フィルタ（仕様書 §5-1, §5-3）

ar-reconcile / auto-classify は以下を **自動的に除外** する:

- 振込 + 個人名カナ（給与振込・個人入金）
- そごう / 西武 関連の振込（百貨店明細取込タスク側で処理）
- 日本公庫 / 公庫（借入返済按分、手動）
- 振込手数料 100-300 円（journal-rules 側で対応想定）
- 銀行利息（取引額小、ルール化済み）

新しいパターンが出てきたら `accounting/tasks/ar_reconcile/excluder.py` を編集する。

## 信頼度しきい値（仕様書 §5-4）

| confidence | action_taken | freee 登録 |
|---|---|---|
| ≥ 0.85 | registered（production） | する |
| 0.6-0.85 | review_required | しない（要確認） |
| < 0.6 | skipped | しない |

shadow モードでは confidence にかかわらず `shadow_logged` で記録のみ。

## 冪等性

- `executed_operations`(`task`, `external_id`) で freee 書き込みを二重に走らせない
- ar-reconcile: `external_id = ar-reconcile:wallet_txn:{wallet_txn_id}`
- auto-classify: `external_id = auto-classify:wallet_txn:{wallet_txn_id}`

dry-run / shadow は executed_operations を書かない（rehearsal を本番冪等性に混ぜない）。

## トラブルシューティング

- メールが届かない: `accounting ping` が通ること + `RESEND_API_KEY` 確認 + Resend ダッシュボードで bounce 確認
- shadow なのに freee に書き込まれた: `auto_classify_candidates.mode='shadow'` の行が
  `action_taken='registered'` になっていないか確認。仕様上ありえないが、報告して欲しい
- Claude エラーで全件 failed: `ANTHROPIC_API_KEY` か `claude-sonnet-4-6` の API 名を確認。
  `accounting/connectors/anthropic_classifier.py` で model 指定を上書き可能
