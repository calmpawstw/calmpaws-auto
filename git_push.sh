#!/bin/bash
# 安寵 Calm Paws — 併發安全的狀態回寫
#
# 多個 workflow 可能同時完成並推送 metrics.db。
# 這是二進位檔案，git 無法逐列合併 —— 用 --ours/--theirs 只會整份覆蓋，
# 必定丟掉其中一邊的資料（而且會「推送成功」，錯得很安靜）。
#
# 所以流程是：偵測到衝突時，取回遠端版本，在 SQLite 層級把本次結果併進去，
# 再重新提交。這樣兩邊的資料都會保留。
#
# 用法：./git_push.sh "commit 訊息" [檔案...]

set -uo pipefail

MSG="${1:-chore: 更新狀態}"
shift || true
FILES=("$@")
[ ${#FILES[@]} -eq 0 ] && FILES=("data/metrics.db")

BRANCH="${GITHUB_REF_NAME:-main}"
DB="data/metrics.db"
MAX_TRY=5

git config user.name  "calmpaws-bot"
git config user.email "calmpaws-bot@users.noreply.github.com"

stage_files () {
    local added=0
    for f in "${FILES[@]}"; do
        [ -e "$f" ] && git add -A "$f" && added=1
    done
    return $((1 - added))
}

if ! stage_files; then
    echo "沒有檔案可提交"
    exit 0
fi

if git diff --staged --quiet; then
    echo "無變更，不需推送"
    exit 0
fi

git commit -q -m "$MSG [skip ci]"

for i in $(seq 1 $MAX_TRY); do
    if git push origin "HEAD:$BRANCH" 2>/dev/null; then
        echo "✅ 已推送（第 $i 次嘗試）"
        exit 0
    fi

    echo "推送衝突，於資料庫層級合併後重試（$i/$MAX_TRY）..."

    # 保留本次執行的資料庫
    MINE=$(mktemp /tmp/cp_mine.XXXXXX.db)
    [ -f "$DB" ] && cp "$DB" "$MINE"

    git fetch -q origin "$BRANCH" || true

    # 退回到遠端最新狀態
    git reset -q --hard "origin/$BRANCH"

    # 把本次結果併進遠端版本
    if [ -f "$MINE" ] && [ -f "db_merge.py" ]; then
        echo "  合併資料庫..."
        python3 db_merge.py --base "$DB" --incoming "$MINE" --out "$DB.merged" \
            && mv "$DB.merged" "$DB" \
            || { echo "  ⚠️ 合併失敗，保留遠端版本"; }
    elif [ -f "$MINE" ]; then
        echo "  ⚠️ 找不到 db_merge.py，直接沿用本次版本（可能覆蓋遠端）"
        cp "$MINE" "$DB"
    fi
    rm -f "$MINE"

    if ! stage_files || git diff --staged --quiet; then
        echo "  合併後與遠端一致，無需推送"
        exit 0
    fi
    git commit -q -m "$MSG（併發合併）[skip ci]"

    sleep $((i * 2))
done

echo "⚠️ 推送失敗（已重試 $MAX_TRY 次），本次狀態未儲存"
exit 1
