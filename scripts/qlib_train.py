#!/usr/bin/env python3
"""
Qlib LightGBM 模型训练脚本
- 增量更新BaoStock数据 → dump_bin → 训练LightGBM
- 模型保存到固定路径供 qlib_scorer.py 加载
- 设计为每周末自动运行
"""

import sys
import os
import json
import pickle
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 确保qlib venv的包可用
VENV_SITE = Path(__file__).parent.parent / "qlib-env/lib/python3.12/site-packages"
if str(VENV_SITE) not in sys.path:
    sys.path.insert(0, str(VENV_SITE))

BASE_DIR = Path(__file__).parent.parent
MODEL_DIR = BASE_DIR / "models"
MODEL_DIR.mkdir(exist_ok=True)
MODEL_PATH = MODEL_DIR / "lgb_model_custom.pkl"
META_PATH = MODEL_DIR / "lgb_model_meta.json"
QLIB_DATA_DIR = str(BASE_DIR / "qlib_data" / "bin")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("qlib_train")


def update_data_baostock():
    """用BaoStock增量更新数据并dump_bin到qlib格式"""
    try:
        import baostock as bs
        import pandas as pd
        from qlib.data import D
        
        logger.info("开始BaoStock增量更新...")
        lg = bs.login()
        if lg.error_code != '0':
            logger.error(f"BaoStock登录失败: {lg.error_msg}")
            return False
        
        # 获取沪深300成分股
        rs = bs.query_hs300_stocks()
        stocks = []
        while rs.next():
            stocks.append(rs.get_row_data())
        stock_df = pd.DataFrame(stocks, columns=rs.fields)
        codes = stock_df['code'].tolist()
        
        # 计算增量日期范围
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
        
        csv_dir = BASE_DIR / "qlib_data" / "csv_update"
        csv_dir.mkdir(parents=True, exist_ok=True)
        
        for code in codes:
            rs = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume,amount,turn",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2"
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(rows, columns=rs.fields)
                # 转换代码格式: sh.600000 -> SH600000
                qlib_code = code.replace('.', '').upper()
                df.to_csv(csv_dir / f"{qlib_code}.csv", index=False)
        
        bs.logout()
        
        # dump_bin
        logger.info("执行dump_bin...")
        os.system(
            f"cd {BASE_DIR} && source qlib-env/bin/activate && "
            f"python -m qlib.data.dump_bin "
            f"--csv_path {csv_dir} --qlib_dir {QLIB_DATA_DIR} "
            f"--include_fields open,high,low,close,volume,amount,turn "
            f"--date_field_name date --symbol_field_name code "
            f"--freq day 2>&1"
        )
        logger.info("数据更新完成")
        return True
    except Exception as e:
        logger.warning(f"数据更新失败(将用现有数据训练): {e}")
        return False


def train_model(skip_data_update=False):
    """训练LightGBM模型并保存"""
    import qlib
    from qlib.config import REG_CN
    
    # 初始化qlib
    qlib.init(provider_uri=QLIB_DATA_DIR, region=REG_CN)
    logger.info("Qlib初始化完成")
    
    if not skip_data_update:
        update_data_baostock()
    
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH, TSDatasetH
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from custom_alpha_handler import CustomAlpha158 as Alpha158
    
    # 日期范围
    # 自动检测数据实际日期范围
    import glob, struct
    cal_file = os.path.join(QLIB_DATA_DIR, "calendars", "day.bin")
    if os.path.exists(cal_file):
        with open(cal_file, 'rb') as f:
            data = f.read()
        n = len(data) // 4
        if n > 0:
            vals = struct.unpack(f'<{n}f', data)
            from datetime import datetime as _dt
            import pandas as pd
            last_date = pd.Timestamp('2020-01-01') + pd.Timedelta(days=vals[-1])
            data_end = last_date.strftime('%Y-%m-%d')
        else:
            data_end = datetime.now().strftime('%Y-%m-%d')
    else:
        # 尝试读text calendar
        cal_txt = os.path.join(QLIB_DATA_DIR, "calendars", "day.txt")
        if os.path.exists(cal_txt):
            with open(cal_txt) as f:
                lines = [l.strip() for l in f if l.strip()]
            data_end = lines[-1] if lines else datetime.now().strftime('%Y-%m-%d')
        else:
            data_end = datetime.now().strftime('%Y-%m-%d')
    
    train_end = data_end
    # 训练集3年，验证集最后6个月
    from dateutil.relativedelta import relativedelta
    end_dt = datetime.strptime(train_end, '%Y-%m-%d')
    train_start = (end_dt - relativedelta(years=3)).strftime('%Y-%m-%d')
    valid_start = (end_dt - relativedelta(months=6)).strftime('%Y-%m-%d')
    
    handler_config = {
        "start_time": train_start,
        "end_time": train_end,
        "fit_start_time": train_start,
        "fit_end_time": valid_start,
        "instruments": "csi300",
    }
    
    handler = Alpha158(**handler_config)
    
    dataset = DatasetH(
        handler=handler,
        segments={
            "train": (train_start, valid_start),
            "valid": (valid_start, train_end),
        },
    )
    
    # LightGBM参数
    model = LGBModel(
        loss="mse",
        colsample_bytree=0.8879,
        learning_rate=0.0421,
        subsample=0.8789,
        lambda_l1=205.6999,
        lambda_l2=580.9768,
        max_depth=8,
        num_leaves=210,
        num_threads=4,
    )
    
    logger.info("开始训练LightGBM...")
    model.fit(dataset)
    logger.info("训练完成")
    
    # 保存模型
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(model, f)
    
    # 保存元数据
    meta = {
        "trained_at": datetime.now().isoformat(),
        "train_range": f"{train_start} ~ {train_end}",
        "valid_start": valid_start,
        "model_type": "LGBModel",
        "handler": "CustomAlpha158",
        "instruments": "csi300",
    }
    with open(META_PATH, 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    logger.info(f"模型已保存: {MODEL_PATH}")
    logger.info(f"元数据: {META_PATH}")
    return model


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qlib LightGBM训练")
    parser.add_argument("--skip-data-update", action="store_true", help="跳过数据更新")
    args = parser.parse_args()
    train_model(skip_data_update=args.skip_data_update)
