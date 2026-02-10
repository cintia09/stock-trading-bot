#!/usr/bin/env python3
"""
主调度脚本 - 运行完整交易周期
用法: python3 main.py [cycle|discover|report]
"""

import sys
import json
from datetime import datetime
from pathlib import Path

# 添加脚本目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from trading_engine import run_trading_cycle, run_enhanced_trading_cycle, load_account
from stock_discovery import discover_stocks, update_watchlist_from_discovery
from news_sentiment import get_market_sentiment
from fetch_stock_data import fetch_market_overview, fetch_realtime_sina

BASE_DIR = Path(__file__).parent.parent

def generate_report() -> str:
    """生成交易报告"""
    account = load_account()
    
    # 获取持仓实时价格
    if account.get("holdings"):
        codes = [h["code"] for h in account["holdings"]]
        realtime = fetch_realtime_sina(codes)
        
        holdings_value = 0
        for h in account["holdings"]:
            rt = realtime.get(h["code"], {})
            price = rt.get("price", h["cost_price"])
            h["current_price"] = price
            h["market_value"] = round(price * h["quantity"], 2)
            h["pnl"] = round((price - h["cost_price"]) * h["quantity"], 2)
            h["pnl_pct"] = round((price - h["cost_price"]) / h["cost_price"] * 100, 2)
            holdings_value += h["market_value"]
        
        account["total_value"] = round(account["current_cash"] + holdings_value, 2)
        account["total_pnl"] = round(account["total_value"] - account["initial_capital"], 2)
        account["total_pnl_pct"] = round(account["total_pnl"] / account["initial_capital"] * 100, 2)
    
    # 获取大盘
    market = fetch_market_overview()
    
    # 构建报告
    report = []
    report.append(f"📊 **股票交易日报** | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report.append("")
    
    # 大盘
    report.append("**【大盘指数】**")
    for code in ["sh000001", "sz399001", "sz399006"]:
        if code in market:
            m = market[code]
            emoji = "🟢" if m.get("change_pct", 0) > 0 else "🔴"
            report.append(f"{emoji} {m['name']}: {m['price']} ({m.get('change_pct', 0):+.2f}%)")
    report.append("")
    
    # 账户
    report.append("**【账户状态】**")
    report.append(f"💰 总市值: ¥{account['total_value']:,.2f}")
    report.append(f"💵 现金: ¥{account['current_cash']:,.2f}")
    pnl_emoji = "📈" if account.get('total_pnl', 0) >= 0 else "📉"
    report.append(f"{pnl_emoji} 累计盈亏: ¥{account.get('total_pnl', 0):+,.2f} ({account.get('total_pnl_pct', 0):+.2f}%)")
    report.append("")
    
    # 持仓
    if account.get("holdings"):
        report.append("**【持仓明细】**")
        for h in account["holdings"]:
            emoji = "🟢" if h.get("pnl", 0) >= 0 else "🔴"
            report.append(f"{emoji} {h['name']}({h['code']})")
            report.append(f"   {h['quantity']}股 @ ¥{h.get('current_price', h['cost_price'])}")
            report.append(f"   成本¥{h['cost_price']} | 盈亏¥{h.get('pnl', 0):+,.0f}({h.get('pnl_pct', 0):+.1f}%)")
        report.append("")
    else:
        report.append("**【持仓】** 空仓")
        report.append("")
    
    # 今日交易
    tx_file = BASE_DIR / "transactions.json"
    if tx_file.exists():
        with open(tx_file, 'r') as f:
            transactions = json.load(f)
        
        today = datetime.now().strftime("%Y-%m-%d")
        today_tx = [t for t in transactions if t.get("timestamp", "").startswith(today)]
        
        if today_tx:
            report.append("**【今日交易】**")
            for t in today_tx:
                emoji = "📈" if t["type"] == "buy" else "📉"
                report.append(f"{emoji} {t['type'].upper()} {t['name']} {t['quantity']}股 @ ¥{t['price']}")
                if t.get("pnl"):
                    report.append(f"   盈亏: ¥{t['pnl']:+,.2f}")
            report.append("")
    
    return "\n".join(report)

def run_full_cycle():
    """运行完整交易周期"""
    print("=" * 60)
    print(f"🚀 开始完整交易周期 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 1. 发现新股票 (每天一次)
    discover_file = BASE_DIR / "data" / "discovered_stocks.json"
    need_discover = True
    if discover_file.exists():
        with open(discover_file, 'r') as f:
            discovered = json.load(f)
        last_discover = discovered.get("discovered_at", "")
        if last_discover.startswith(datetime.now().strftime("%Y-%m-%d")):
            need_discover = False
    
    if need_discover:
        print("\n📡 运行股票发现...")
        discover_stocks()
        update = update_watchlist_from_discovery()
        print(f"   新增关注: {update['added']}")
    
    # 2. 运行增强版交易周期 (包含 T+0 和多因子)
    result = run_enhanced_trading_cycle()
    
    # 3. 生成报告
    report = generate_report()
    
    # 保存报告
    report_file = BASE_DIR / "data" / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    with open(report_file, 'w') as f:
        f.write(report)
    
    print("\n" + report)
    
    return {
        "trades": len(result.get("trades", [])) if result else 0,
        "account": result.get("account", {}) if result else {},
        "report": report
    }

def main():
    if len(sys.argv) < 2:
        cmd = "cycle"
    else:
        cmd = sys.argv[1]
    
    if cmd == "cycle":
        run_full_cycle()
    elif cmd == "discover":
        discover_stocks()
        update_watchlist_from_discovery()
    elif cmd == "report":
        print(generate_report())
    elif cmd == "sentiment":
        sentiment = get_market_sentiment()
        print(f"市场情绪: {sentiment['overall_label']} ({sentiment['overall_sentiment']:+d})")
        for sector, count, score in sentiment['hot_sectors'][:5]:
            print(f"  {sector}: {count}次提及, 情绪{score:+.1f}")
    else:
        print(f"未知命令: {cmd}")
        print("用法: python3 main.py [cycle|discover|report|sentiment]")

if __name__ == "__main__":
    main()
