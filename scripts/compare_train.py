#!/usr/bin/env python3
"""
对比训练：Alpha158 vs Alpha158+自定义因子
输出IC对比 + feature importance
"""

import sys, os, pickle, json, logging
from pathlib import Path

VENV_SITE = Path(__file__).parent.parent / "qlib-env/lib/python3.12/site-packages"
if str(VENV_SITE) not in sys.path:
    sys.path.insert(0, str(VENV_SITE))

BASE_DIR = Path(__file__).parent.parent
QLIB_DATA_DIR = str(BASE_DIR / "qlib_data" / "bin")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compare_train")

import qlib
from qlib.config import REG_CN
qlib.init(provider_uri=QLIB_DATA_DIR, region=REG_CN)

from qlib.contrib.model.gbdt import LGBModel
from qlib.data.dataset import DatasetH
from qlib.contrib.data.handler import Alpha158
import numpy as np
import pandas as pd

# Import custom handler
sys.path.insert(0, str(Path(__file__).parent))
from custom_alpha_handler import CustomAlpha158, CUSTOM_FIELDS

# 日期配置
TRAIN_START = "2017-01-01"
VALID_START = "2022-07-01" 
TEST_START = "2023-01-01"
TEST_END = "2023-12-29"

LGB_PARAMS = dict(
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


def compute_ic(pred_df, label_col="label"):
    """计算每日截面IC"""
    ics = []
    for dt, group in pred_df.groupby(level=0):
        if len(group) < 5:
            continue
        ic = group["pred"].corr(group[label_col])
        if not np.isnan(ic):
            ics.append(ic)
    return np.mean(ics), np.std(ics), len(ics)


def train_and_eval(handler_cls, name, handler_kwargs):
    """训练模型并返回IC指标"""
    logger.info(f"=== 训练 {name} ===")
    
    handler = handler_cls(**handler_kwargs)
    
    dataset = DatasetH(
        handler=handler,
        segments={
            "train": (TRAIN_START, VALID_START),
            "valid": (VALID_START, TEST_START),
            "test": (TEST_START, TEST_END),
        },
    )
    
    model = LGBModel(**LGB_PARAMS)
    model.fit(dataset)
    
    # 在test集预测
    test_data = dataset.prepare("test", col_set=["feature", "label"])
    features = test_data["feature"]
    labels = test_data["label"]
    
    pred = model.predict(dataset, segment="test")
    
    # 构建预测df
    pred_df = pd.DataFrame({"pred": pred, "label": labels.iloc[:, 0]}, index=labels.index)
    
    ic_mean, ic_std, n_days = compute_ic(pred_df)
    icir = ic_mean / ic_std if ic_std > 0 else 0
    
    logger.info(f"{name}: IC={ic_mean:.4f}, ICIR={icir:.4f}, days={n_days}")
    
    # Feature importance
    importance = None
    if hasattr(model, 'model') and hasattr(model.model, 'feature_importance'):
        imp = model.model.feature_importance(importance_type='gain')
        feat_names = model.model.feature_name()
        importance = sorted(zip(feat_names, imp), key=lambda x: -x[1])
    
    return {
        "name": name,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": icir,
        "n_days": n_days,
        "importance": importance,
        "model": model,
    }


def main():
    handler_kwargs = {
        "start_time": TRAIN_START,
        "end_time": TEST_END,
        "fit_start_time": TRAIN_START,
        "fit_end_time": VALID_START,
        "instruments": "csi300",
    }
    
    # 1. Baseline: Alpha158
    baseline = train_and_eval(Alpha158, "Alpha158_baseline", handler_kwargs)
    
    # 2. Custom: Alpha158 + 自定义因子
    custom = train_and_eval(CustomAlpha158, "Alpha158_custom", handler_kwargs)
    
    # 输出对比
    print("\n" + "="*60)
    print("对比结果")
    print("="*60)
    print(f"{'指标':<20} {'Alpha158':>12} {'Alpha158+Custom':>15} {'变化':>10}")
    print("-"*60)
    print(f"{'IC Mean':<20} {baseline['ic_mean']:>12.4f} {custom['ic_mean']:>15.4f} {custom['ic_mean']-baseline['ic_mean']:>+10.4f}")
    print(f"{'IC Std':<20} {baseline['ic_std']:>12.4f} {custom['ic_std']:>15.4f}")
    print(f"{'ICIR':<20} {baseline['icir']:>12.4f} {custom['icir']:>15.4f} {custom['icir']-baseline['icir']:>+10.4f}")
    
    # 自定义因子的feature importance排名
    if custom['importance']:
        custom_factor_names = [n for n, _ in CUSTOM_FIELDS]
        print(f"\n自定义因子在LightGBM中的重要性排名:")
        print("-"*50)
        
        total_imp = sum(v for _, v in custom['importance'])
        for rank, (feat, imp) in enumerate(custom['importance'], 1):
            if feat in custom_factor_names:
                print(f"  #{rank:>3d}/{len(custom['importance'])} {feat:<25s} gain={imp:.0f} ({imp/total_imp*100:.2f}%)")
        
        print(f"\nTop 10 features overall:")
        for rank, (feat, imp) in enumerate(custom['importance'][:10], 1):
            marker = " ★" if feat in custom_factor_names else ""
            print(f"  #{rank} {feat:<25s} gain={imp:.0f}{marker}")
    
    # 保存custom模型
    model_path = BASE_DIR / "models" / "lgb_model_custom.pkl"
    with open(model_path, 'wb') as f:
        pickle.dump(custom['model'], f)
    
    meta = {
        "trained_at": __import__('datetime').datetime.now().isoformat(),
        "baseline_ic": baseline['ic_mean'],
        "custom_ic": custom['ic_mean'],
        "baseline_icir": baseline['icir'],
        "custom_icir": custom['icir'],
        "custom_factors": custom_factor_names,
        "handler": "CustomAlpha158",
    }
    with open(BASE_DIR / "models" / "lgb_model_custom_meta.json", 'w') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    
    print(f"\n模型已保存: {model_path}")
    return baseline, custom


if __name__ == "__main__":
    main()
