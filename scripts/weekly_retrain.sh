#!/bin/bash
set -e
LOG_DIR="/root/.openclaw/workspace/stock-trading/logs"
LOG_FILE="$LOG_DIR/weekly_retrain_$(date +'%Y%m%d_%H%M%S').log"
mkdir -p "$LOG_DIR"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "===== Qlib Weekly Retrain Start: $(date) ====="
cd /root/.openclaw/workspace/stock-trading || exit 1
source qlib-env/bin/activate

# 1. 增量采集最近2周数据
if [ -x scripts/collect_baostock_incremental.sh ]; then
  echo "[Step 1] Collecting BaoStock incremental data..."
  bash scripts/collect_baostock_incremental.sh 14
else
  echo "[Step 1] collect_baostock_incremental.sh not found or not executable, skipped."
fi

# 2. dump_bin转换
if [ -x scripts/dump_bin.sh ]; then
  echo "[Step 2] Running dump_bin conversion..."
  bash scripts/dump_bin.sh
else
  echo "[Step 2] dump_bin.sh not found or not executable, skipped."
fi

# 3. 更新日历和instruments
if [ -x scripts/update_calendar_and_instruments.sh ]; then
  echo "[Step 3] Updating calendar and instruments..."
  bash scripts/update_calendar_and_instruments.sh
else
  echo "[Step 3] update_calendar_and_instruments.sh not found or not executable, skipped."
fi

# 4. 训练模型
if [ -f scripts/qlib_train.py ]; then
  echo "[Step 4] Training model..."
  python scripts/qlib_train.py
else
  echo "[Step 4] qlib_train.py not found, aborting."
  exit 1
fi

# 5. 对比新旧模型IC（可选）
if [ -x scripts/compare_ic.sh ]; then
  echo "[Step 5] Comparing IC of old and new models..."
  bash scripts/compare_ic.sh
else
  echo "[Step 5] compare_ic.sh not found or not executable, skipped."
fi

echo "===== Qlib Weekly Retrain Complete: $(date) ====="
