#!/bin/sh
# Git 凭据 helper：get 模式从本地 token 文件读取，凭据零暴露
case "${1:-get}" in
  get)
    echo "username=x-access-token"
    echo "password=$(cat /home/work/.openclaw/workspace/.openclaw/tmp/token.txt)"
    ;;
esac
exit 0
