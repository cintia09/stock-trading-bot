#!/usr/bin/env python3
"""
T+0 日内交易策略模块
利用底仓进行日内差价交易

策略核心：
1. 高抛低吸：日内波动超过阈值时交易
2. 网格交易：预设价格网格自动买卖
3. 趋势跟踪：跟随日内趋势顺势操作
"""

from datetime import datetime, time as dt_time
from typing import Dict, List, Tuple, Optional
import numpy as np


class T0Strategy:
    """T+0 日内交易策略"""
    
    def __init__(self, config: Dict = None):
        self.config = config or self._default_config()
        
        # 日内交易状态
        self.intraday_trades = {}  # {code: [{'action': 'sell', 'price': x, 'qty': n, 'time': t}]}
        self.grid_orders = {}  # 网格挂单
        
    def _default_config(self) -> Dict:
        return {
            # 波动阈值
            "min_swing_pct": 2.0,       # 最小波动幅度触发交易
            "take_profit_pct": 3.0,     # 日内止盈
            "stop_loss_pct": -2.0,      # 日内止损
            
            # 网格参数
            "grid_enabled": True,
            "grid_step_pct": 1.5,       # 网格间距
            "grid_levels": 3,           # 网格层数
            
            # 时间窗口
            "no_trade_start": "09:30",  # 开盘观察期
            "no_trade_end": "09:45",
            "must_close_by": "14:50",   # 必须平仓时间
            
            # 仓位控制
            "max_t0_ratio": 0.5,        # T+0 最大使用仓位比例
            "min_trade_amount": 5000,   # 最小交易金额
        }
    
    def is_trading_time(self) -> Tuple[bool, str]:
        """检查当前是否为交易时间"""
        now = datetime.now().time()
        
        # A股交易时间
        morning_start = dt_time(9, 30)
        morning_end = dt_time(11, 30)
        afternoon_start = dt_time(13, 0)
        afternoon_end = dt_time(15, 0)
        
        if morning_start <= now <= morning_end:
            return True, "morning"
        elif afternoon_start <= now <= afternoon_end:
            return True, "afternoon"
        elif dt_time(9, 15) <= now < morning_start:
            return False, "pre_market"
        else:
            return False, "closed"
    
    def is_no_trade_window(self) -> bool:
        """检查是否在禁止交易窗口（开盘前15分钟观察）"""
        now = datetime.now().time()
        no_start = dt_time(*map(int, self.config["no_trade_start"].split(":")))
        no_end = dt_time(*map(int, self.config["no_trade_end"].split(":")))
        return no_start <= now <= no_end
    
    def should_force_close(self) -> bool:
        """检查是否需要强制平仓"""
        now = datetime.now().time()
        must_close = dt_time(*map(int, self.config["must_close_by"].split(":")))
        return now >= must_close
    
    def analyze_intraday_pattern(self, minutes_data: List[Dict]) -> Dict:
        """
        分析日内走势模式
        
        minutes_data: 分钟K线数据 [{'time': '09:31', 'open': x, 'high': x, 'low': x, 'close': x, 'volume': x}]
        """
        if len(minutes_data) < 5:
            return {"pattern": "unknown", "confidence": 0}
        
        closes = [m["close"] for m in minutes_data]
        volumes = [m["volume"] for m in minutes_data]
        
        # 计算关键价位
        open_price = minutes_data[0]["open"]
        current_price = closes[-1]
        high_price = max(m["high"] for m in minutes_data)
        low_price = min(m["low"] for m in minutes_data)
        
        # 计算波动率
        price_range = (high_price - low_price) / open_price * 100
        change_pct = (current_price - open_price) / open_price * 100
        
        # 量价分析
        first_half_vol = sum(volumes[:len(volumes)//2])
        second_half_vol = sum(volumes[len(volumes)//2:])
        vol_trend = "increasing" if second_half_vol > first_half_vol * 1.2 else \
                   "decreasing" if second_half_vol < first_half_vol * 0.8 else "stable"
        
        # 识别走势模式
        pattern = "unknown"
        confidence = 0.5
        signal = None
        
        if price_range < 1.0:
            pattern = "consolidation"  # 横盘整理
            confidence = 0.7
            
        elif change_pct > 2.0 and current_price > open_price * 1.02:
            if current_price >= high_price * 0.98:
                pattern = "strong_uptrend"  # 强势上涨
                confidence = 0.8
                signal = "hold_or_add"
            else:
                pattern = "pullback_from_high"  # 冲高回落
                confidence = 0.75
                signal = "sell"
                
        elif change_pct < -2.0 and current_price < open_price * 0.98:
            if current_price <= low_price * 1.02:
                pattern = "strong_downtrend"  # 强势下跌
                confidence = 0.8
                signal = "hold"  # 等待企稳
            else:
                pattern = "rebound_from_low"  # 探底回升
                confidence = 0.75
                signal = "buy"
                
        elif abs(change_pct) < 1.0:
            if high_price > open_price * 1.02 and low_price < open_price * 0.98:
                pattern = "wide_swing"  # 宽幅震荡
                confidence = 0.7
                signal = "grid_trade"
            else:
                pattern = "narrow_range"  # 窄幅震荡
                confidence = 0.6
        
        return {
            "pattern": pattern,
            "confidence": confidence,
            "signal": signal,
            "price_range": round(price_range, 2),
            "change_pct": round(change_pct, 2),
            "vol_trend": vol_trend,
            "key_levels": {
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "current": current_price
            }
        }
    
    def generate_t0_signal(self, 
                           code: str,
                           current_price: float,
                           pre_close: float,
                           open_price: float,
                           high_price: float,
                           low_price: float,
                           available_sell_qty: int,
                           cost_price: float = None,
                           already_sold_today: int = 0,
                           sold_avg_price: float = 0) -> Optional[Dict]:
        """
        生成 T+0 交易信号
        
        Args:
            code: 股票代码
            current_price: 当前价
            pre_close: 昨收价
            open_price: 今开价
            high_price: 今日最高价
            low_price: 今日最低价
            available_sell_qty: 可卖数量（昨日持仓）
            cost_price: 持仓成本价
            already_sold_today: 今日已卖出数量
            sold_avg_price: 今日卖出均价
        
        Returns:
            交易信号 或 None
        """
        # 检查交易时间
        is_trading, session = self.is_trading_time()
        if not is_trading:
            return None
        
        # 开盘观察期不交易
        if self.is_no_trade_window():
            return None
        
        # 计算涨跌幅
        change_from_open = (current_price - open_price) / open_price * 100
        change_from_close = (current_price - pre_close) / pre_close * 100
        change_from_high = (current_price - high_price) / high_price * 100
        change_from_low = (current_price - low_price) / low_price * 100
        
        # 已卖出，寻找买回机会
        if already_sold_today > 0 and sold_avg_price > 0:
            return self._find_buyback_signal(
                code, current_price, sold_avg_price, 
                low_price, pre_close, already_sold_today
            )
        
        # 未卖出，寻找卖出机会
        if available_sell_qty > 0:
            return self._find_sell_signal(
                code, current_price, pre_close, open_price,
                high_price, low_price, available_sell_qty, cost_price
            )
        
        return None
    
    def _find_sell_signal(self, 
                          code: str,
                          current_price: float,
                          pre_close: float,
                          open_price: float,
                          high_price: float,
                          low_price: float,
                          available_qty: int,
                          cost_price: float = None) -> Optional[Dict]:
        """寻找 T+0 卖出信号"""
        
        change_from_close = (current_price - pre_close) / pre_close * 100
        change_from_high = (current_price - high_price) / high_price * 100 if high_price > 0 else 0
        
        signal = None
        reason = ""
        confidence = 0.5
        
        # 信号1: 冲高回落
        if high_price / pre_close > 1.03 and change_from_high < -1.5:
            signal = "sell"
            reason = f"冲高回落: 最高涨{(high_price/pre_close-1)*100:.1f}%, 已回落{change_from_high:.1f}%"
            confidence = 0.75
            
        # 信号2: 高开低走
        elif open_price / pre_close > 1.02 and current_price < open_price * 0.99:
            signal = "sell"
            reason = f"高开低走: 开盘涨{(open_price/pre_close-1)*100:.1f}%, 现跌破开盘价"
            confidence = 0.7
            
        # 信号3: 涨幅达到止盈
        elif change_from_close >= self.config["take_profit_pct"]:
            signal = "sell"
            reason = f"达到日内止盈: 涨{change_from_close:.1f}%"
            confidence = 0.8
            
        # 信号4: 成本止盈
        elif cost_price and current_price / cost_price > 1.05:
            signal = "sell"
            reason = f"成本止盈: 盈利{(current_price/cost_price-1)*100:.1f}%"
            confidence = 0.7
            
        # 信号5: 强制平仓时间
        if self.should_force_close() and change_from_close > 0:
            signal = "sell"
            reason = f"临近收盘强制止盈: 涨{change_from_close:.1f}%"
            confidence = 0.9
        
        if signal:
            # 计算卖出数量（T+0只卖一半底仓）
            sell_qty = min(
                available_qty,
                int(available_qty * self.config["max_t0_ratio"])
            )
            # 确保100股整数
            sell_qty = (sell_qty // 100) * 100
            
            if sell_qty * current_price >= self.config["min_trade_amount"]:
                return {
                    "action": "t0_sell",
                    "code": code,
                    "quantity": sell_qty,
                    "price": current_price,
                    "reason": reason,
                    "confidence": confidence,
                    "expected_buyback": current_price * 0.97  # 预期买回价格
                }
        
        return None
    
    def _find_buyback_signal(self,
                             code: str,
                             current_price: float,
                             sold_price: float,
                             low_price: float,
                             pre_close: float,
                             sold_qty: int) -> Optional[Dict]:
        """寻找 T+0 买回信号"""
        
        change_from_sold = (current_price - sold_price) / sold_price * 100
        change_from_low = (current_price - low_price) / low_price * 100 if low_price > 0 else 0
        
        signal = None
        reason = ""
        confidence = 0.5
        
        # 信号1: 回落达到目标
        if change_from_sold < -2.0:
            signal = "buy"
            reason = f"回落买入: 较卖出价跌{abs(change_from_sold):.1f}%"
            confidence = 0.75
            
        # 信号2: 探底回升
        elif low_price / pre_close < 0.97 and change_from_low > 1.0:
            signal = "buy"
            reason = f"探底回升: 最低跌{(low_price/pre_close-1)*100:.1f}%, 已反弹{change_from_low:.1f}%"
            confidence = 0.7
            
        # 信号3: 接近收盘必须买回
        if self.should_force_close():
            signal = "buy"
            reason = f"临近收盘买回: 差价{change_from_sold:.1f}%"
            confidence = 0.95  # 必须执行
        
        if signal:
            return {
                "action": "t0_buy",
                "code": code,
                "quantity": sold_qty,
                "price": current_price,
                "reason": reason,
                "confidence": confidence,
                "profit_pct": round(-change_from_sold, 2)  # T+0 盈利
            }
        
        return None
    
    def generate_grid_orders(self,
                            code: str,
                            current_price: float,
                            available_qty: int,
                            cash_available: float) -> List[Dict]:
        """
        生成网格交易订单
        
        在当前价格上下布置网格，自动挂单
        """
        if not self.config["grid_enabled"]:
            return []
        
        orders = []
        step_pct = self.config["grid_step_pct"] / 100
        levels = self.config["grid_levels"]
        
        # 计算每格交易量
        grid_qty = available_qty // (levels * 2)
        grid_qty = (grid_qty // 100) * 100
        
        if grid_qty < 100:
            return []
        
        for i in range(1, levels + 1):
            # 卖出网格（上方）
            sell_price = round(current_price * (1 + step_pct * i), 2)
            orders.append({
                "action": "grid_sell",
                "code": code,
                "price": sell_price,
                "quantity": grid_qty,
                "level": i,
                "side": "sell"
            })
            
            # 买入网格（下方）
            buy_price = round(current_price * (1 - step_pct * i), 2)
            buy_amount = buy_price * grid_qty
            if buy_amount <= cash_available / levels:
                orders.append({
                    "action": "grid_buy",
                    "code": code,
                    "price": buy_price,
                    "quantity": grid_qty,
                    "level": i,
                    "side": "buy"
                })
        
        return orders
    
    def calculate_t0_profit(self, trades: List[Dict]) -> Dict:
        """计算 T+0 交易盈亏"""
        if not trades:
            return {"profit": 0, "profit_pct": 0, "trades": 0}
        
        total_sell = sum(t["price"] * t["quantity"] for t in trades if t["action"] == "t0_sell")
        total_buy = sum(t["price"] * t["quantity"] for t in trades if t["action"] == "t0_buy")
        
        profit = total_sell - total_buy
        avg_price = (total_sell + total_buy) / 2 if total_sell + total_buy > 0 else 0
        profit_pct = profit / avg_price * 100 if avg_price > 0 else 0
        
        return {
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "trades": len(trades),
            "sell_amount": round(total_sell, 2),
            "buy_amount": round(total_buy, 2)
        }


class IntradayMomentum:
    """日内动量策略"""
    
    def __init__(self, lookback_minutes: int = 15):
        self.lookback = lookback_minutes
        
    def calculate_momentum(self, prices: List[float]) -> float:
        """计算动量"""
        if len(prices) < self.lookback:
            return 0
        
        recent = prices[-self.lookback:]
        momentum = (recent[-1] - recent[0]) / recent[0] * 100
        return round(momentum, 3)
    
    def detect_breakout(self, 
                       current_price: float,
                       prices: List[float],
                       volumes: List[float]) -> Optional[str]:
        """检测突破"""
        if len(prices) < 20:
            return None
        
        # 计算近期高低点
        recent_high = max(prices[-20:-1])
        recent_low = min(prices[-20:-1])
        recent_avg_vol = np.mean(volumes[-20:-1])
        current_vol = volumes[-1]
        
        # 放量突破
        if current_price > recent_high and current_vol > recent_avg_vol * 1.5:
            return "breakout_up"
        elif current_price < recent_low and current_vol > recent_avg_vol * 1.5:
            return "breakout_down"
        
        return None


class VWAPStrategy:
    """VWAP 策略 - 成交量加权平均价"""
    
    def calculate_vwap(self, minutes_data: List[Dict]) -> float:
        """计算 VWAP"""
        if not minutes_data:
            return 0
        
        total_value = sum(
            (m["high"] + m["low"] + m["close"]) / 3 * m["volume"] 
            for m in minutes_data
        )
        total_volume = sum(m["volume"] for m in minutes_data)
        
        return round(total_value / total_volume, 3) if total_volume > 0 else 0
    
    def generate_signal(self, current_price: float, vwap: float) -> Optional[str]:
        """基于 VWAP 生成信号"""
        if vwap <= 0:
            return None
        
        deviation = (current_price - vwap) / vwap * 100
        
        if deviation > 2:
            return "above_vwap_sell"  # 高于 VWAP 2%，考虑卖出
        elif deviation < -2:
            return "below_vwap_buy"  # 低于 VWAP 2%，考虑买入
        
        return None


if __name__ == "__main__":
    # 测试代码
    strategy = T0Strategy()
    
    print("T+0 策略测试")
    print("=" * 50)
    
    # 模拟信号
    signal = strategy.generate_t0_signal(
        code="601318",
        current_price=67.5,
        pre_close=66.0,
        open_price=66.5,
        high_price=68.0,
        low_price=66.2,
        available_sell_qty=2000,
        cost_price=65.0
    )
    
    if signal:
        print(f"信号: {signal['action']}")
        print(f"原因: {signal['reason']}")
        print(f"数量: {signal['quantity']}")
        print(f"置信度: {signal['confidence']}")
    else:
        print("无信号")
