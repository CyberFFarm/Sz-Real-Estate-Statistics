#!/bin/bash
# 自动批量缓存房源价格，每10分钟由 cron 触发
curl -s -X POST http://localhost:8080/api/presale/batch-cache > /dev/null 2>&1
