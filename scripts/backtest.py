#!/usr/bin/env python3
"""
回测引擎 - 用历史数据验证交易策略
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_kline

BASE_DIR = Path(__file__).parent.parent
BACKTEST_DIR = BASE_DIR / "backtest_results"
BACKTEST_DIR.mkdir(exist_ok=True)


@dataclass
class Trade:
    """单笔交易"""
    date: str
    code: str
    name: str
    action: str  # buy/sell
    price: float
    quantity: int
    reason: str = ""
    pnl: float = 0
    pnl_pct: float = 0


@dataclass
class Position:
    """持仓"""
    code: str
    name: str
    quantity: int
    cost_price: float
    buy_date: str


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    annual_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    sharpe_ratio: float
    trades: List[Trade] = field(default_factory=list)
    daily_values: List[Dict] = field(default_factory=list)


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, initial_capital: float = 1000000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.daily_values: List[Dict] = []
        self.params_file = BASE_DIR / "strategy_params.json"
    
    def load_params(self) -> Dict:
        """加载策略参数"""
        if self.params_file.exists():
            with open(self.params_file, 'r') as f:
                return json.load(f)
        return {
            "stop_loss_pct": -0.08,
            "take_profit_pct": 0.05,
            "min_score": 65,
            "max_position_pct": 0.15,
        }
    
    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        """计算组合总值"""
        stock_value = sum(
            pos.quantity * prices.get(pos.code, pos.cost_price)
            for pos in self.positions.values()
        )
        return self.cash + stock_value
    
    def calculate_score(self, klines: List[Dict], idx: int) -> float:
        """
        计算买入信号评分 (0-100)
        基于技术指标
        """
        if idx < 20 or len(klines) <= idx:
            return 50
        
        score = 50
        
        # 当前及历史数据
        current = klines[idx]
        
        # 1. 均线趋势 (20分)
        ma5 = sum(k["close"] for k in klines[idx-4:idx+1]) / 5
        ma10 = sum(k["close"] for k in klines[idx-9:idx+1]) / 10
        ma20 = sum(k["close"] for k in klines[idx-19:idx+1]) / 20
        
        if current["close"] > ma5 > ma10 > ma20:
            score += 20  # 多头排列
        elif current["close"] > ma5 > ma10:
            score += 10
        elif current["close"] < ma5 < ma10 < ma20:
            score -= 15  # 空头排列
        elif current["close"] < ma5 < ma10:
            score -= 10
        
        # 2. 量价配合 (15分)
        vol_avg = sum(k["volume"] for k in klines[idx-4:idx]) / 5
        if current["volume"] > vol_avg * 1.5 and current["change_pct"] > 0:
            score += 15  # 放量上涨
        elif current["volume"] > vol_avg * 1.5 and current["change_pct"] < 0:
            score -= 10  # 放量下跌
        elif current["volume"] < vol_avg * 0.7:
            score -= 5  # 缩量
        
        # 3. 短期动量 (15分)
        momentum_5d = sum(k["change_pct"] for k in klines[idx-4:idx+1])
        if momentum_5d > 5:
            score += 10
        elif momentum_5d > 2:
            score += 5
        elif momentum_5d < -5:
            score -= 10
        elif momentum_5d < -2:
            score -= 5
        
        # 4. 突破信号 (10分)
        high_20d = max(k["high"] for k in klines[idx-19:idx])
        low_20d = min(k["low"] for k in klines[idx-19:idx])
        
        if current["close"] > high_20d:
            score += 10  # 突破20日新高
        elif current["close"] < low_20d:
            score -= 10  # 跌破20日新低
        
        return max(0, min(100, score))
    
    def should_buy(self, code: str, klines: List[Dict], idx: int, params: Dict) -> bool:
        """判断是否应该买入"""
        if code in self.positions:
            return False
        
        score = self.calculate_score(klines, idx)
        
        # 基本条件
        if score < params.get("min_score", 65):
            return False
        
        # 价格在5日线上
        if idx >= 5:
            ma5 = sum(k["close"] for k in klines[idx-4:idx+1]) / 5
            if klines[idx]["close"] < ma5:
                return False
        
        return True
    
    def should_sell(self, pos: Position, current_price: float, params: Dict) -> tuple:
        """判断是否应该卖出，返回 (是否卖出, 原因)"""
        pnl_pct = (current_price - pos.cost_price) / pos.cost_price
        
        # 止损
        stop_loss = params.get("stop_loss_pct", -0.08)
        if pnl_pct <= stop_loss:
            return True, f"止损 ({pnl_pct*100:.1f}%)"
        
        # 止盈
        take_profit = params.get("take_profit_pct", 0.05)
        if pnl_pct >= take_profit:
            return True, f"止盈 ({pnl_pct*100:.1f}%)"
        
        return False, ""
    
    def execute_buy(self, date: str, code: str, name: str, 
                    price: float, reason: str = "") -> Optional[Trade]:
        """执行买入"""
        params = self.load_params()
        max_position = params.get("max_position_pct", 0.15)
        
        # 计算买入金额
        portfolio_value = self.cash + sum(
            p.quantity * p.cost_price for p in self.positions.values()
        )
        buy_amount = min(self.cash * 0.95, portfolio_value * max_position)
        
        if buy_amount < 10000:
            return None
        
        quantity = int(buy_amount / price / 100) * 100  # 整手
        if quantity <= 0:
            return None
        
        cost = quantity * price
        if cost > self.cash:
            return None
        
        self.cash -= cost
        self.positions[code] = Position(
            code=code,
            name=name,
            quantity=quantity,
            cost_price=price,
            buy_date=date
        )
        
        trade = Trade(
            date=date,
            code=code,
            name=name,
            action="buy",
            price=price,
            quantity=quantity,
            reason=reason
        )
        self.trades.append(trade)
        return trade
    
    def execute_sell(self, date: str, pos: Position, 
                     price: float, reason: str = "") -> Trade:
        """执行卖出"""
        pnl = (price - pos.cost_price) * pos.quantity
        pnl_pct = (price - pos.cost_price) / pos.cost_price
        
        self.cash += pos.quantity * price
        del self.positions[pos.code]
        
        trade = Trade(
            date=date,
            code=pos.code,
            name=pos.name,
            action="sell",
            price=price,
            quantity=pos.quantity,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct
        )
        self.trades.append(trade)
        return trade
    
    def run_backtest(self, stocks: List[Dict], 
                     start_date: str, end_date: str,
                     strategy_name: str = "默认策略") -> BacktestResult:
        """
        运行回测
        stocks: [{"code": "601318", "name": "中国平安"}, ...]
        """
        print(f"开始回测: {strategy_name}")
        print(f"回测区间: {start_date} ~ {end_date}")
        print(f"初始资金: ¥{self.initial_capital:,.0f}")
        print(f"股票池: {len(stocks)} 只")
        print("-" * 50)
        
        params = self.load_params()
        
        # 获取所有股票的K线数据
        all_klines = {}
        for stock in stocks:
            code = stock["code"]
            print(f"获取 {stock['name']} ({code}) K线数据...")
            klines = fetch_kline(code, limit=500)
            if klines:
                all_klines[code] = {
                    "name": stock["name"],
                    "klines": klines
                }
        
        if not all_klines:
            print("无法获取K线数据")
            return None
        
        # 构建日期序列
        all_dates = set()
        for data in all_klines.values():
            for k in data["klines"]:
                if start_date <= k["date"] <= end_date:
                    all_dates.add(k["date"])
        
        dates = sorted(all_dates)
        print(f"回测交易日: {len(dates)} 天")
        
        # 逐日回测
        for date in dates:
            # 获取当日价格
            daily_prices = {}
            for code, data in all_klines.items():
                for i, k in enumerate(data["klines"]):
                    if k["date"] == date:
                        daily_prices[code] = {
                            "price": k["close"],
                            "klines": data["klines"],
                            "idx": i,
                            "name": data["name"]
                        }
                        break
            
            # 检查卖出信号
            for code in list(self.positions.keys()):
                if code in daily_prices:
                    pos = self.positions[code]
                    price = daily_prices[code]["price"]
                    should_sell, reason = self.should_sell(pos, price, params)
                    if should_sell:
                        trade = self.execute_sell(date, pos, price, reason)
                        print(f"[{date}] 卖出 {trade.name}: {trade.pnl:+.0f}元 ({trade.pnl_pct*100:+.1f}%) - {reason}")
            
            # 检查买入信号
            for code, data in daily_prices.items():
                if code not in self.positions:
                    if self.should_buy(code, data["klines"], data["idx"], params):
                        trade = self.execute_buy(
                            date, code, data["name"], 
                            data["price"], "评分达标"
                        )
                        if trade:
                            print(f"[{date}] 买入 {trade.name}: {trade.quantity}股 @ {trade.price:.2f}")
            
            # 记录每日净值
            portfolio_value = self.get_portfolio_value(
                {code: d["price"] for code, d in daily_prices.items()}
            )
            self.daily_values.append({
                "date": date,
                "value": portfolio_value,
                "cash": self.cash,
                "positions": len(self.positions)
            })
        
        # 计算回测结果
        return self.calculate_result(strategy_name, start_date, end_date)
    
    def calculate_result(self, strategy_name: str, 
                         start_date: str, end_date: str) -> BacktestResult:
        """计算回测结果统计"""
        
        final_capital = self.daily_values[-1]["value"] if self.daily_values else self.initial_capital
        total_return = (final_capital - self.initial_capital) / self.initial_capital
        
        # 年化收益
        days = len(self.daily_values)
        annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1
        
        # 最大回撤
        peak = self.initial_capital
        max_drawdown = 0
        for dv in self.daily_values:
            if dv["value"] > peak:
                peak = dv["value"]
            drawdown = (peak - dv["value"]) / peak
            max_drawdown = max(max_drawdown, drawdown)
        
        # 交易统计
        sell_trades = [t for t in self.trades if t.action == "sell"]
        winning = [t for t in sell_trades if t.pnl > 0]
        losing = [t for t in sell_trades if t.pnl < 0]
        
        win_rate = len(winning) / len(sell_trades) if sell_trades else 0
        avg_win = sum(t.pnl for t in winning) / len(winning) if winning else 0
        avg_loss = abs(sum(t.pnl for t in losing)) / len(losing) if losing else 0
        
        total_profit = sum(t.pnl for t in winning)
        total_loss = abs(sum(t.pnl for t in losing))
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        
        # 夏普比率 (简化版)
        if len(self.daily_values) > 1:
            returns = []
            for i in range(1, len(self.daily_values)):
                r = (self.daily_values[i]["value"] - self.daily_values[i-1]["value"]) / self.daily_values[i-1]["value"]
                returns.append(r)
            
            if returns:
                import statistics
                avg_return = statistics.mean(returns)
                std_return = statistics.stdev(returns) if len(returns) > 1 else 0.01
                sharpe_ratio = (avg_return * 252 - 0.03) / (std_return * (252 ** 0.5)) if std_return > 0 else 0
            else:
                sharpe_ratio = 0
        else:
            sharpe_ratio = 0
        
        return BacktestResult(
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=len(self.trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            avg_win=avg_win,
            avg_loss=avg_loss,
            sharpe_ratio=sharpe_ratio,
            trades=self.trades,
            daily_values=self.daily_values
        )
    
    def print_result(self, result: BacktestResult):
        """打印回测结果"""
        print("\n" + "=" * 50)
        print(f"📊 回测结果: {result.strategy_name}")
        print("=" * 50)
        print(f"回测区间: {result.start_date} ~ {result.end_date}")
        print(f"初始资金: ¥{result.initial_capital:,.0f}")
        print(f"最终资金: ¥{result.final_capital:,.0f}")
        print("-" * 50)
        
        emoji = "🟢" if result.total_return >= 0 else "🔴"
        print(f"{emoji} 总收益率: {result.total_return*100:+.2f}%")
        print(f"📈 年化收益: {result.annual_return*100:+.2f}%")
        print(f"📉 最大回撤: {result.max_drawdown*100:.2f}%")
        print(f"📊 夏普比率: {result.sharpe_ratio:.2f}")
        print("-" * 50)
        
        print(f"交易次数: {result.total_trades} ({result.total_trades//2} 轮)")
        print(f"胜率: {result.win_rate*100:.1f}% ({result.winning_trades}胜/{result.losing_trades}负)")
        print(f"盈亏比: {result.profit_factor:.2f}")
        print(f"平均盈利: ¥{result.avg_win:,.0f} / 平均亏损: ¥{result.avg_loss:,.0f}")
    
    def save_result(self, result: BacktestResult):
        """保存回测结果"""
        filename = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = BACKTEST_DIR / filename
        
        data = {
            "strategy_name": result.strategy_name,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
            "total_return": result.total_return,
            "annual_return": result.annual_return,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "trades": [
                {
                    "date": t.date,
                    "code": t.code,
                    "name": t.name,
                    "action": t.action,
                    "price": t.price,
                    "quantity": t.quantity,
                    "reason": t.reason,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct
                }
                for t in result.trades
            ],
            "daily_values": result.daily_values
        }
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"\n结果已保存: {filepath}")
        return filepath


def main():
    """运行回测示例"""
    
    # 股票池
    stocks = [
        {"code": "601318", "name": "中国平安"},
        {"code": "600036", "name": "招商银行"},
        {"code": "000333", "name": "美的集团"},
        {"code": "300896", "name": "爱美客"},
        {"code": "300144", "name": "宋城演艺"},
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000858", "name": "五粮液"},
        {"code": "002714", "name": "牧原股份"},
        {"code": "300750", "name": "宁德时代"},
        {"code": "600900", "name": "长江电力"},
    ]
    
    # 回测区间
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    
    # 创建回测引擎
    engine = BacktestEngine(initial_capital=1000000)
    
    # 运行回测
    result = engine.run_backtest(
        stocks=stocks,
        start_date=start_date,
        end_date=end_date,
        strategy_name="均线突破策略 v1"
    )
    
    if result:
        engine.print_result(result)
        engine.save_result(result)


if __name__ == "__main__":
    main()
