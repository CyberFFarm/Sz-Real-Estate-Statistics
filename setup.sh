#!/bin/bash
# 深圳房地产数据分析系统 - 数据库组装脚本
# 使用方法: bash setup.sh

echo "正在组装数据库..."
cat data/szfdc_data_part_* > szfdc_data.db
echo "✅ 数据库组装完成 ($(du -h szfdc_data.db | cut -f1))"
echo ""
echo "启动服务: python3 server.py"
echo "访问: http://localhost:8080"
