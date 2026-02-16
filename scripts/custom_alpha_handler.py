#!/usr/bin/env python3
"""
自定义Alpha因子Handler - 在Alpha158基础上追加A股特色因子

方案：不走外部API（太慢），直接从Qlib已有的OHLCV数据通过表达式引擎计算因子。
Qlib Expression Engine 支持的算子足够实现连板/情绪等因子。

追加因子：
1. consecutive_limit_up: 基于涨跌幅>9.8%的连续天数（近似）
2. first_board_premium: 涨停次日收益率
3. limit_up_flag: 当日是否涨停（辅助）
4. vol_price_corr_5: 5日量价相关性（情绪代理）
5. up_ratio_5: 近5日上涨天数占比（微观情绪）
6. vol_surge: 成交量突增比率（资金异动代理）
7. high_low_range_5: 5日振幅均值（波动率/情绪）
8. close_to_high_5: 近5日收盘价距最高价比率（强弱指标）
"""

from qlib.contrib.data.handler import Alpha158
from qlib.data.dataset.handler import DataHandlerLP


# 自定义因子表达式（Qlib Expression Engine语法）
CUSTOM_FIELDS = [
    # === 连板相关因子 ===
    # 当日涨停标记: (close/Ref(close,1) - 1) >= 0.098
    ("limit_up_flag", "Gt($close/Ref($close,1)-1, 0.098)"),
    
    # 连续涨停天数（近似：用连续满足条件的天数，最多看5天）
    # Qlib没有直接的consecutive count，用累加近似
    ("consecutive_limit_up",
     "If(Gt($close/Ref($close,1)-1, 0.098), "
     "If(Gt(Ref($close,1)/Ref($close,2)-1, 0.098), "
     "If(Gt(Ref($close,2)/Ref($close,3)-1, 0.098), 3, 2), 1), 0)"),
    
    # 首板次日溢价: 昨日涨停且前日未涨停时，今日的收益
    ("first_board_premium",
     "If(And(Gt(Ref($close,1)/Ref($close,2)-1, 0.098), "
     "Le(Ref($close,2)/Ref($close,3)-1, 0.098)), "
     "$close/Ref($close,1)-1, 0)"),
    
    # === 量价因子（替代难以获取的融资融券/北向资金）===
    # 5日量价相关性（高相关=趋势，低相关=分歧）
    ("vol_price_corr_5", "Corr($close, $volume, 5)"),
    
    # 成交量突增比率 (今日量 / 20日均量)
    ("vol_surge", "$volume / Mean($volume, 20)"),
    
    # 5日换手（量比均值）
    ("vol_ratio_5", "Mean($volume, 5) / Mean($volume, 20)"),
    
    # === 情绪/动量因子 ===
    # 近5日上涨天数占比
    ("up_ratio_5",
     "(Gt($close/Ref($close,1)-1, 0) + Gt(Ref($close,1)/Ref($close,2)-1, 0) + "
     "Gt(Ref($close,2)/Ref($close,3)-1, 0) + Gt(Ref($close,3)/Ref($close,4)-1, 0) + "
     "Gt(Ref($close,4)/Ref($close,5)-1, 0)) / 5"),
    
    # 5日振幅均值
    ("high_low_range_5", "Mean(($high - $low) / $close, 5)"),
    
    # 近5日收盘价距最高价比率（越接近1越强）
    ("close_to_high_5", "$close / Max($high, 5)"),
    
    # 近10日最大回撤
    ("max_drawdown_10", "1 - $close / Max($high, 10)"),
    
    # 20日动量分歧度（波动率/均值）
    ("momentum_dispersion", "Std($close/Ref($close,1)-1, 20) / (Abs(Mean($close/Ref($close,1)-1, 20)) + 1e-8)"),
]


def get_custom_fields():
    """返回自定义因子的fields_group格式，适配Alpha158"""
    fields = []
    names = []
    for name, expr in CUSTOM_FIELDS:
        fields.append(expr)
        names.append(name)
    return fields, names


class CustomAlpha158(Alpha158):
    """在Alpha158基础上追加自定义因子"""
    
    def get_feature_config(self):
        """重写以追加自定义因子"""
        fields, names = super().get_feature_config()
        
        custom_fields, custom_names = get_custom_fields()
        fields += custom_fields
        names += custom_names
        
        return fields, names


if __name__ == "__main__":
    # 测试因子表达式
    import sys, os
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / "qlib-env/lib/python3.12/site-packages"))
    
    import qlib
    from qlib.config import REG_CN
    
    BASE_DIR = __import__('pathlib').Path(__file__).parent.parent
    qlib.init(provider_uri=str(BASE_DIR / "qlib_data" / "bin"), region=REG_CN)
    
    handler = CustomAlpha158(
        start_time="2020-01-01",
        end_time="2020-03-31",
        fit_start_time="2020-01-01",
        fit_end_time="2020-03-31",
        instruments="csi300",
    )
    
    from qlib.data.dataset import DatasetH
    ds = DatasetH(handler=handler, segments={"train": ("2020-01-01", "2020-03-31")})
    df = ds.prepare("train")
    
    print(f"Shape: {df.shape}")
    print(f"Columns ({len(df.columns)}):")
    for c in df.columns:
        print(f"  {c}")
    print(f"\nCustom factor stats:")
    custom_names = [n for n, _ in CUSTOM_FIELDS]
    for col in df.columns:
        if col[0] in custom_names:
            print(f"  {col[0]}: mean={df[col].mean():.4f}, std={df[col].std():.4f}, null%={df[col].isnull().mean()*100:.1f}%")
