#!/bin/bash
# 自动缓存脚本 - 由 cron 触发，每30分钟运行
# 用法: bash auto_cache.sh [refresh|full|scraper]

ACTION=${1:-refresh}

case "$ACTION" in
  refresh)
    # 批量缓存房源价格
    curl -s -X POST http://localhost:8080/api/presale/batch-cache > /dev/null 2>&1
    echo "[$(date)] Batch cache triggered"
    ;;
  scraper)
    # 运行爬虫更新市场数据
    cd "$(dirname "$0")"
    python3 sz_fdc_scraper.py > /dev/null 2>&1
    echo "[$(date)] Scraper run"
    ;;
  full)
    # 全量刷新
    curl -s http://localhost:8080/api/refresh-all > /dev/null 2>&1
    echo "[$(date)] Full refresh triggered"
    ;;
  market)
    # 刷新市场数据
    curl -s -X POST http://localhost:8080/api/market/refresh > /dev/null 2>&1
    echo "[$(date)] Market refresh triggered"
    ;;
  *)
    echo "Usage: $0 [refresh|scraper|full|market]"
    ;;
esac
