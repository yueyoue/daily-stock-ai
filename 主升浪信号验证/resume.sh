#!/usr/bin/env bash
# 一键续跑：已抓数据自动跳过，可随时 Ctrl-C 中断后重跑
set -u
cd "$(dirname "$0")/scripts"
mkdir -p ../../.openclaw/tmp/stock-analysis

echo "== [1/6] 股票清单 =="
python3 01_fetch_universe.py

echo "== [2/6] 日线（前复权 + 不复权），断点续跑 =="
python3 02_fetch_kline.py both 2>&1 | tee ../../.openclaw/tmp/stock-analysis/fetch_kline.log

echo "== [3/6] 每日估值 =="
python3 03_fetch_valuation.py 2>&1 | tee ../../.openclaw/tmp/stock-analysis/fetch_val.log

echo "== [4/6] 定期业绩 =="
python3 04_fetch_financials.py 2>&1 | tee ../../.openclaw/tmp/stock-analysis/fetch_fin.log

echo "== [5/6] 主升浪识别 + 特征 =="
python3 05_detect.py

echo "== [6/6] 统计验证 + 案例校准 =="
python3 06_stats.py
python3 07_cases_check.py

echo "== 完成。请更新 PROGRESS.md 并查看 tables/ =="
