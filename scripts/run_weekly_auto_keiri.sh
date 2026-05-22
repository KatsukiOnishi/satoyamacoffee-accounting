#!/usr/bin/env bash
#
# auto-keiri 週次バッチ（scheduled-tasks / launchd / cron からの起動を想定）
# 仕様書 §6 の3本立てを順次実行する。
#
# 順序:
#   1. ar-reconcile   — 売掛金消込
#   2. auto-classify  — 信頼度付き分類（shadow / production はDBで決定）
#   3. email-digest   — 週次1通ダイジェスト
#
# 環境変数:
#   AUTO_KEIRI_DAYS    対象日数（既定 14）
#   AUTO_KEIRI_DRY_RUN  "1" なら全段 dry-run（既定: 本番）
#
# 推奨スケジュール: 毎週月曜 09:00 JST
#
set -euo pipefail

cd "$(dirname "$0")/.."

DAYS="${AUTO_KEIRI_DAYS:-14}"
if [[ "${AUTO_KEIRI_DRY_RUN:-0}" == "1" ]]; then
    DRY_FLAG="--dry-run"
else
    DRY_FLAG="--no-dry-run"
fi

echo "[auto-keiri] start: days=${DAYS}  ${DRY_FLAG}"

# uv が無い環境（システム python のみ）でも accounting CLI が走るよう、両対応する。
if command -v uv >/dev/null 2>&1; then
    RUN="uv run accounting"
else
    RUN="accounting"
fi

$RUN ar-reconcile run $DRY_FLAG --days "$DAYS"
$RUN auto-classify run $DRY_FLAG --days "$DAYS"
$RUN email-digest send $DRY_FLAG

echo "[auto-keiri] done"
