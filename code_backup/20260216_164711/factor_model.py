#!/usr/bin/env python3
"""
多因子选股模型
综合多个因子对股票进行评分和排序
"""

from typing import Dict, List, Tuple
import numpy as np
from datetime import datetime, timedelta


class FactorModel:
    """多因子选股模型"""
    
    # 因子权重配置
    DEFAULT_WEIGHTS = {
        "momentum": 0.25,      # 动量因子
        "technical": 0.25,     # 技术因子  
        "volume": 0.20,        # 量价因子
        "money_flow": 0.15,    # 资金因子
        "sentiment": 0.15,     # 情绪因子
    }
    
    def __init__(self, weights: Dict[str, float] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS
        
    def calculate_momentum_factor(self, klines: List[Dict]) -> Dict:
        """
        计算动量因子
        
        子因子:
        - 5日涨幅
        - 20日涨幅
        - 相对强弱 (RS)
        """
        if len(klines) < 20:
            return {"score": 50, "details": {"error": "数据不足"}}
        
        closes = [k["close"] for k in klines]
        
        # 5日涨幅
        ret_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
        
        # 20日涨幅
        ret_20d = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
        
        # 相对强弱 (简化版：用涨跌天数比)
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        up_days = sum(1 for c in changes[-20:] if c > 0)
        down_days = sum(1 for c in changes[-20:] if c < 0)
        rs_ratio = up_days / max(down_days, 1)
        
        # 评分 (0-100)
        score = 50
        
        # 5日动量得分
        if ret_5d > 10:
            score += 15
        elif ret_5d > 5:
            score += 10
        elif ret_5d > 2:
            score += 5
        elif ret_5d < -10:
            score -= 15
        elif ret_5d < -5:
            score -= 10
        elif ret_5d < -2:
            score -= 5
        
        # 20日动量得分
        if ret_20d > 20:
            score += 15
        elif ret_20d > 10:
            score += 10
        elif ret_20d > 5:
            score += 5
        elif ret_20d < -20:
            score -= 15
        elif ret_20d < -10:
            score -= 10
        elif ret_20d < -5:
            score -= 5
        
        # RS比率得分
        if rs_ratio > 2:
            score += 10
        elif rs_ratio > 1.5:
            score += 5
        elif rs_ratio < 0.5:
            score -= 10
        elif rs_ratio < 0.7:
            score -= 5
        
        # 限制在0-100
        score = max(0, min(100, score))
        
        return {
            "score": score,
            "details": {
                "ret_5d": round(ret_5d, 2),
                "ret_20d": round(ret_20d, 2),
                "rs_ratio": round(rs_ratio, 2),
                "up_days": up_days,
                "down_days": down_days
            }
        }
    
    def calculate_technical_factor(self, klines: List[Dict], signals: Dict = None) -> Dict:
        """
        计算技术因子
        
        子因子:
        - MACD 状态
        - KDJ 状态
        - 均线多空排列
        - 布林带位置
        """
        if len(klines) < 26:
            return {"score": 50, "details": {"error": "数据不足"}}
        
        closes = [k["close"] for k in klines]
        highs = [k["high"] for k in klines]
        lows = [k["low"] for k in klines]
        
        score = 50
        details = {}
        
        # 从信号中获取指标状态
        if signals and "indicators" in signals:
            ind = signals["indicators"]
            
            # MACD
            macd_signal = ind.get("macd", "")
            if macd_signal == "golden_cross":
                score += 15
                details["macd"] = "金叉 +15"
            elif macd_signal == "bullish":
                score += 8
                details["macd"] = "多头 +8"
            elif macd_signal == "death_cross":
                score -= 15
                details["macd"] = "死叉 -15"
            elif macd_signal == "bearish":
                score -= 8
                details["macd"] = "空头 -8"
            else:
                details["macd"] = "中性 0"
            
            # KDJ
            kdj_signal = ind.get("kdj", "")
            if kdj_signal == "golden_cross":
                score += 10
                details["kdj"] = "金叉 +10"
            elif kdj_signal == "oversold":
                score += 8
                details["kdj"] = "超卖 +8"
            elif kdj_signal == "death_cross":
                score -= 10
                details["kdj"] = "死叉 -10"
            elif kdj_signal == "overbought":
                score -= 8
                details["kdj"] = "超买 -8"
            else:
                details["kdj"] = "中性 0"
            
            # RSI
            rsi = ind.get("rsi", 50)
            if rsi < 30:
                score += 10
                details["rsi"] = f"超卖({rsi}) +10"
            elif rsi < 40:
                score += 5
                details["rsi"] = f"偏低({rsi}) +5"
            elif rsi > 70:
                score -= 10
                details["rsi"] = f"超买({rsi}) -10"
            elif rsi > 60:
                score -= 5
                details["rsi"] = f"偏高({rsi}) -5"
            else:
                details["rsi"] = f"中性({rsi}) 0"
            
            # 布林带
            boll_signal = ind.get("boll", "")
            if boll_signal == "touch_lower":
                score += 8
                details["boll"] = "触下轨 +8"
            elif boll_signal == "touch_upper":
                score -= 8
                details["boll"] = "触上轨 -8"
            else:
                details["boll"] = "中性 0"
        
        # 均线排列
        ma5 = np.mean(closes[-5:])
        ma10 = np.mean(closes[-10:])
        ma20 = np.mean(closes[-20:])
        current = closes[-1]
        
        ma_bullish = 0
        if current > ma5:
            ma_bullish += 1
        if ma5 > ma10:
            ma_bullish += 1
        if ma10 > ma20:
            ma_bullish += 1
        if current > ma20:
            ma_bullish += 1
        
        if ma_bullish >= 4:
            score += 10
            details["ma_align"] = "多头排列 +10"
        elif ma_bullish >= 3:
            score += 5
            details["ma_align"] = "偏多 +5"
        elif ma_bullish <= 1:
            score -= 10
            details["ma_align"] = "空头排列 -10"
        elif ma_bullish == 2:
            score -= 5
            details["ma_align"] = "偏空 -5"
        else:
            details["ma_align"] = "中性 0"
        
        score = max(0, min(100, score))
        
        return {
            "score": score,
            "details": details
        }
    
    def calculate_volume_factor(self, klines: List[Dict], realtime: Dict = None) -> Dict:
        """
        计算量价因子
        
        子因子:
        - 量比
        - 换手率
        - 量价配合
        """
        if len(klines) < 10:
            return {"score": 50, "details": {"error": "数据不足"}}
        
        closes = [k["close"] for k in klines]
        volumes = [k["volume"] for k in klines]
        turnovers = [k.get("turnover", 0) for k in klines]
        
        score = 50
        details = {}
        
        # 量比
        avg_vol_5d = np.mean(volumes[-6:-1]) if len(volumes) >= 6 else np.mean(volumes)
        current_vol = volumes[-1]
        vol_ratio = current_vol / avg_vol_5d if avg_vol_5d > 0 else 1
        
        # 今日涨跌
        price_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        
        # 量价配合评分
        if vol_ratio > 2:
            if price_change > 0:
                score += 15
                details["vol_price"] = f"放量上涨(量比{vol_ratio:.1f}) +15"
            else:
                score -= 10
                details["vol_price"] = f"放量下跌(量比{vol_ratio:.1f}) -10"
        elif vol_ratio > 1.5:
            if price_change > 0:
                score += 8
                details["vol_price"] = f"温和放量上涨 +8"
            else:
                score -= 5
                details["vol_price"] = f"温和放量下跌 -5"
        elif vol_ratio < 0.5:
            if price_change > 0:
                score += 5
                details["vol_price"] = f"缩量上涨(惜售) +5"
            else:
                score -= 3
                details["vol_price"] = f"缩量下跌 -3"
        else:
            details["vol_price"] = f"量价正常 0"
        
        # 换手率
        avg_turnover = np.mean([t for t in turnovers if t > 0]) if any(t > 0 for t in turnovers) else 0
        if avg_turnover > 10:
            score += 5  # 活跃
            details["turnover"] = f"高换手({avg_turnover:.1f}%) +5"
        elif avg_turnover < 1:
            score -= 5  # 不活跃
            details["turnover"] = f"低换手({avg_turnover:.1f}%) -5"
        else:
            details["turnover"] = f"正常({avg_turnover:.1f}%) 0"
        
        # 连续放量/缩量
        vol_trend = []
        for i in range(-5, 0):
            if i - 1 >= -len(volumes):
                vol_trend.append(volumes[i] > volumes[i-1])
        
        if len(vol_trend) >= 3:
            if all(vol_trend[-3:]):
                score += 8
                details["vol_trend"] = "连续放量 +8"
            elif not any(vol_trend[-3:]):
                score -= 5
                details["vol_trend"] = "连续缩量 -5"
            else:
                details["vol_trend"] = "量能波动 0"
        
        score = max(0, min(100, score))
        details["vol_ratio"] = round(vol_ratio, 2)
        
        return {
            "score": score,
            "details": details
        }
    
    def calculate_money_flow_factor(self, klines: List[Dict], extra_data: Dict = None) -> Dict:
        """
        计算资金因子
        
        子因子:
        - 主力净流入（通过大单推算）
        - 资金趋势
        """
        if len(klines) < 5:
            return {"score": 50, "details": {"error": "数据不足"}}
        
        score = 50
        details = {}
        
        # 简化版：通过量价推算资金流向
        # 上涨放量视为流入，下跌放量视为流出
        net_flow_score = 0
        
        for i in range(-5, 0):
            if i >= -len(klines) and i-1 >= -len(klines):
                price_change = klines[i]["close"] - klines[i-1]["close"]
                avg_vol = np.mean([k["volume"] for k in klines[-10:]])
                vol_ratio = klines[i]["volume"] / avg_vol if avg_vol > 0 else 1
                
                if price_change > 0 and vol_ratio > 1.2:
                    net_flow_score += 2  # 放量上涨 = 资金流入
                elif price_change < 0 and vol_ratio > 1.2:
                    net_flow_score -= 2  # 放量下跌 = 资金流出
                elif price_change > 0:
                    net_flow_score += 1
                elif price_change < 0:
                    net_flow_score -= 1
        
        if net_flow_score >= 5:
            score += 20
            details["flow"] = f"强势流入(+{net_flow_score}) +20"
        elif net_flow_score >= 3:
            score += 10
            details["flow"] = f"资金流入(+{net_flow_score}) +10"
        elif net_flow_score <= -5:
            score -= 20
            details["flow"] = f"强势流出({net_flow_score}) -20"
        elif net_flow_score <= -3:
            score -= 10
            details["flow"] = f"资金流出({net_flow_score}) -10"
        else:
            details["flow"] = f"资金平衡({net_flow_score}) 0"
        
        # 如果有额外数据（如北向资金）
        if extra_data:
            north_flow = extra_data.get("north_flow", 0)
            if north_flow > 50:  # 亿
                score += 10
                details["north"] = f"北向流入{north_flow}亿 +10"
            elif north_flow < -50:
                score -= 10
                details["north"] = f"北向流出{abs(north_flow)}亿 -10"
        
        score = max(0, min(100, score))
        
        return {
            "score": score,
            "details": details
        }
    
    def calculate_sentiment_factor(self, sentiment_data: Dict = None, market_data: Dict = None) -> Dict:
        """
        计算情绪因子
        
        子因子:
        - 市场整体情绪
        - 个股新闻情绪
        - 板块热度
        """
        score = 50
        details = {}
        
        if sentiment_data:
            # 整体市场情绪
            overall = sentiment_data.get("overall_sentiment", 0)
            if overall > 5:
                score += 15
                details["market"] = f"市场乐观(+{overall}) +15"
            elif overall > 2:
                score += 8
                details["market"] = f"市场偏多(+{overall}) +8"
            elif overall < -5:
                score -= 15
                details["market"] = f"市场悲观({overall}) -15"
            elif overall < -2:
                score -= 8
                details["market"] = f"市场偏空({overall}) -8"
            else:
                details["market"] = f"市场中性({overall}) 0"
        
        if market_data:
            # 大盘趋势
            index_change = market_data.get("sh000001", {}).get("change_pct", 0)
            if index_change > 2:
                score += 10
                details["index"] = f"大盘大涨 +10"
            elif index_change > 1:
                score += 5
                details["index"] = f"大盘上涨 +5"
            elif index_change < -2:
                score -= 10
                details["index"] = f"大盘大跌 -10"
            elif index_change < -1:
                score -= 5
                details["index"] = f"大盘下跌 -5"
        
        score = max(0, min(100, score))
        
        return {
            "score": score,
            "details": details
        }
    
    def calculate_composite_score(self,
                                  klines: List[Dict],
                                  realtime: Dict = None,
                                  signals: Dict = None,
                                  sentiment: Dict = None,
                                  market: Dict = None) -> Dict:
        """
        计算综合因子得分
        """
        factors = {}
        
        # 计算各因子得分
        factors["momentum"] = self.calculate_momentum_factor(klines)
        factors["technical"] = self.calculate_technical_factor(klines, signals)
        factors["volume"] = self.calculate_volume_factor(klines, realtime)
        factors["money_flow"] = self.calculate_money_flow_factor(klines)
        factors["sentiment"] = self.calculate_sentiment_factor(sentiment, market)
        
        # 加权计算总分
        total_score = 0
        for factor_name, weight in self.weights.items():
            factor_score = factors.get(factor_name, {}).get("score", 50)
            total_score += factor_score * weight
        
        # 生成交易建议
        if total_score >= 75:
            recommendation = "strong_buy"
            action_cn = "强烈买入"
        elif total_score >= 65:
            recommendation = "buy"
            action_cn = "买入"
        elif total_score >= 55:
            recommendation = "weak_buy"
            action_cn = "观望偏多"
        elif total_score <= 25:
            recommendation = "strong_sell"
            action_cn = "强烈卖出"
        elif total_score <= 35:
            recommendation = "sell"
            action_cn = "卖出"
        elif total_score <= 45:
            recommendation = "weak_sell"
            action_cn = "观望偏空"
        else:
            recommendation = "hold"
            action_cn = "持有观望"
        
        return {
            "total_score": round(total_score, 1),
            "recommendation": recommendation,
            "action_cn": action_cn,
            "factors": {
                name: {
                    "score": f["score"],
                    "weight": self.weights.get(name, 0),
                    "weighted_score": round(f["score"] * self.weights.get(name, 0), 1),
                    "details": f.get("details", {})
                }
                for name, f in factors.items()
            }
        }
    
    def rank_stocks(self, stocks_data: List[Dict]) -> List[Dict]:
        """
        对多只股票进行排名
        
        stocks_data: [{"code": "xxx", "name": "xxx", "klines": [...], ...}]
        """
        scored_stocks = []
        
        for stock in stocks_data:
            result = self.calculate_composite_score(
                klines=stock.get("klines", []),
                realtime=stock.get("realtime"),
                signals=stock.get("signals"),
                sentiment=stock.get("sentiment"),
                market=stock.get("market")
            )
            
            scored_stocks.append({
                "code": stock["code"],
                "name": stock.get("name", ""),
                "score": result["total_score"],
                "recommendation": result["recommendation"],
                "action_cn": result["action_cn"],
                "factors": result["factors"]
            })
        
        # 按得分排序
        scored_stocks.sort(key=lambda x: x["score"], reverse=True)
        
        return scored_stocks


class StockScreener:
    """股票筛选器"""
    
    def __init__(self):
        self.factor_model = FactorModel()
        
    def screen_by_criteria(self, 
                          stocks_data: List[Dict],
                          min_score: float = 60,
                          max_results: int = 20) -> List[Dict]:
        """按条件筛选股票"""
        
        ranked = self.factor_model.rank_stocks(stocks_data)
        
        # 过滤
        filtered = [s for s in ranked if s["score"] >= min_score]
        
        # 限制数量
        return filtered[:max_results]
    
    def screen_for_t0(self, stocks_data: List[Dict]) -> List[Dict]:
        """筛选适合 T+0 的股票"""
        
        suitable = []
        
        for stock in stocks_data:
            klines = stock.get("klines", [])
            if len(klines) < 20:
                continue
            
            # 计算波动率
            closes = [k["close"] for k in klines]
            daily_returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100 
                           for i in range(1, len(closes))]
            avg_volatility = np.std(daily_returns[-20:]) if len(daily_returns) >= 20 else 0
            
            # T+0 需要足够波动但不能太大
            if 1.5 <= avg_volatility <= 5.0:
                # 计算日内振幅
                amplitudes = [(k["high"] - k["low"]) / k["open"] * 100 for k in klines[-10:]]
                avg_amplitude = np.mean(amplitudes)
                
                if avg_amplitude >= 2.0:  # 平均振幅大于2%
                    suitable.append({
                        "code": stock["code"],
                        "name": stock.get("name", ""),
                        "volatility": round(avg_volatility, 2),
                        "amplitude": round(avg_amplitude, 2),
                        "t0_score": round(avg_amplitude * 10 + (5 - avg_volatility) * 5, 1)
                    })
        
        # 按 T+0 适合度排序
        suitable.sort(key=lambda x: x["t0_score"], reverse=True)
        
        return suitable


if __name__ == "__main__":
    # 测试代码
    model = FactorModel()
    
    # 模拟数据
    test_klines = []
    base_price = 10.0
    for i in range(60):
        change = np.random.uniform(-0.03, 0.04)
        base_price *= (1 + change)
        test_klines.append({
            "date": f"2026-01-{i+1:02d}",
            "open": base_price * 0.99,
            "high": base_price * 1.02,
            "low": base_price * 0.98,
            "close": base_price,
            "volume": int(np.random.uniform(1e6, 5e6)),
            "turnover": np.random.uniform(1, 5)
        })
    
    result = model.calculate_composite_score(test_klines)
    
    print("多因子模型测试")
    print("=" * 50)
    print(f"综合得分: {result['total_score']}")
    print(f"建议: {result['action_cn']}")
    print("\n各因子详情:")
    for name, factor in result["factors"].items():
        print(f"  {name}: {factor['score']} (权重{factor['weight']}) = {factor['weighted_score']}")
