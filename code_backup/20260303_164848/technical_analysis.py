#!/usr/bin/env python3
"""
技术分析模块 - 计算各种技术指标和交易信号
"""

import numpy as np
from typing import List, Dict, Tuple

def calculate_atr(klines: List[Dict], period: int = 20) -> float:
    """计算ATR（平均真实波幅），返回百分比形式"""
    if not klines or len(klines) < period + 1:
        return 0.02  # 默认2%
    trs = []
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_close = klines[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr / prev_close if prev_close > 0 else 0)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.02
    return sum(trs[-period:]) / period

def calculate_hybrid_atr(klines: List[Dict], realtime: Dict = None) -> float:
    """混合ATR = max(20日ATR, 5日ATR, 当日实时振幅)
    解决纯20日ATR在暴涨首日的滞后问题"""
    atr20 = calculate_atr(klines, 20)
    atr5 = calculate_atr(klines, 5)
    
    # 当日实时振幅
    daily_range = 0.0
    if realtime:
        high = realtime.get("high", 0)
        low = realtime.get("low", 0)
        pre_close = realtime.get("pre_close", 0)
        if pre_close > 0 and high > 0 and low > 0:
            daily_range = (high - low) / pre_close
    
    return max(atr20, atr5, daily_range)


def calculate_ma(prices: List[float], period: int) -> List[float]:
    """计算移动平均线"""
    if len(prices) < period:
        return [None] * len(prices)
    
    ma = [None] * (period - 1)
    for i in range(period - 1, len(prices)):
        ma.append(round(np.mean(prices[i - period + 1:i + 1]), 3))
    return ma

def calculate_ema(prices: List[float], period: int) -> List[float]:
    """计算指数移动平均线"""
    if len(prices) < period:
        return [None] * len(prices)
    
    multiplier = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(np.mean(prices[:period]))  # 第一个EMA用SMA
    
    for i in range(period, len(prices)):
        ema.append(round(prices[i] * multiplier + ema[-1] * (1 - multiplier), 3))
    
    return ema

def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """计算MACD指标"""
    if len(prices) < slow + signal:
        return {"dif": [], "dea": [], "macd": [], "signal": None}
    
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    
    dif = []
    for i in range(len(prices)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif.append(round(ema_fast[i] - ema_slow[i], 3))
        else:
            dif.append(None)
    
    # 计算DEA (DIF的EMA)
    dif_values = [d for d in dif if d is not None]
    if len(dif_values) < signal:
        return {"dif": dif, "dea": [], "macd": [], "signal": None}
    
    dea = calculate_ema(dif_values, signal)
    
    # 对齐到原始长度
    pad_len = len(dif) - len(dea)
    dea = [None] * pad_len + dea
    
    # 计算MACD柱
    macd_bar = []
    for i in range(len(dif)):
        if dif[i] is not None and dea[i] is not None:
            macd_bar.append(round((dif[i] - dea[i]) * 2, 3))
        else:
            macd_bar.append(None)
    
    # 判断信号
    signal_type = None
    if len(macd_bar) >= 2 and macd_bar[-1] is not None and macd_bar[-2] is not None:
        if macd_bar[-2] < 0 and macd_bar[-1] > 0:
            signal_type = "golden_cross"  # 金叉
        elif macd_bar[-2] > 0 and macd_bar[-1] < 0:
            signal_type = "death_cross"  # 死叉
        elif macd_bar[-1] > macd_bar[-2] > 0:
            signal_type = "bullish"  # 多头
        elif macd_bar[-1] < macd_bar[-2] < 0:
            signal_type = "bearish"  # 空头
    
    return {
        "dif": dif,
        "dea": dea,
        "macd": macd_bar,
        "signal": signal_type
    }

def calculate_rsi(prices: List[float], period: int = 14) -> List[float]:
    """计算RSI指标"""
    if len(prices) < period + 1:
        return [None] * len(prices)
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    
    rsi = [None] * period
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    if avg_loss == 0:
        rsi.append(100)
    else:
        rs = avg_gain / avg_loss
        rsi.append(round(100 - 100 / (1 + rs), 2))
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(round(100 - 100 / (1 + rs), 2))
    
    return rsi

def calculate_kdj(high: List[float], low: List[float], close: List[float], 
                  n: int = 9, m1: int = 3, m2: int = 3) -> Dict:
    """计算KDJ指标"""
    if len(close) < n:
        return {"k": [], "d": [], "j": [], "signal": None}
    
    rsv = []
    for i in range(len(close)):
        if i < n - 1:
            rsv.append(None)
        else:
            lowest = min(low[i-n+1:i+1])
            highest = max(high[i-n+1:i+1])
            if highest == lowest:
                rsv.append(50)
            else:
                rsv.append(round((close[i] - lowest) / (highest - lowest) * 100, 2))
    
    k = []
    d = []
    
    first_valid = n - 1
    for i in range(len(rsv)):
        if rsv[i] is None:
            k.append(None)
            d.append(None)
        elif i == first_valid:
            k.append(50)  # 初始K值
            d.append(50)  # 初始D值
        else:
            new_k = round((k[-1] * (m1 - 1) + rsv[i]) / m1, 2)
            new_d = round((d[-1] * (m2 - 1) + new_k) / m2, 2)
            k.append(new_k)
            d.append(new_d)
    
    j = []
    for i in range(len(k)):
        if k[i] is not None and d[i] is not None:
            j.append(round(3 * k[i] - 2 * d[i], 2))
        else:
            j.append(None)
    
    # 判断信号
    signal_type = None
    if len(k) >= 2 and k[-1] is not None and k[-2] is not None:
        if k[-2] < d[-2] and k[-1] > d[-1]:
            signal_type = "golden_cross"
        elif k[-2] > d[-2] and k[-1] < d[-1]:
            signal_type = "death_cross"
        elif k[-1] < 20 and d[-1] < 20:
            signal_type = "oversold"  # 超卖
        elif k[-1] > 80 and d[-1] > 80:
            signal_type = "overbought"  # 超买
    
    return {"k": k, "d": d, "j": j, "signal": signal_type}

def calculate_boll(prices: List[float], period: int = 20, std_dev: int = 2) -> Dict:
    """计算布林带"""
    if len(prices) < period:
        return {"upper": [], "middle": [], "lower": [], "width": [], "signal": None}
    
    middle = calculate_ma(prices, period)
    upper = []
    lower = []
    width = []
    
    for i in range(len(prices)):
        if middle[i] is None:
            upper.append(None)
            lower.append(None)
            width.append(None)
        else:
            std = np.std(prices[i-period+1:i+1])
            upper.append(round(middle[i] + std_dev * std, 3))
            lower.append(round(middle[i] - std_dev * std, 3))
            width.append(round((upper[-1] - lower[-1]) / middle[i] * 100, 2))
    
    # 判断信号
    signal_type = None
    if upper[-1] is not None:
        current_price = prices[-1]
        if current_price >= upper[-1]:
            signal_type = "touch_upper"  # 触及上轨
        elif current_price <= lower[-1]:
            signal_type = "touch_lower"  # 触及下轨
        elif width[-1] is not None and width[-2] is not None:
            if width[-1] < width[-2] * 0.8:
                signal_type = "squeeze"  # 缩口
    
    return {"upper": upper, "middle": middle, "lower": lower, "width": width, "signal": signal_type}

def calculate_volume_ratio(volumes: List[int], period: int = 5) -> float:
    """计算量比"""
    if len(volumes) < period + 1:
        return 1.0
    
    avg_volume = np.mean(volumes[-(period+1):-1])
    if avg_volume == 0:
        return 1.0
    
    return round(volumes[-1] / avg_volume, 2)

def analyze_trend(prices: List[float]) -> Dict:
    """趋势分析"""
    if len(prices) < 20:
        return {"trend": "unknown", "strength": 0, "ma_status": {}}
    
    ma5 = calculate_ma(prices, 5)
    ma10 = calculate_ma(prices, 10)
    ma20 = calculate_ma(prices, 20)
    
    current_price = prices[-1]
    
    # MA排列状态
    ma_status = {
        "price_above_ma5": current_price > ma5[-1] if ma5[-1] else False,
        "price_above_ma10": current_price > ma10[-1] if ma10[-1] else False,
        "price_above_ma20": current_price > ma20[-1] if ma20[-1] else False,
        "ma5_above_ma10": ma5[-1] > ma10[-1] if ma5[-1] and ma10[-1] else False,
        "ma10_above_ma20": ma10[-1] > ma20[-1] if ma10[-1] and ma20[-1] else False,
    }
    
    # 判断趋势
    bullish_count = sum(ma_status.values())
    
    if bullish_count >= 4:
        trend = "strong_bullish"
        strength = bullish_count / 5
    elif bullish_count >= 3:
        trend = "bullish"
        strength = bullish_count / 5
    elif bullish_count <= 1:
        trend = "strong_bearish"
        strength = -(5 - bullish_count) / 5
    elif bullish_count == 2:
        trend = "bearish"
        strength = -(5 - bullish_count) / 5
    else:
        trend = "neutral"
        strength = 0
    
    return {"trend": trend, "strength": round(strength, 2), "ma_status": ma_status}

def generate_signals(klines: List[Dict]) -> Dict:
    """综合分析生成交易信号"""
    if len(klines) < 30:
        return {"action": "hold", "confidence": 0, "reasons": ["数据不足"]}
    
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    volumes = [k["volume"] for k in klines]
    
    # 计算各种指标
    macd = calculate_macd(closes)
    rsi = calculate_rsi(closes)
    kdj = calculate_kdj(highs, lows, closes)
    boll = calculate_boll(closes)
    trend = analyze_trend(closes)
    vol_ratio = calculate_volume_ratio(volumes)
    
    buy_signals = []
    sell_signals = []
    
    # MACD信号
    if macd["signal"] == "golden_cross":
        buy_signals.append("MACD金叉")
    elif macd["signal"] == "death_cross":
        sell_signals.append("MACD死叉")
    
    # RSI信号
    current_rsi = rsi[-1] if rsi[-1] else 50
    if current_rsi < 30:
        buy_signals.append(f"RSI超卖({current_rsi})")
    elif current_rsi > 70:
        sell_signals.append(f"RSI超买({current_rsi})")
    
    # KDJ信号
    if kdj["signal"] == "golden_cross":
        buy_signals.append("KDJ金叉")
    elif kdj["signal"] == "death_cross":
        sell_signals.append("KDJ死叉")
    elif kdj["signal"] == "oversold":
        buy_signals.append("KDJ超卖")
    elif kdj["signal"] == "overbought":
        sell_signals.append("KDJ超买")
    
    # 布林带信号
    if boll["signal"] == "touch_lower":
        buy_signals.append("触及布林下轨")
    elif boll["signal"] == "touch_upper":
        sell_signals.append("触及布林上轨")
    
    # 趋势信号
    if trend["trend"] in ["strong_bullish", "bullish"]:
        buy_signals.append(f"趋势向上({trend['trend']})")
    elif trend["trend"] in ["strong_bearish", "bearish"]:
        sell_signals.append(f"趋势向下({trend['trend']})")
    
    # 量价配合
    price_change = (closes[-1] - closes[-2]) / closes[-2] * 100
    if vol_ratio > 1.5 and price_change > 1:
        buy_signals.append(f"放量上涨(量比{vol_ratio})")
    elif vol_ratio > 1.5 and price_change < -1:
        sell_signals.append(f"放量下跌(量比{vol_ratio})")
    
    # 综合判断
    buy_score = len(buy_signals)
    sell_score = len(sell_signals)
    
    if buy_score >= 3 and buy_score > sell_score:
        action = "buy"
        confidence = min(buy_score * 0.2, 0.9)
        reasons = buy_signals
    elif sell_score >= 3 and sell_score > buy_score:
        action = "sell"
        confidence = min(sell_score * 0.2, 0.9)
        reasons = sell_signals
    elif buy_score >= 2 and sell_score == 0:
        action = "weak_buy"
        confidence = buy_score * 0.15
        reasons = buy_signals
    elif sell_score >= 2 and buy_score == 0:
        action = "weak_sell"
        confidence = sell_score * 0.15
        reasons = sell_signals
    else:
        action = "hold"
        confidence = 0.3
        reasons = buy_signals + sell_signals if buy_signals or sell_signals else ["无明显信号"]
    
    return {
        "action": action,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "indicators": {
            "macd": macd["signal"],
            "rsi": current_rsi,
            "kdj": kdj["signal"],
            "boll": boll["signal"],
            "trend": trend["trend"],
            "vol_ratio": vol_ratio
        }
    }

if __name__ == "__main__":
    # 测试代码
    test_prices = [10, 10.2, 10.5, 10.3, 10.8, 11.0, 10.9, 11.2, 11.5, 11.3,
                   11.8, 12.0, 11.9, 12.2, 12.5, 12.3, 12.8, 13.0, 12.9, 13.2,
                   13.5, 13.3, 13.8, 14.0, 13.9, 14.2, 14.5, 14.3, 14.8, 15.0]
    
    print("MA5:", calculate_ma(test_prices, 5)[-5:])
    print("RSI:", calculate_rsi(test_prices)[-5:])
    print("Trend:", analyze_trend(test_prices))
