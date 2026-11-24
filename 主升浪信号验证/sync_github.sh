#!/bin/sh
# 将工作区 主升浪信号验证/ 同步到 GitHub 仓库并推送（凭据由 helper 内部读取，零暴露）
# 用法: bash sync_github.sh "commit message"
set -u
WS=/home/work/.openclaw/workspace
REPO=$WS/.openclaw/tmp/daily-stock-ai
HELP=$WS/.openclaw/tmp/stock-analysis/git-cred-helper.sh
MSG="${1:-update: 进度同步}"

mkdir -p "$REPO/主升浪信号验证"
cp -r "$WS/主升浪信号验证/." "$REPO/主升浪信号验证/"
cd "$REPO" || exit 1
git add -A 主升浪信号验证
git -c user.name=openclaw-agent -c user.email=openclaw-agent@local commit -q -m "$MSG" 2>/dev/null
BR=$(git rev-parse --abbrev-ref HEAD)
GIT_TERMINAL_PROMPT=0 timeout 60 git -c credential.helper="$HELP" push origin "HEAD:$BR" 2>&1 \
  | sed -E 's/[A-Za-z0-9_]{30,}/<masked>/g'
