#!/usr/bin/env python3
"""
Qlib LightGBM 预测打分模块
- 加载训练好的模型，对股票列表推理
- 输出归一化到0-100的ml_score
- 提供 get_ml_scores(stock_list) 接口
"""

import os
import sys
import pickle
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 确保qlib venv的包可用
VENV_SITE = Path(__file__).parent.parent / "qlib-env/lib/python3.12/site-packages"
if str(VENV_SITE) not in sys.path:
    sys.path.insert(0, str(VENV_SITE))

BASE_DIR = Path(__file__).parent.parent
MODEL_PATH = BASE_DIR / "models" / "lgb_model_custom.pkl"
QLIB_DATA_DIR = str(BASE_DIR / "qlib_data" / "bin")

logger = logging.getLogger("qlib_scorer")

# 全局缓存，避免重复加载
_model_cache = None
_qlib_initialized = False


def _code_baostock_to_qlib(code: str) -> str:
    """sh.600000 -> SH600000"""
    return code.replace('.', '').upper()


def _code_qlib_to_baostock(code: str) -> str:
    """SH600000 -> sh.600000"""
    prefix = code[:2].lower()
    num = code[2:]
    return f"{prefix}.{num}"


def _ensure_qlib():
    """确保qlib已初始化"""
    global _qlib_initialized
    if _qlib_initialized:
        return True
    try:
        import qlib
        from qlib.config import REG_CN
        qlib.init(provider_uri=QLIB_DATA_DIR, region=REG_CN)
        _qlib_initialized = True
        return True
    except Exception as e:
        logger.error(f"Qlib初始化失败: {e}")
        return False


def _load_model():
    """加载模型（带缓存）"""
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    if not MODEL_PATH.exists():
        logger.warning(f"模型文件不存在: {MODEL_PATH}")
        return None
    try:
        with open(MODEL_PATH, 'rb') as f:
            _model_cache = pickle.load(f)
        logger.info(f"模型加载成功: {MODEL_PATH}")
        return _model_cache
    except Exception as e:
        logger.error(f"模型加载失败: {e}")
        return None


def get_ml_scores(stock_list: list) -> dict:
    """
    对股票列表做ML推理打分
    
    Args:
        stock_list: BaoStock格式的股票代码列表，如 ["sh.600000", "sz.000001"]
    
    Returns:
        dict: {stock_code: ml_score}，ml_score归一化到0-100
        失败返回空dict
    """
    if not stock_list:
        return {}
    
    model = _load_model()
    if model is None:
        logger.warning("模型未加载，返回空分数")
        return {}
    
    if not _ensure_qlib():
        return {}
    
    try:
        from qlib.data.dataset import DatasetH
        import sys; sys.path.insert(0, os.path.dirname(__file__)); from custom_alpha_handler import CustomAlpha158 as Alpha158
        
        # 转换代码格式
        qlib_codes = [_code_baostock_to_qlib(c) for c in stock_list]
        
        # 构建推理数据集（最近30个交易日的特征）
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        
        handler = Alpha158(
            start_time=start_date,
            end_time=end_date,
            fit_start_time=start_date,
            fit_end_time=end_date,
            instruments=qlib_codes,
        )
        
        dataset = DatasetH(
            handler=handler,
            segments={"test": (start_date, end_date)},
        )
        
        # 获取特征数据，去掉LABEL列
        df = dataset.prepare("test", col_set="feature")
        # 去掉任何LABEL列（CustomAlpha158可能混入LABEL0）
        label_cols = [c for c in df.columns if 'LABEL' in str(c).upper()]
        if label_cols:
            logger.info(f"去掉LABEL列: {label_cols}")
            df = df.drop(columns=label_cols)
        
        # 取最新交易日，用fillna(0)而非dropna（自定义因子可能有NaN）
        latest_date = df.index.get_level_values('datetime').max()
        latest = df.xs(latest_date, level='datetime').fillna(0)
        
        if latest.empty:
            logger.warning("最新日期无数据")
            return {}
        
        logger.info(f"推理特征数: {latest.shape[1]}, 股票数: {latest.shape[0]}")
        
        # 用模型内部的predict（绕过qlib Dataset wrapper）
        import numpy as np
        if hasattr(model, 'model'):
            # qlib LGBModel wrapper
            pred_values = model.model.predict(latest.values)
        else:
            pred_values = model.predict(latest.values)
        
        # 构建结果
        latest_scores = {}
        for i, inst in enumerate(latest.index):
            latest_scores[inst] = float(pred_values[i])
        
        if not latest_scores:
            return {}
        
        # 归一化到0-100（用min-max + clip）
        values = list(latest_scores.values())
        min_v = min(values)
        max_v = max(values)
        spread = max_v - min_v if max_v != min_v else 1.0
        
        result = {}
        for code_bao in stock_list:
            qlib_code = _code_baostock_to_qlib(code_bao)
            if qlib_code in latest_scores:
                raw = latest_scores[qlib_code]
                normalized = (raw - min_v) / spread * 100.0
                normalized = max(0.0, min(100.0, normalized))
                result[code_bao] = round(normalized, 2)
        
        logger.info(f"ML打分完成: {len(result)}/{len(stock_list)}只股票")
        return result
        
    except Exception as e:
        logger.error(f"ML推理失败: {e}", exc_info=True)
        return {}


def reload_model():
    """强制重新加载模型"""
    global _model_cache
    _model_cache = None
    return _load_model() is not None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 测试
    test_stocks = ["sh.600519", "sz.000001", "sh.601318"]
    scores = get_ml_scores(test_stocks)
    print(f"测试结果: {scores}")
