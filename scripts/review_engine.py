#!/usr/bin/env python3
"""
交易复盘引擎 - 分析盈亏原因并改进策略
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
import statistics

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
REVIEW_DIR = BASE_DIR / "reviews"
REVIEW_DIR.mkdir(exist_ok=True)


@dataclass
class TradeReview:
    """单笔交易复盘"""
    code: str
    name: str
    action: str  # buy/sell
    price: float
    quantity: int
    timestamp: str
    pnl: float = 0  # 盈亏金额
    pnl_pct: float = 0  # 盈亏比例
    hold_days: int = 0  # 持有天数
    reason: str = ""  # 交易原因
    issue: str = ""  # 发现的问题
    lesson: str = ""  # 教训


@dataclass
class DailyReview:
    """每日复盘报告"""
    date: str
    total_pnl: float
    total_pnl_pct: float
    win_count: int
    lose_count: int
    win_rate: float
    avg_win: float
    avg_lose: float
    profit_factor: float  # 盈亏比
    max_drawdown: float
    trades: List[TradeReview]
    issues: List[str]
    improvements: List[str]
    strategy_updates: Dict


class ReviewEngine:
    """复盘引擎"""
    
    def __init__(self):
        self.account_file = BASE_DIR / "account.json"
        self.transactions_file = BASE_DIR / "transactions.json"
        self.strategy_file = BASE_DIR / "strategy.md"
        self.params_file = BASE_DIR / "strategy_params.json"
        
    def load_transactions(self) -> List[Dict]:
        """加载交易记录"""
        if self.transactions_file.exists():
            with open(self.transactions_file, 'r') as f:
                return json.load(f)
        return []
    
    def load_account(self) -> Dict:
        """加载账户"""
        if self.account_file.exists():
            with open(self.account_file, 'r') as f:
                return json.load(f)
        return {}
    
    def load_strategy_params(self) -> Dict:
        """加载策略参数"""
        if self.params_file.exists():
            with open(self.params_file, 'r') as f:
                return json.load(f)
        # 默认参数
        return {
            "stop_loss_pct": -0.08,
            "take_profit_pct": 0.05,
            "take_profit_full_pct": 0.10,
            "min_score": 65,
            "max_position_pct": 0.15,
            "volume_ratio_min": 1.2,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "version": 1,
            "last_updated": datetime.now().isoformat()
        }
    
    def save_strategy_params(self, params: Dict):
        """保存策略参数"""
        params["last_updated"] = datetime.now().isoformat()
        with open(self.params_file, 'w') as f:
            json.dump(params, f, indent=2, ensure_ascii=False)
    
    def get_today_transactions(self) -> List[Dict]:
        """获取今日交易"""
        today = datetime.now().strftime("%Y-%m-%d")
        transactions = self.load_transactions()
        return [t for t in transactions if t.get("timestamp", "").startswith(today)]
    
    def get_recent_transactions(self, days: int = 7) -> List[Dict]:
        """获取最近N天交易"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        transactions = self.load_transactions()
        return [t for t in transactions if t.get("timestamp", "") >= cutoff]
    
    def analyze_trade(self, trade: Dict, account: Dict) -> TradeReview:
        """分析单笔交易"""
        review = TradeReview(
            code=trade.get("code", ""),
            name=trade.get("name", ""),
            action=trade.get("action", ""),
            price=trade.get("price", 0),
            quantity=trade.get("quantity", 0),
            timestamp=trade.get("timestamp", ""),
            pnl=trade.get("pnl", 0),
            pnl_pct=trade.get("pnl_pct", 0),
            reason=trade.get("reason", "技术信号")
        )
        
        # 分析问题
        if review.action == "sell":
            if review.pnl < 0:
                if review.pnl_pct < -0.08:
                    review.issue = "超过止损线才卖出，止损执行不及时"
                    review.lesson = "需要更严格执行止损，考虑降低止损阈值"
                elif review.pnl_pct > -0.03:
                    review.issue = "小幅亏损即卖出，可能过于敏感"
                    review.lesson = "可能需要给予更多波动空间"
                else:
                    review.issue = "正常止损"
                    review.lesson = "止损执行正确"
            else:
                if review.pnl_pct < 0.03:
                    review.issue = "盈利较少即卖出，可能卖早了"
                    review.lesson = "可适当提高止盈阈值"
                elif review.pnl_pct > 0.10:
                    review.issue = "盈利丰厚，操作成功"
                    review.lesson = "继续保持这类交易模式"
                else:
                    review.issue = "正常止盈"
                    review.lesson = "操作得当"
        
        return review
    
    def analyze_daily(self, date: str = None) -> DailyReview:
        """分析某日交易表现"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        transactions = self.load_transactions()
        day_trades = [t for t in transactions if t.get("timestamp", "").startswith(date)]
        account = self.load_account()
        
        # 统计盈亏
        sell_trades = [t for t in day_trades if t.get("action") == "sell"]
        wins = [t for t in sell_trades if t.get("pnl", 0) > 0]
        loses = [t for t in sell_trades if t.get("pnl", 0) < 0]
        
        total_pnl = sum(t.get("pnl", 0) for t in sell_trades)
        win_amounts = [t.get("pnl", 0) for t in wins]
        lose_amounts = [abs(t.get("pnl", 0)) for t in loses]
        
        avg_win = statistics.mean(win_amounts) if win_amounts else 0
        avg_lose = statistics.mean(lose_amounts) if lose_amounts else 0
        
        # 盈亏比
        total_win = sum(win_amounts)
        total_lose = sum(lose_amounts)
        profit_factor = total_win / total_lose if total_lose > 0 else float('inf')
        
        # 分析每笔交易
        trade_reviews = [self.analyze_trade(t, account) for t in day_trades]
        
        # 识别问题
        issues = self._identify_issues(trade_reviews, account)
        
        # 生成改进建议
        improvements, strategy_updates = self._generate_improvements(
            trade_reviews, issues, account
        )
        
        return DailyReview(
            date=date,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl / account.get("initial_capital", 1000000) * 100,
            win_count=len(wins),
            lose_count=len(loses),
            win_rate=len(wins) / len(sell_trades) * 100 if sell_trades else 0,
            avg_win=avg_win,
            avg_lose=avg_lose,
            profit_factor=profit_factor,
            max_drawdown=account.get("max_drawdown", 0),
            trades=trade_reviews,
            issues=issues,
            improvements=improvements,
            strategy_updates=strategy_updates
        )
    
    def _identify_issues(self, trades: List[TradeReview], account: Dict) -> List[str]:
        """识别交易中的问题"""
        issues = []
        
        # 1. 检查亏损交易
        losing_trades = [t for t in trades if t.pnl < 0]
        if len(losing_trades) > 3:
            issues.append(f"今日亏损交易过多({len(losing_trades)}笔)，需检查选股逻辑")
        
        # 2. 检查止损执行
        big_losses = [t for t in losing_trades if t.pnl_pct < -0.08]
        if big_losses:
            issues.append(f"有{len(big_losses)}笔交易超过8%止损线，止损执行不及时")
        
        # 3. 检查早卖问题
        small_wins = [t for t in trades if 0 < t.pnl_pct < 0.02]
        if len(small_wins) > 2:
            issues.append(f"有{len(small_wins)}笔交易盈利不足2%就卖出，可能卖太早")
        
        # 4. 检查仓位问题
        holdings = account.get("holdings", [])
        total_value = account.get("total_value", 1000000)
        for h in holdings:
            position_pct = h.get("quantity", 0) * h.get("cost_price", 0) / total_value
            if position_pct > 0.20:
                issues.append(f"{h.get('name')}仓位过重({position_pct:.1%})，超过20%警戒线")
        
        # 5. 检查追涨行为
        buy_trades = [t for t in trades if t.action == "buy"]
        # TODO: 需要对比买入价和当日开盘价来判断是否追涨
        
        # 6. 检查总体亏损
        total_pnl = account.get("daily_pnl", 0)
        if total_pnl < -30000:
            issues.append(f"今日亏损{abs(total_pnl):.0f}元，超过3万警戒线")
        
        return issues
    
    def _generate_improvements(
        self, 
        trades: List[TradeReview], 
        issues: List[str],
        account: Dict
    ) -> Tuple[List[str], Dict]:
        """生成改进建议和策略更新"""
        improvements = []
        strategy_updates = {}
        params = self.load_strategy_params()
        
        # 根据问题生成建议
        for issue in issues:
            if "止损执行不及时" in issue:
                improvements.append("建议：收紧止损线到-6%，并设置硬性止损提醒")
                strategy_updates["stop_loss_pct"] = max(params["stop_loss_pct"], -0.06)
                
            elif "卖太早" in issue:
                improvements.append("建议：适当放宽止盈阈值，从5%调整到6%")
                strategy_updates["take_profit_pct"] = min(params["take_profit_pct"] + 0.01, 0.08)
                
            elif "仓位过重" in issue:
                improvements.append("建议：单只股票仓位控制在15%以内，分散持仓")
                strategy_updates["max_position_pct"] = 0.15
                
            elif "亏损交易过多" in issue:
                improvements.append("建议：提高选股评分阈值，只买入评分>70的股票")
                strategy_updates["min_score"] = max(params.get("min_score", 65), 70)
                
            elif "超过3万" in issue:
                improvements.append("建议：触发单日亏损上限，明日降低仓位操作")
                strategy_updates["daily_max_loss_triggered"] = True
        
        # 统计分析改进
        losing_trades = [t for t in trades if t.pnl < 0 and t.action == "sell"]
        winning_trades = [t for t in trades if t.pnl > 0 and t.action == "sell"]
        sell_trades = [t for t in trades if t.action == "sell"]
        
        if losing_trades:
            avg_loss_pct = statistics.mean([t.pnl_pct for t in losing_trades])
            if avg_loss_pct < -0.05:
                improvements.append(f"平均亏损{avg_loss_pct:.1%}，考虑更早止损")
        
        if winning_trades:
            avg_win_pct = statistics.mean([t.pnl_pct for t in winning_trades])
            if avg_win_pct < 0.03:
                improvements.append(f"平均盈利仅{avg_win_pct:.1%}，考虑延长持有时间")
        
        # 胜率分析
        if sell_trades:
            win_rate = len(winning_trades) / len(sell_trades)
            if win_rate < 0.4:
                improvements.append(f"胜率仅{win_rate:.1%}，需要优化选股策略")
                strategy_updates["min_score"] = max(params.get("min_score", 65), 70)
            elif win_rate > 0.7:
                improvements.append(f"胜率高达{win_rate:.1%}，可适当加大仓位")
        
        return improvements, strategy_updates
    
    def apply_strategy_updates(self, updates: Dict) -> str:
        """应用策略更新"""
        if not updates:
            return "无需更新策略参数"
        
        params = self.load_strategy_params()
        changes = []
        
        for key, value in updates.items():
            if key in params and params[key] != value:
                old_value = params[key]
                params[key] = value
                changes.append(f"{key}: {old_value} → {value}")
        
        if changes:
            params["version"] = params.get("version", 0) + 1
            self.save_strategy_params(params)
            return "策略参数已更新:\n" + "\n".join(changes)
        
        return "策略参数无变化"
    
    def generate_review_report(self, review: DailyReview) -> str:
        """生成复盘报告"""
        report = []
        report.append(f"# 📊 交易复盘报告 | {review.date}")
        report.append("")
        
        # 盈亏概况
        emoji = "🟢" if review.total_pnl >= 0 else "🔴"
        report.append("## 📈 盈亏概况")
        report.append(f"- {emoji} 今日盈亏: ¥{review.total_pnl:+,.0f} ({review.total_pnl_pct:+.2f}%)")
        report.append(f"- 🎯 胜率: {review.win_rate:.1f}% ({review.win_count}胜/{review.lose_count}负)")
        report.append(f"- 📊 平均盈利: ¥{review.avg_win:,.0f} / 平均亏损: ¥{review.avg_lose:,.0f}")
        report.append(f"- ⚖️ 盈亏比: {review.profit_factor:.2f}")
        report.append("")
        
        # 交易明细
        if review.trades:
            report.append("## 📝 交易明细")
            for t in review.trades:
                if t.action == "sell":
                    emoji = "🟢" if t.pnl >= 0 else "🔴"
                    report.append(f"- {emoji} {t.name}: {t.pnl:+,.0f}元 ({t.pnl_pct:+.1%})")
                    if t.issue:
                        report.append(f"  - 问题: {t.issue}")
            report.append("")
        
        # 问题分析
        if review.issues:
            report.append("## ⚠️ 发现问题")
            for issue in review.issues:
                report.append(f"- {issue}")
            report.append("")
        
        # 改进建议
        if review.improvements:
            report.append("## 💡 改进建议")
            for imp in review.improvements:
                report.append(f"- {imp}")
            report.append("")
        
        # 策略调整
        if review.strategy_updates:
            report.append("## 🔧 策略调整")
            for key, value in review.strategy_updates.items():
                report.append(f"- {key}: {value}")
            report.append("")
        
        return "\n".join(report)
    
    def save_review(self, review: DailyReview):
        """保存复盘记录"""
        review_file = REVIEW_DIR / f"{review.date}.json"
        
        # 转换为可序列化格式
        data = {
            "date": review.date,
            "total_pnl": review.total_pnl,
            "total_pnl_pct": review.total_pnl_pct,
            "win_count": review.win_count,
            "lose_count": review.lose_count,
            "win_rate": review.win_rate,
            "avg_win": review.avg_win,
            "avg_lose": review.avg_lose,
            "profit_factor": review.profit_factor,
            "max_drawdown": review.max_drawdown,
            "trades": [asdict(t) for t in review.trades],
            "issues": review.issues,
            "improvements": review.improvements,
            "strategy_updates": review.strategy_updates
        }
        
        with open(review_file, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def run_daily_review(self, date: str = None) -> str:
        """运行每日复盘"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        # 分析
        review = self.analyze_daily(date)
        
        # 保存复盘记录
        self.save_review(review)
        
        # 应用策略更新
        update_result = self.apply_strategy_updates(review.strategy_updates)
        
        # 生成报告
        report = self.generate_review_report(review)
        report += f"\n---\n{update_result}"
        
        # 保存报告到daily-log
        log_dir = BASE_DIR / "daily-log"
        log_file = log_dir / f"{date}.md"
        if log_file.exists():
            with open(log_file, 'a') as f:
                f.write(f"\n\n{report}")
        
        return report
    
    def get_weekly_summary(self) -> str:
        """获取周度复盘总结"""
        reviews = []
        for i in range(7):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            review_file = REVIEW_DIR / f"{date}.json"
            if review_file.exists():
                with open(review_file, 'r') as f:
                    reviews.append(json.load(f))
        
        if not reviews:
            return "本周暂无复盘数据"
        
        total_pnl = sum(r["total_pnl"] for r in reviews)
        total_wins = sum(r["win_count"] for r in reviews)
        total_loses = sum(r["lose_count"] for r in reviews)
        
        report = ["# 📊 周度复盘总结", ""]
        report.append(f"- 累计盈亏: ¥{total_pnl:+,.0f}")
        report.append(f"- 总胜负: {total_wins}胜/{total_loses}负")
        if total_wins + total_loses > 0:
            report.append(f"- 周胜率: {total_wins/(total_wins+total_loses)*100:.1f}%")
        
        # 汇总所有问题
        all_issues = []
        for r in reviews:
            all_issues.extend(r.get("issues", []))
        
        if all_issues:
            from collections import Counter
            issue_counts = Counter(all_issues)
            report.append("\n## 本周常见问题")
            for issue, count in issue_counts.most_common(5):
                report.append(f"- {issue} (出现{count}次)")
        
        return "\n".join(report)


def main():
    """命令行入口"""
    import sys
    
    engine = ReviewEngine()
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "daily":
            date = sys.argv[2] if len(sys.argv) > 2 else None
            print(engine.run_daily_review(date))
        elif cmd == "weekly":
            print(engine.get_weekly_summary())
        else:
            print(f"Unknown command: {cmd}")
    else:
        # 默认运行今日复盘
        print(engine.run_daily_review())


if __name__ == "__main__":
    main()
