#!/usr/bin/env python3
"""
盘中实时监控 - 每30分钟采集一次盘面数据，累积保存，动态决策
"""

import sys
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_realtime_sina, fetch_market_overview, fetch_kline
from technical_analysis import generate_signals, calculate_volume_ratio
from trading_engine import (load_account, save_account, execute_trade, TRADING_RULES,
                            load_watchlist, save_watchlist, score_stock, get_holding_value,
                            get_current_cash, calculate_trade_cost,
                            get_today_stop_loss_codes, get_today_buy_count)

# 可转债扫描（盘中增量接入）
from cb_scanner import fetch_cb_list, scan
from bull_bear_debate import debate_stock, apply_debate_to_decision

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "intraday_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

FEISHU_CARD = Path("/root/.openclaw/workspace/scripts/feishu_card.py")


def _send_feishu_card(title: str, content_md: str, template: str = "blue", note: str = "小豆豆") -> bool:
    """Send Feishu card via subprocess (no import).

    Returns True on success, False otherwise.
    """
    if not FEISHU_CARD.exists():
        print(f"⚠️ Feishu card script not found: {FEISHU_CARD}")
        return False

    try:
        cp = subprocess.run(
            [
                sys.executable,
                str(FEISHU_CARD),
                "--title",
                title,
                "--content",
                content_md,
                "--template",
                template,
                "--note",
                note,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            print(f"⚠️ Feishu card send failed rc={cp.returncode}: {cp.stderr.strip() or cp.stdout.strip()}")
            return False
        print(f"✅ Feishu card sent: {title}")
        return True
    except Exception as e:
        print(f"⚠️ Feishu card send exception: {e}")
        return False


def _format_holdings_block(holdings: list[dict[str, Any]]) -> str:
    """Feishu card doesn't support tables; use code block alignment."""
    if not holdings:
        return "(空仓)"

    lines = []
    header = f"{'名称':<8} {'代码':<8} {'现价':>7} {'涨跌%':>7} {'成本盈亏%':>9}"
    lines.append(header)
    lines.append("-" * len(header))

    for h in holdings:
        name = str(h.get("name", ""))[:8]
        code = str(h.get("code", ""))[:8]
        price = h.get("price", 0)
        chg = h.get("change_pct", 0)
        pnl = h.get("pnl_from_cost_pct", 0)
        lines.append(f"{name:<8} {code:<8} {price:>7.2f} {chg:>+7.2f} {pnl:>+9.2f}")

    return "\n".join(lines)


def _format_trades_block(trades: list[dict[str, Any]]) -> str:
    if not trades:
        return "(无)"
    lines = []
    header = f"{'类型':<4} {'名称':<8} {'代码':<8} {'数量':>6} {'价格':>7} {'PnL':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for t in trades:
        ttype = str(t.get("type", ""))[:4]
        name = str(t.get("name", ""))[:8]
        code = str(t.get("code", ""))[:8]
        qty = int(t.get("quantity", 0) or 0)
        price = float(t.get("price", 0) or 0)
        pnl = t.get("pnl", "")
        pnl_txt = f"{float(pnl):+.0f}" if isinstance(pnl, (int, float)) else ""
        lines.append(f"{ttype:<4} {name:<8} {code:<8} {qty:>6d} {price:>7.2f} {pnl_txt:>8}")
    return "\n".join(lines)

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
    
    # 获取可转债实时数据并更新account
    cb_holdings = account.get("cb_holdings", [])
    if cb_holdings:
        cb_codes = [cb["bond_code"] for cb in cb_holdings]
        cb_realtime = fetch_realtime_sina(cb_codes)
        cb_total_value = 0
        for cb in cb_holdings:
            cb_rt = cb_realtime.get(cb["bond_code"], {})
            if cb_rt.get("price", 0) > 0:
                cb["current_price"] = cb_rt["price"]
                cb["market_value"] = round(cb_rt["price"] * cb["shares"], 2)
                cb["pnl_pct"] = round((cb_rt["price"] - cb["cost_price"]) / cb["cost_price"] * 100, 2)
            cb_total_value += cb.get("market_value", 0)
        save_account(account)
    else:
        cb_total_value = 0
    
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
        "cb_value": round(cb_total_value, 2),
        "total_value": round(account.get("current_cash", 0) + total_holdings_value + cb_total_value, 2),
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
    # Find a valid prev snapshot (holdings must be a list with dicts, not a dict-of-dicts)
    prev = None
    for i in range(len(snapshots) - 2, -1, -1):
        h = snapshots[i].get("holdings", [])
        if isinstance(h, list) and len(h) > 0 and isinstance(h[0], dict) and "code" in h[0]:
            prev = snapshots[i]
            break
    if prev is None:
        sh_now = latest["market"].get("sh000001", {}).get("change_pct", 0)
        return {"trend": "无可比数据", "signals": ["📡 无有效历史快照可对比"], "market_change": sh_now, "snapshot_count": len(snapshots)}
    first = snapshots[0]
    
    signals = []
    
    # 大盘趋势
    sh_now = latest["market"].get("sh000001", {}).get("change_pct", 0)
    sh_prev = prev.get("market", {}).get("sh000001", {}).get("change_pct", 0)
    sh_first = first.get("market", {}).get("sh000001", {}).get("change_pct", 0)
    
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
    
    # 读取策略参数止损线
    try:
        params_file = Path(__file__).parent.parent / "strategy_params.json"
        with open(params_file) as f:
            strategy_params = json.load(f)
        strategy_stop_loss_pct = strategy_params.get("stop_loss_pct", -0.042) * 100  # 转为百分比
    except:
        strategy_stop_loss_pct = -4.2
    
    # 读取复盘计划的个股止损线
    review_stop_prices = {}
    try:
        reviews_dir = Path(__file__).parent.parent / "reviews"
        if reviews_dir.exists():
            review_files = sorted(reviews_dir.glob("*.json"), reverse=True)
            for rf in review_files[:3]:  # 最近3个复盘文件
                with open(rf) as f:
                    review = json.load(f)
                for plan in review.get("tomorrow_plan", review.get("plans", [])):
                    if isinstance(plan, dict) and plan.get("stop_price"):
                        review_stop_prices[plan.get("code", "")] = plan["stop_price"]
    except:
        pass
    
    for h in snapshot["holdings"]:
        code = h["code"]
        name = h["name"]
        pnl = h["pnl_from_cost_pct"]
        price = h["price"]
        quantity = h["quantity"]
        
        # 计算盘中趋势（最近几个快照的价格变化方向）
        recent_prices = []
        for s in snapshots[-4:]:  # 最近4个快照（约2小时）
            holdings_data = s.get("holdings", [])
            # Handle dict-of-dicts format (code as keys)
            if isinstance(holdings_data, dict):
                if code in holdings_data:
                    recent_prices.append(holdings_data[code].get("price", 0))
                continue
            for sh in holdings_data:
                if isinstance(sh, dict) and sh.get("code") == code:
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
        
        # 0. 策略参数止损：亏损超过strategy_params设定值（默认-3%~-4.2%）
        if pnl <= strategy_stop_loss_pct:
            decisions.append({
                "code": code, "name": name, "action": "SELL_ALL",
                "trade_type": "sell", "price": price, "quantity": quantity,
                "reason": f"策略止损：浮亏{pnl:.1f}%超过止损线{strategy_stop_loss_pct:.1f}%",
                "urgency": "HIGH",
                "score": 5
            })
            continue
        
        # 0b. 复盘计划个股止损线
        if code in review_stop_prices and price <= review_stop_prices[code]:
            decisions.append({
                "code": code, "name": name, "action": "SELL_ALL",
                "trade_type": "sell", "price": price, "quantity": quantity,
                "reason": f"复盘止损：现价{price}跌破止损线{review_stop_prices[code]}",
                "urgency": "HIGH",
                "score": 5
            })
            continue
        
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
    
    # === P0: 日买入数量限制 ===
    max_daily_buys = TRADING_RULES.get("max_daily_buys", 2)
    today_buys = get_today_buy_count()
    if today_buys >= max_daily_buys:
        print(f"   ⛔ 日买入限制: 今日已买{today_buys}只(上限{max_daily_buys})，跳过watchlist扫描")
        return opportunities
    remaining_buys = max_daily_buys - today_buys
    
    # === P0: 获取今日止损代码 ===
    stop_loss_codes = get_today_stop_loss_codes()
    
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
        
        # === P0: 止损后同日禁买 ===
        if code in stop_loss_codes:
            print(f"   ⛔ 跳过{rt.get('name', code)}: 今日已止损，禁止买回")
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
                # 计算买入数量（P1: 新仓分批制 + 最小有效建仓阈值）
                first_buy_max = TRADING_RULES.get("first_buy_max_pct", 0.07)
                min_position_pct = TRADING_RULES.get("min_position_pct", 0.05)
                min_amount = total_value * min_position_pct
                max_buy_amount = min(
                    cash * 0.25,  # 单次最多用25%可用现金
                    total_value * first_buy_max  # 首笔上限7%（而非12%）
                )
                buy_qty = int(max_buy_amount / price // 100) * 100
                
                if buy_qty >= 100:
                    actual_amount = buy_qty * price
                    if actual_amount < min_amount:
                        print(f"   ⛔ 最小仓位过滤: {rt.get('name', code)} ¥{actual_amount:.0f}<{min_position_pct*100:.0f}%总资产(¥{min_amount:.0f})")
                        continue
                    
                    # === P1: Bull/Bear辩论 ===
                    try:
                        debate_info = {
                            "name": rt.get("name", c.get("name", code)),
                            "price": price,
                            "change_pct": round(change_pct, 2),
                            "pe": rt.get("pe", "未知"),
                            "pb": rt.get("pb", "未知"),
                            "industry": c.get("industry", "未知"),
                            "score": score,
                            "technical_signals": ", ".join(analysis_result.get("reasons", [])[:3]),
                            "news": c.get("catalyst", c.get("reason", "无")),
                        }
                        debate_result = debate_stock(code, debate_info)
                        adj_qty, debate_reason = apply_debate_to_decision(debate_result, buy_qty)
                        print(f"   🐂🐻 辩论: {debate_info['name']} 置信度={debate_result['confidence']} → {'买入' if adj_qty > 0 else '放弃'}")
                        if adj_qty == 0:
                            print(f"      ❌ {debate_reason}")
                            continue
                        if adj_qty < buy_qty:
                            print(f"      ⚠️ 减量: {buy_qty}→{adj_qty}股, {debate_reason}")
                        buy_qty = adj_qty
                    except Exception as e:
                        print(f"   ⚠️ 辩论异常(不影响买入): {e}")
                        debate_result = {"confidence": 50, "error": str(e)}
                    
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
                        "source": c.get("reason", "watchlist"),
                        "debate": debate_result,
                    })
    
    # 按分数排序，只取最好的（受日买入限制）
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities[:remaining_buys]


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
            if op.get('debate'):
                d = op['debate']
                print(f"      🐂🐻 置信度{d.get('confidence',50)} | 风险:{d.get('key_risk','?')} | 机会:{d.get('key_opportunity','?')}")
        decisions.extend(watchlist_ops)
    
    trades_made = []
    critical_signal = False
    if decisions:
        print(f"\n🎯 交易决策: {len(decisions)}个")
        account = load_account()
        for d in decisions:
            print(f"   {'🔴' if 'SELL' in d['action'] else '🟢'} {d['action']} {d['name']} {d['quantity']}股 @ ¥{d['price']}")
            print(f"      理由: {d['reason']}")

            # 关键告警（止损/强制卖出）
            reason_txt = str(d.get("reason", ""))
            if ("止损" in reason_txt) or (d.get("urgency") == "HIGH" and "SELL" in str(d.get("action", ""))):
                critical_signal = True

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

    # 5. 可转债套利扫描（不影响主流程，设超时防挂起）
    cb_over_50 = []
    cb_scan_ok = False
    try:
        import signal
        def _cb_timeout(signum, frame):
            raise TimeoutError("CB scan timed out after 90s")
        old_handler = signal.signal(signal.SIGALRM, _cb_timeout)
        signal.alarm(90)  # 90秒超时
        cb_list = fetch_cb_list()
        cb_opps = scan(cb_list) if cb_list else []
        signal.alarm(0)  # 取消超时
        signal.signal(signal.SIGALRM, old_handler)

        # 保存扫描结果（看板数据源依赖该文件）
        cb_output = DATA_DIR / "cb_opportunities.json"
        cb_output.parent.mkdir(parents=True, exist_ok=True)
        cb_result = {
            "scan_time": datetime.now().isoformat(),
            "total_listed": len(cb_list) if cb_list else 0,
            "opportunities_found": len(cb_opps),
            "opportunities": cb_opps[:30],
        }
        with open(cb_output, "w", encoding="utf-8") as f:
            json.dump(cb_result, f, ensure_ascii=False, indent=2)

        cb_scan_ok = True

        # 评分>50 的机会（给飞书/看板简要提示）
        cb_over_50 = [op for op in cb_opps if float(op.get('score', 0) or 0) > 50]
        if cb_over_50:
            top = cb_over_50[:3]
            brief = "；".join([
                f"{x.get('bond_name','')}({x.get('bond_code','')}) 评分{x.get('score')} 溢价{x.get('premium_rate')}%"
                for x in top
            ])
            analysis["signals"].append(f"💎 转债套利机会(>50分): {brief}")
        else:
            analysis["signals"].append("💎 转债套利机会: 暂无>50分")

        # 更新看板数据（update_data.py 内部会确保HTTP服务启动）
        dashboard_script = BASE_DIR.parent / "dashboard" / "update_data.py"
        subprocess.run([sys.executable, str(dashboard_script)], check=False)
    except Exception as e:
        print(f"⚠️ 可转债扫描失败(已忽略，不影响主监控): {e}")

    # 6. 当前持仓摘要
    print(f"\n{'─'*40}")
    print(f"💰 总资产: ¥{snapshot['total_value']:,.2f}")
    print(f"💵 现金: ¥{snapshot['cash']:,.2f}")
    print(f"📊 股票:")
    for h in snapshot["holdings"]:
        emoji = "🔴" if h["pnl_from_cost_pct"] >= 0 else "🟢"
        print(f"   {emoji} {h['name']} ¥{h['price']} ({h['change_pct']:+.1f}%) 成本盈亏{h['pnl_from_cost_pct']:+.1f}%")
    # 可转债明细
    _account = load_account()
    cb_holdings = _account.get("cb_holdings", [])
    if cb_holdings:
        print(f"📋 可转债:")
        for cb in cb_holdings:
            emoji = "🔴" if cb.get("pnl_pct", 0) >= 0 else "🟢"
            print(f"   {emoji} {cb['bond_name']} {cb['shares']}张 ¥{cb['current_price']} 市值¥{cb.get('market_value',0):,.2f} {cb.get('pnl_pct',0):+.1f}%")

    # 飞书推送（脚本自发，不依赖LLM）
    try:
        title = f"盘中监控 {now.strftime('%H:%M')} | {analysis['trend']} {analysis['market_change']:+.2f}%"

        sig_lines = "\n".join([f"- {s}" for s in (analysis.get("signals") or [])[:12]]) or "- (无特别信号)"

        watch_lines = ""
        if watchlist_ops:
            watch_lines = "\n".join([
                f"- {op.get('name','')}({op.get('code','')}) ¥{op.get('price',0):.2f} ({op.get('change_pct',0):+.1f}%) 评分{op.get('score',0)} 建议{op.get('quantity',0)}股"
                for op in watchlist_ops[:8]
            ])
        else:
            watch_lines = "- (无)"

        holdings_block = _format_holdings_block(snapshot.get("holdings", []))
        trades_block = _format_trades_block(trades_made)

        cb_brief = ""
        if cb_scan_ok:
            cb_brief = f"\n\n**可转债扫描**\n- >50分机会: {len(cb_over_50)}个"
            if cb_over_50:
                top = cb_over_50[:3]
                cb_brief += "\n" + "\n".join([
                    f"- {x.get('bond_name','')}({x.get('bond_code','')}) 评分{x.get('score')} 溢价{x.get('premium_rate')}%"
                    for x in top
                ])
        else:
            cb_brief = "\n\n**可转债扫描**\n- (失败/超时，已忽略)"

        # 可转债持仓明细
        cb_holdings_lines = ""
        cb_holdings_list = _account.get("cb_holdings", [])
        if cb_holdings_list:
            cb_rows = [f"{'名称':<8} {'张数':>4} {'现价':>7} {'市值':>9} {'盈亏%':>7}"]
            cb_rows.append("-" * 45)
            for cb in cb_holdings_list:
                cb_rows.append(f"{cb['bond_name']:<8} {cb['shares']:>4} {cb.get('current_price',0):>7.2f} {cb.get('market_value',0):>9,.0f} {cb.get('pnl_pct',0):>+7.1f}")
            cb_holdings_lines = f"\n\n**可转债持仓**\n```\n" + "\n".join(cb_rows) + "\n```"

        # 账户汇总
        total_val = snapshot["total_value"]
        cash = snapshot["cash"]
        stock_val = sum(h.get("market_value", 0) for h in snapshot.get("holdings", []))
        cb_val = snapshot.get("cb_value", 0)
        initial = _account.get("initial_capital", 1000000)
        total_pnl = total_val - initial
        pos_pct = (1 - cash / total_val) * 100 if total_val > 0 else 0
        summary_block = (
            f"\n\n**账户汇总**\n"
            f"- 💰 总资产: ¥{total_val:,.0f}\n"
            f"- 💵 可用资金: ¥{cash:,.0f}\n"
            f"- 📊 股票市值: ¥{stock_val:,.0f} | 可转债: ¥{cb_val:,.0f}\n"
            f"- 📊 仓位: {pos_pct:.1f}%\n"
            f"- 💰 总盈亏(含已实现): {total_pnl:+,.0f}元({total_pnl/initial*100:+.2f}%)"
        )

        content_md = (
            f"**大盘**\n"
            f"- 趋势: {analysis['trend']}\n"
            f"- 上证: {analysis['market_change']:+.2f}%\n"
            f"- 快照: 今日第{len(all_snapshots)}次\n\n"
            f"**信号**\n{sig_lines}\n\n"
            f"**持仓快照**\n```\n{holdings_block}\n```"
            f"{cb_holdings_lines}\n\n"
            f"**Watchlist扫描**\n{watch_lines}\n\n"
            f"**本次成交**\n```\n{trades_block}\n```"
            f"{cb_brief}"
            f"{summary_block}"
        )

        template = "red" if (critical_signal or (trades_made and any((t.get('type') == 'sell' and (t.get('pnl', 0) or 0) < 0) for t in trades_made))) else "blue"
        # 监控类默认蓝；出现止损/强风险则红
        _send_feishu_card(title=title, content_md=content_md, template=template, note="小豆豆")
    except Exception as e:
        print(f"⚠️ Feishu push build failed: {e}")

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
        "cb_scan_ok": cb_scan_ok,
        "cb_opportunities_over_50": len(cb_over_50),
        "cb_top_over_50": cb_over_50[:5],
    }


if __name__ == "__main__":
    result = run_monitor()
    print(f"\n结果: {json.dumps(result, ensure_ascii=False, default=str)}")
