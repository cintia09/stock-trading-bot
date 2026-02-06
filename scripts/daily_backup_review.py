#!/usr/bin/env python3
"""
每日复盘数据备份 + 回测验证
每天收盘后运行，保存数据并验证策略表现
"""

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_kline, fetch_realtime_sina, fetch_market_overview
from deep_review_v2 import DeepReviewV2
from backtest import BacktestEngine

# 路径配置
WORKSPACE = Path(__file__).parent.parent
BACKUP_ROOT = Path("/root/backups/stock-trading")
BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

# 备份子目录
SNAPSHOTS_DIR = BACKUP_ROOT / "daily-snapshots"
KLINE_CACHE_DIR = BACKUP_ROOT / "kline-cache"
REVIEWS_DIR = BACKUP_ROOT / "reviews"

for d in [SNAPSHOTS_DIR, KLINE_CACHE_DIR, REVIEWS_DIR]:
    d.mkdir(exist_ok=True)


class DailyBackupAndReview:
    """每日备份和复盘"""
    
    def __init__(self):
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.account_file = WORKSPACE / "account.json"
        self.transactions_file = WORKSPACE / "transactions.json"
        self.params_file = WORKSPACE / "strategy_params.json"
        self.watchlist_file = WORKSPACE / "watchlist.json"
    
    def load_json(self, path: Path) -> dict:
        if path.exists():
            with open(path, 'r') as f:
                return json.load(f)
        return {}
    
    def save_json(self, path: Path, data: dict):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def backup_daily_snapshot(self) -> str:
        """备份当日快照"""
        snapshot = {
            "date": self.today,
            "timestamp": datetime.now().isoformat(),
            "account": self.load_json(self.account_file),
            "transactions": self.load_json(self.transactions_file),
            "strategy_params": self.load_json(self.params_file),
            "watchlist": self.load_json(self.watchlist_file),
        }
        
        # 获取市场数据
        snapshot["market"] = fetch_market_overview()
        
        # 获取持仓实时价格
        account = snapshot["account"]
        holdings = account.get("holdings", [])
        if holdings:
            codes = [h["code"] for h in holdings]
            snapshot["realtime_prices"] = fetch_realtime_sina(codes)
        
        # 计算当日盈亏
        total_value = account.get("current_cash", 0)
        for h in holdings:
            code = h["code"]
            price = snapshot.get("realtime_prices", {}).get(code, {}).get("price", h["cost_price"])
            total_value += price * h["quantity"]
        
        snapshot["total_value"] = total_value
        snapshot["daily_pnl"] = total_value - account.get("initial_capital", 1000000)
        snapshot["daily_pnl_pct"] = snapshot["daily_pnl"] / account.get("initial_capital", 1000000) * 100
        
        # 保存
        snapshot_file = SNAPSHOTS_DIR / f"snapshot_{self.today}.json"
        self.save_json(snapshot_file, snapshot)
        
        print(f"✅ 快照已保存: {snapshot_file}")
        return str(snapshot_file)
    
    def backup_kline_data(self) -> str:
        """备份持仓股票的K线数据"""
        account = self.load_json(self.account_file)
        holdings = account.get("holdings", [])
        
        kline_backup = {
            "date": self.today,
            "stocks": {}
        }
        
        for h in holdings:
            code = h["code"]
            name = h["name"]
            print(f"  获取 {name} ({code}) K线...")
            klines = fetch_kline(code, limit=120)
            if klines:
                kline_backup["stocks"][code] = {
                    "name": name,
                    "klines": klines
                }
        
        kline_file = KLINE_CACHE_DIR / f"klines_{self.today}.json"
        self.save_json(kline_file, kline_backup)
        
        print(f"✅ K线数据已备份: {kline_file}")
        return str(kline_file)
    
    def run_review_with_backtest(self) -> dict:
        """运行复盘并进行回测验证"""
        
        results = {
            "date": self.today,
            "review": None,
            "backtest": None,
            "comparison": None
        }
        
        # 1. 运行 5-Why 深度复盘
        print("\n📊 运行 5-Why 深度复盘...")
        reviewer = DeepReviewV2()
        review_report = reviewer.run_review()
        results["review"] = review_report
        
        # 保存复盘报告
        review_file = REVIEWS_DIR / f"review_{self.today}.md"
        with open(review_file, 'w') as f:
            f.write(review_report)
        print(f"✅ 复盘报告已保存: {review_file}")
        
        # 2. 运行回测验证当前策略
        print("\n📈 运行策略回测...")
        account = self.load_json(self.account_file)
        holdings = account.get("holdings", [])
        
        if holdings:
            stocks = [{"code": h["code"], "name": h["name"]} for h in holdings]
            
            # 回测最近60天
            end_date = self.today
            start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            
            engine = BacktestEngine(initial_capital=1000000)
            backtest_result = engine.run_backtest(
                stocks=stocks,
                start_date=start_date,
                end_date=end_date,
                strategy_name=f"当前策略_{self.today}"
            )
            
            if backtest_result:
                engine.print_result(backtest_result)
                backtest_file = engine.save_result(backtest_result)
                
                # 复制到备份目录
                shutil.copy(backtest_file, REVIEWS_DIR / f"backtest_{self.today}.json")
                
                results["backtest"] = {
                    "total_return": backtest_result.total_return,
                    "annual_return": backtest_result.annual_return,
                    "max_drawdown": backtest_result.max_drawdown,
                    "win_rate": backtest_result.win_rate,
                    "profit_factor": backtest_result.profit_factor,
                    "sharpe_ratio": backtest_result.sharpe_ratio
                }
        
        # 3. 与历史回测对比
        print("\n📊 与历史对比...")
        results["comparison"] = self.compare_with_history()
        
        return results
    
    def compare_with_history(self) -> dict:
        """与历史回测结果对比"""
        
        backtest_files = sorted(REVIEWS_DIR.glob("backtest_*.json"))
        
        if len(backtest_files) < 2:
            return {"message": "历史数据不足，无法对比"}
        
        history = []
        for f in backtest_files[-7:]:  # 最近7次
            with open(f, 'r') as fp:
                data = json.load(fp)
                history.append({
                    "date": f.stem.replace("backtest_", ""),
                    "return": data.get("total_return", 0),
                    "win_rate": data.get("win_rate", 0),
                    "max_drawdown": data.get("max_drawdown", 0)
                })
        
        # 计算趋势
        if len(history) >= 2:
            recent = history[-1]
            previous = history[-2]
            
            return {
                "recent": recent,
                "previous": previous,
                "return_trend": "improving" if recent["return"] > previous["return"] else "declining",
                "win_rate_trend": "improving" if recent["win_rate"] > previous["win_rate"] else "declining",
                "history_count": len(history)
            }
        
        return {"message": "需要更多数据"}
    
    def get_historical_stats(self) -> dict:
        """获取历史统计"""
        snapshots = sorted(SNAPSHOTS_DIR.glob("snapshot_*.json"))
        
        if not snapshots:
            return {"message": "无历史数据"}
        
        daily_pnls = []
        for f in snapshots:
            with open(f, 'r') as fp:
                data = json.load(fp)
                daily_pnls.append({
                    "date": data.get("date"),
                    "pnl": data.get("daily_pnl", 0),
                    "pnl_pct": data.get("daily_pnl_pct", 0)
                })
        
        if daily_pnls:
            import statistics
            pnl_values = [d["pnl"] for d in daily_pnls]
            return {
                "total_days": len(daily_pnls),
                "total_pnl": sum(pnl_values),
                "avg_daily_pnl": statistics.mean(pnl_values),
                "best_day": max(daily_pnls, key=lambda x: x["pnl"]),
                "worst_day": min(daily_pnls, key=lambda x: x["pnl"]),
                "winning_days": len([p for p in pnl_values if p > 0]),
                "losing_days": len([p for p in pnl_values if p < 0])
            }
        
        return {"message": "数据为空"}
    
    def run_full_daily_process(self) -> str:
        """运行完整的每日备份和复盘流程"""
        
        print("=" * 60)
        print(f"📅 每日复盘和备份 | {self.today}")
        print("=" * 60)
        
        # 1. 备份快照
        print("\n📦 备份当日快照...")
        self.backup_daily_snapshot()
        
        # 2. 备份K线
        print("\n📈 备份K线数据...")
        self.backup_kline_data()
        
        # 3. 运行复盘+回测
        results = self.run_review_with_backtest()
        
        # 4. 获取历史统计
        print("\n📊 历史统计...")
        historical = self.get_historical_stats()
        print(f"  累计交易日: {historical.get('total_days', 0)}")
        print(f"  累计盈亏: ¥{historical.get('total_pnl', 0):,.0f}")
        
        # 5. 生成汇总报告
        summary = self.generate_summary(results, historical)
        
        # 保存汇总
        summary_file = REVIEWS_DIR / f"summary_{self.today}.md"
        with open(summary_file, 'w') as f:
            f.write(summary)
        
        print(f"\n✅ 汇总报告: {summary_file}")
        print("\n" + summary)
        
        return summary
    
    def generate_summary(self, results: dict, historical: dict) -> str:
        """生成汇总报告"""
        
        lines = []
        lines.append(f"# 📊 每日复盘汇总 | {self.today}")
        lines.append("")
        
        # 回测结果
        if results.get("backtest"):
            bt = results["backtest"]
            lines.append("## 📈 策略回测验证")
            emoji = "🟢" if bt["total_return"] >= 0 else "🔴"
            lines.append(f"- {emoji} 收益率: {bt['total_return']*100:+.2f}%")
            lines.append(f"- 📉 最大回撤: {bt['max_drawdown']*100:.2f}%")
            lines.append(f"- 🎯 胜率: {bt['win_rate']*100:.1f}%")
            lines.append(f"- ⚖️ 盈亏比: {bt['profit_factor']:.2f}")
            lines.append(f"- 📊 夏普: {bt['sharpe_ratio']:.2f}")
            lines.append("")
        
        # 与历史对比
        if results.get("comparison") and results["comparison"].get("return_trend"):
            comp = results["comparison"]
            lines.append("## 📊 与上次对比")
            lines.append(f"- 收益趋势: {'📈 改善' if comp['return_trend'] == 'improving' else '📉 下降'}")
            lines.append(f"- 胜率趋势: {'📈 改善' if comp['win_rate_trend'] == 'improving' else '📉 下降'}")
            lines.append("")
        
        # 历史统计
        if historical.get("total_days"):
            lines.append("## 📅 累计统计")
            lines.append(f"- 交易日: {historical['total_days']} 天")
            lines.append(f"- 累计盈亏: ¥{historical['total_pnl']:+,.0f}")
            lines.append(f"- 日均盈亏: ¥{historical['avg_daily_pnl']:+,.0f}")
            lines.append(f"- 盈利天数: {historical['winning_days']} | 亏损天数: {historical['losing_days']}")
            lines.append("")
        
        return "\n".join(lines)


def main():
    processor = DailyBackupAndReview()
    summary = processor.run_full_daily_process()
    return summary


if __name__ == "__main__":
    main()
