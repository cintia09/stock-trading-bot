#!/usr/bin/env python3
"""
盘中实时监控 - 每30分钟采集一次盘面数据，累积保存，动态决策
"""

import sys
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_realtime_sina, fetch_market_overview, fetch_kline
from technical_analysis import generate_signals, calculate_volume_ratio
from trading_engine import (load_account, save_account, execute_trade, TRADING_RULES,
                            load_watchlist, save_watchlist, score_stock, get_holding_value,
                            get_available_cash, calculate_trade_cost)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "intraday_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

def collect_snapshot():
    """采集当前盘面快照并追加到今日文件"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    ts = now.strftime("%H:%M:%S")
    
    account = load_account()
    
    # 获取大盘指数
    market = fetch_market_overview()
    market_data = {}
    for code in ["sh000001", "sz399001", "sz399006"]:
        if code in market:
            m = market[code]
            market_data[code] = {
                "name": m["name"],
                "price": m["price"],
                "change_pct": m.get("change_pct", 0),
                "volume": m.get("volume", 0),
                "amount": m.get("amount", 0),
            }
    
    # 获取持仓实时数据
    holdings_codes = [h["code"] for h in account.get("holdings", [])]
    realtime = fetch_realtime_sina(holdings_codes) if holdings_codes else {}
    
    holdings_snapshot = []
    total_holdings_value = 0
    for h in account.get("holdings", []):
        rt = realtime.get(h["code"], {})
        price = rt.get("price", h.get("current_price", h["cost_price"]))
        volume = rt.get("volume", 0)
        amount = rt.get("amount", 0)
        high = rt.get("high", price)
        low = rt.get("low", price)
        open_price = rt.get("open", price)
        prev_close = rt.get("prev_close", h["cost_price"])
        change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
        pnl_from_cost = round((price - h["cost_price"]) / h["cost_price"] * 100, 2)
        mv = round(price * h["quantity"], 2)
        total_holdings_value += mv
        
        holdings_snapshot.append({
            "code": h["code"],
            "name": h["name"],
            "price": price,
            "open": open_price,
            "high": high,
            "low": low,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": volume,
            "amount": amount,
            "quantity": h["quantity"],
            "cost_price": h["cost_price"],
            "pnl_from_cost_pct": pnl_from_cost,
            "market_value": mv,
        })
    
    snapshot = {
        "timestamp": now.isoformat(),
        "time": ts,
        "market": market_data,
        "holdings": holdings_snapshot,
        "cash": account.get("current_cash", 0),
        "total_value": round(account.get("current_cash", 0) + total_holdings_value, 2),
    }
    
    # 追加到今日快照文件
    snapshot_file = SNAPSHOT_DIR / f"{today}.json"
    snapshots = []
    if snapshot_file.exists():
        with open(snapshot_file, 'r') as f:
            snapshots = json.load(f)
    snapshots.append(snapshot)
    with open(snapshot_file, 'w') as f:
        json.dump(snapshots, f, ensure_ascii=False, indent=2)
    
    return snapshot, snapshots


def analyze_trend(snapshots):
    """分析盘中趋势变化（基于累积快照）"""
    if len(snapshots) < 2:
        sh_now = snapshots[-1]["market"].get("sh000001", {}).get("change_pct", 0) if snapshots else 0
        return {"trend": "首次采集", "signals": ["📡 首次采集数据，下次开始对比"], "market_change": sh_now, "snapshot_count": len(snapshots)}
    
    latest = snapshots[-1]
    prev = snapshots[-2]
    first = snapshots[0]
    
    signals = []
    
    # 大盘趋势
    sh_now = latest["market"].get("sh000001", {}).get("change_pct", 0)
    sh_prev = prev["market"].get("sh000001", {}).get("change_pct", 0)
    sh_first = first["market"].get("sh000001", {}).get("change_pct", 0)
    
    if sh_now > sh_prev + 0.3:
        signals.append("📈 大盘加速上涨")
    elif sh_now < sh_prev - 0.3:
        signals.append("📉 大盘回落")
    
    if sh_now > 1.5:
        signals.append("🔥 大盘强势（>1.5%）")
    elif sh_now < -1.5:
        signals.append("❄️ 大盘弱势（<-1.5%）")
    
    # 个股趋势
    for h_now in latest["holdings"]:
        code = h_now["code"]
        name = h_now["name"]
        
        # 找前一次数据
        h_prev = None
        for hp in prev["holdings"]:
            if hp["code"] == code:
                h_prev = hp
                break
        
        if not h_prev:
            continue
        
        price_now = h_now["price"]
        price_prev = h_prev["price"]
        pnl = h_now["pnl_from_cost_pct"]
        
        # 价格变化
        delta = round((price_now - price_prev) / price_prev * 100, 2) if price_prev else 0
        
        if delta > 1:
            signals.append(f"🚀 {name} 半小时涨{delta:.1f}%")
        elif delta < -1:
            signals.append(f"⬇️ {name} 半小时跌{abs(delta):.1f}%")
        
        # 从成本看
        if pnl >= 5:
            signals.append(f"💰 {name} 浮盈{pnl:.1f}%，考虑减仓锁利")
        elif pnl >= 3:
            signals.append(f"✅ {name} 浮盈{pnl:.1f}%，关注能否突破")
        elif pnl <= -5:
            signals.append(f"⚠️ {name} 浮亏{abs(pnl):.1f}%，接近止损线")
        elif pnl <= -8:
            signals.append(f"🔴 {name} 浮亏{abs(pnl):.1f}%，建议止损！")
        
        # 量价配合：高位放量可能见顶，低位放量可能反转
        vol_now = h_now.get("volume", 0)
        vol_prev = h_prev.get("volume", 0)
        if vol_prev > 0 and vol_now > vol_prev * 1.5:
            if pnl > 3:
                signals.append(f"📊 {name} 放量上涨，注意可能冲高回落")
            elif pnl < -3:
                signals.append(f"📊 {name} 低位放量，可能有资金进场")
    
    # 整体仓位建议
    cash_ratio = latest["cash"] / latest["total_value"] * 100
    if sh_now > 2 and cash_ratio > 20:
        signals.append(f"💡 大盘强势+现金{cash_ratio:.0f}%，可考虑加仓")
    elif sh_now < -2 and cash_ratio < 30:
        signals.append(f"💡 大盘弱势+仓位重，可考虑减仓避险")
    
    return {
        "trend": "上涨" if sh_now > 0.5 else ("下跌" if sh_now < -0.5 else "震荡"),
        "market_change": sh_now,
        "signals": signals,
        "snapshot_count": len(snapshots),
    }


def make_dynamic_decisions(snapshot, analysis, snapshots):
    """基于盘面动态变化做交易决策（不死守预设条件）"""
    decisions = []
    account = load_account()
    
    for h in snapshot["holdings"]:
        code = h["code"]
        name = h["name"]
        pnl = h["pnl_from_cost_pct"]
        price = h["price"]
        quantity = h["quantity"]
        
        # 计算盘中趋势（最近几个快照的价格变化方向）
        recent_prices = []
        for s in snapshots[-4:]:  # 最近4个快照（约2小时）
            for sh in s["holdings"]:
                if sh["code"] == code:
                    recent_prices.append(sh["price"])
                    break
        
        # 判断趋势方向
        if len(recent_prices) >= 3:
            trend_up = all(recent_prices[i] <= recent_prices[i+1] for i in range(len(recent_prices)-1))
            trend_down = all(recent_prices[i] >= recent_prices[i+1] for i in range(len(recent_prices)-1))
        else:
            trend_up = trend_down = False
        
        market_strong = analysis["market_change"] > 1
        market_weak = analysis["market_change"] < -1
        
        # === 动态卖出决策 ===
        
        # 1. 硬止损：亏损超8%必须止损
        if pnl <= -8:
            decisions.append({
                "code": code, "name": name, "action": "SELL_ALL",
                "trade_type": "sell", "price": price, "quantity": quantity,
                "reason": f"硬止损：浮亏{pnl:.1f}%超过-8%",
                "urgency": "HIGH",
                "score": 10
            })
            continue
        
        # 2. 趋势恶化+亏损：连续下跌且亏损超3%，主动减仓
        if trend_down and pnl <= -3 and not market_strong:
            sell_qty = (quantity // 100) * 100 // 2  # 减半仓
            if sell_qty >= 100:
                decisions.append({
                    "code": code, "name": name, "action": "SELL_HALF",
                    "trade_type": "sell", "price": price, "quantity": sell_qty,
                    "reason": f"趋势恶化：连续下跌+浮亏{pnl:.1f}%，主动减仓",
                    "urgency": "MEDIUM",
                    "score": 30
                })
                continue
        
        # 3. 大盘暴跌防御：大盘跌超2%且个股也在跌，减仓防御
        if market_weak and h["change_pct"] < -1 and pnl < 0:
            sell_qty = (quantity // 100) * 100 // 3  # 减1/3仓
            if sell_qty >= 100:
                decisions.append({
                    "code": code, "name": name, "action": "SELL_PARTIAL",
                    "trade_type": "sell", "price": price, "quantity": sell_qty,
                    "reason": f"大盘暴跌防御：大盘{analysis['market_change']:+.1f}%，减仓避险",
                    "urgency": "MEDIUM",
                    "score": 35
                })
                continue
        
        # 4. 盈利减仓：浮盈超5%且出现滞涨或回落信号
        if pnl >= 5:
            if not trend_up or h["change_pct"] < 0:
                sell_qty = (quantity // 100) * 100 // 3
                if sell_qty >= 100:
                    decisions.append({
                        "code": code, "name": name, "action": "TAKE_PROFIT",
                        "trade_type": "sell", "price": price, "quantity": sell_qty,
                        "reason": f"止盈减仓：浮盈{pnl:.1f}%且涨势减弱",
                        "urgency": "LOW",
                        "score": 55
                    })
        
        # 5. 大盈利全出：浮盈超10%
        if pnl >= 10:
            decisions.append({
                "code": code, "name": name, "action": "SELL_ALL",
                "trade_type": "sell", "price": price, "quantity": quantity,
                "reason": f"大幅盈利止盈：浮盈{pnl:.1f}%",
                "urgency": "MEDIUM",
                "score": 20
            })
    
    # === 动态买入决策 ===
    cash = account.get("current_cash", 0)
    total_value = snapshot["total_value"]
    cash_ratio = cash / total_value * 100 if total_value > 0 else 100
    
    # 大盘强势 + 有现金 + 持仓中有趋势向好的股票 → 考虑加仓
    if market_strong and cash_ratio > 15 and cash > 20000:
        for h in snapshot["holdings"]:
            if h["pnl_from_cost_pct"] > 0 and h["change_pct"] > 0.5:
                # 持仓占比
                position_pct = h["market_value"] / total_value * 100
                if position_pct < 18:  # 不超仓位上限
                    buy_amount = min(cash * 0.2, 50000)  # 最多用20%现金或5万
                    buy_qty = int(buy_amount / h["price"] // 100) * 100
                    if buy_qty >= 100:
                        decisions.append({
                            "code": h["code"], "name": h["name"], "action": "BUY_ADD",
                            "trade_type": "buy", "price": h["price"], "quantity": buy_qty,
                            "reason": f"大盘强势+{h['name']}趋势向好({h['change_pct']:+.1f}%)，加仓",
                            "urgency": "LOW",
                            "score": 65
                        })
                        break  # 一次只加仓一只
    
    return decisions


def scan_watchlist_opportunities(snapshot, analysis):
    """扫描watchlist中的买入机会"""
    opportunities = []
    account = load_account()
    watchlist = load_watchlist()
    
    cash = account.get("current_cash", 0)
    total_value = snapshot["total_value"]
    
    # 计算当前仓位比例
    holdings_value = sum(h["market_value"] for h in snapshot["holdings"])
    current_position_pct = holdings_value / total_value if total_value > 0 else 0
    
    # 如果仓位已满或现金不足，跳过
    max_pos = TRADING_RULES.get("max_total_position", 0.5)
    if current_position_pct >= max_pos or cash < TRADING_RULES.get("min_buy_amount", 5000):
        return opportunities
    
    # 获取持仓代码（排除已持仓）
    holding_codes = {h["code"] for h in account.get("holdings", [])}
    
    # 筛选watchlist中的候选
    candidates = [s for s in watchlist.get("stocks", []) if s["code"] not in holding_codes]
    if not candidates:
        return opportunities
    
    # 获取实时数据（最多取10只，避免太慢）
    candidate_codes = [c["code"] for c in candidates[:10]]
    realtime = fetch_realtime_sina(candidate_codes)
    
    market_strong = analysis["market_change"] > 0.3
    market_neutral = analysis["market_change"] > -0.5
    
    for c in candidates[:10]:
        code = c["code"]
        rt = realtime.get(code, {})
        if not rt or rt.get("price", 0) == 0:
            continue
        
        price = rt["price"]
        pre_close = rt.get("pre_close", rt.get("prev_close", price))
        change_pct = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0
        
        # 获取K线做技术分析
        try:
            klines = fetch_kline(code, period="101", limit=30)
            if len(klines) < 10:
                continue
            signals = generate_signals(klines)
            analysis_result = score_stock(code, rt, klines, None)
        except Exception:
            continue
        
        score = analysis_result.get("score", 0)
        action = analysis_result.get("action", "hold")
        
        # 买入条件：
        # 1. 评分>=65（强信号）
        # 2. 大盘至少中性（不在暴跌中买入）
        # 3. 今日涨幅合理（-1% ~ +5%，不追涨停）
        if score >= 65 and action in ["buy", "strong_buy"] and market_neutral:
            if -1 < change_pct < 5:
                # 计算买入数量
                max_buy_amount = min(
                    cash * 0.25,  # 单次最多用25%可用现金
                    total_value * TRADING_RULES.get("max_position_pct", 0.15)  # 单只最大仓位
                )
                buy_qty = int(max_buy_amount / price // 100) * 100
                
                if buy_qty >= 100:
                    opportunities.append({
                        "code": code,
                        "name": rt.get("name", c.get("name", code)),
                        "price": price,
                        "change_pct": change_pct,
                        "score": score,
                        "action": "BUY_NEW",
                        "trade_type": "buy",
                        "quantity": buy_qty,
                        "amount": round(buy_qty * price, 2),
                        "reason": f"watchlist高分股({score}分): {', '.join(analysis_result.get('reasons', [])[:2])}",
                        "urgency": "MEDIUM" if score >= 70 else "LOW",
                        "source": c.get("reason", "watchlist")
                    })
    
    # 按分数排序，只取最好的2只（避免一次买太多）
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities[:2]


def run_monitor():
    """主入口：采集+分析+决策"""
    now = datetime.now()
    
    # 检查是否在交易时段
    hour, minute = now.hour, now.minute
    t = hour * 60 + minute
    morning_open = 9 * 60 + 25   # 9:25
    morning_close = 11 * 60 + 35  # 11:35
    afternoon_open = 12 * 60 + 55  # 12:55
    afternoon_close = 15 * 60 + 5  # 15:05
    
    in_session = (morning_open <= t <= morning_close) or (afternoon_open <= t <= afternoon_close)
    
    if not in_session:
        print(f"[{now.strftime('%H:%M')}] 非交易时段，跳过")
        return {"status": "skipped", "reason": "非交易时段"}
    
    print(f"\n{'='*50}")
    print(f"📡 盘中监控 | {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    
    # 1. 采集快照
    snapshot, all_snapshots = collect_snapshot()
    print(f"✅ 快照已保存（今日第{len(all_snapshots)}个）")
    
    # 2. 趋势分析
    analysis = analyze_trend(all_snapshots)
    print(f"\n📊 大盘趋势: {analysis['trend']}（{analysis['market_change']:+.2f}%）")
    if analysis["signals"]:
        print("📌 信号:")
        for sig in analysis["signals"]:
            print(f"   {sig}")
    else:
        print("   无特别信号")
    
    # 3. 动态决策（持仓管理）
    decisions = make_dynamic_decisions(snapshot, analysis, all_snapshots)
    
    # 4. 扫描watchlist买入机会
    watchlist_ops = scan_watchlist_opportunities(snapshot, analysis)
    if watchlist_ops:
        print(f"\n🌟 Watchlist买入机会: {len(watchlist_ops)}个")
        for op in watchlist_ops:
            print(f"   🟢 {op['name']}({op['code']}) ¥{op['price']} ({op['change_pct']:+.1f}%) 评分{op['score']}")
            print(f"      建议: 买入{op['quantity']}股 ≈ ¥{op['amount']:,.0f}")
            print(f"      理由: {op['reason']}")
        decisions.extend(watchlist_ops)
    
    trades_made = []
    if decisions:
        print(f"\n🎯 交易决策: {len(decisions)}个")
        account = load_account()
        for d in decisions:
            print(f"   {'🔴' if 'SELL' in d['action'] else '🟢'} {d['action']} {d['name']} {d['quantity']}股 @ ¥{d['price']}")
            print(f"      理由: {d['reason']}")
            
            # 执行交易
            result = execute_trade(account, d)
            if result["success"]:
                trade = result["trade"]
                print(f"      ✅ 已执行: {trade['type']} {trade['quantity']}股")
                trades_made.append(trade)
                account = load_account()  # 重新加载更新后的账户
            else:
                print(f"      ❌ 未执行: {result['reason']}")
    else:
        print("\n💤 无交易信号，继续持有观望")
    
    # 4. 当前持仓摘要
    print(f"\n{'─'*40}")
    print(f"💰 总资产: ¥{snapshot['total_value']:,.2f}")
    print(f"💵 现金: ¥{snapshot['cash']:,.2f}")
    for h in snapshot["holdings"]:
        emoji = "🔴" if h["pnl_from_cost_pct"] >= 0 else "🟢"
        print(f"   {emoji} {h['name']} ¥{h['price']} ({h['change_pct']:+.1f}%) 成本盈亏{h['pnl_from_cost_pct']:+.1f}%")
    
    # 返回结构化结果（供cron任务使用）
    return {
        "status": "ok",
        "timestamp": now.isoformat(),
        "trend": analysis["trend"],
        "market_change": analysis["market_change"],
        "signals": analysis["signals"],
        "decisions": len(decisions),
        "watchlist_opportunities": len(watchlist_ops) if watchlist_ops else 0,
        "trades": trades_made,
        "total_value": snapshot["total_value"],
        "snapshot_count": len(all_snapshots),
    }


if __name__ == "__main__":
    result = run_monitor()
    print(f"\n结果: {json.dumps(result, ensure_ascii=False, default=str)}")
