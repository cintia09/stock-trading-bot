#!/usr/bin/env python3
"""
交易决策引擎 - 综合分析并生成交易决策
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple

from fetch_stock_data import (
    fetch_realtime_sina, fetch_kline, fetch_market_overview,
    fetch_hot_stocks, save_data, load_data
)
from technical_analysis import generate_signals, calculate_volume_ratio, analyze_trend
from news_sentiment import get_market_sentiment
from t0_strategy import T0Strategy, IntradayMomentum, VWAPStrategy
from factor_model import FactorModel, StockScreener

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# 交易规则配置
TRADING_RULES = {
    "min_buy_amount": 5000,       # 最小买入金额
    "max_position_pct": 0.15,     # 单只最大仓位15%
    "max_total_position": 0.70,   # 最大总仓位70%
    "stop_loss_pct": -0.08,       # 止损-8%
    "take_profit_pct": 0.05,      # 止盈+5%减仓
    "take_profit_full_pct": 0.10, # 止盈+10%全出
    "commission_rate": 0.00025,   # 佣金万2.5
    "min_commission": 5,          # 最低佣金5元
    "stamp_tax": 0.001,           # 印花税千1(卖出)
    "transfer_fee": 0.00002,      # 过户费万0.2
}

def load_account() -> Dict:
    """加载账户信息"""
    account_file = BASE_DIR / "account.json"
    if account_file.exists():
        with open(account_file, 'r') as f:
            return json.load(f)
    return {
        "initial_capital": 1000000,
        "current_cash": 1000000,
        "total_value": 1000000,
        "holdings": [],
        "frozen_sells": [],
        "daily_pnl": 0,
        "total_pnl": 0
    }

def save_account(account: Dict):
    """保存账户信息"""
    account["last_updated"] = datetime.now().isoformat()
    with open(BASE_DIR / "account.json", 'w') as f:
        json.dump(account, f, ensure_ascii=False, indent=2)

def load_watchlist() -> Dict:
    """加载关注列表"""
    watchlist_file = BASE_DIR / "watchlist.json"
    if watchlist_file.exists():
        with open(watchlist_file, 'r') as f:
            return json.load(f)
    return {"stocks": []}

def save_watchlist(watchlist: Dict):
    """保存关注列表"""
    watchlist["last_updated"] = datetime.now().isoformat()
    with open(BASE_DIR / "watchlist.json", 'w') as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

def calculate_trade_cost(amount: float, is_sell: bool = False) -> float:
    """计算交易成本"""
    commission = max(amount * TRADING_RULES["commission_rate"], TRADING_RULES["min_commission"])
    transfer = amount * TRADING_RULES["transfer_fee"]
    stamp = amount * TRADING_RULES["stamp_tax"] if is_sell else 0
    return round(commission + transfer + stamp, 2)

def get_available_cash(account: Dict) -> float:
    """获取可用现金"""
    return account.get("current_cash", 0)

def get_holding_value(account: Dict, code: str) -> Tuple[int, float, float]:
    """获取持仓信息: (数量, 成本价, 市值)"""
    for h in account.get("holdings", []):
        if h["code"] == code:
            return h["quantity"], h["cost_price"], h.get("market_value", 0)
    return 0, 0, 0

def can_sell_today(account: Dict, code: str) -> int:
    """检查今日可卖数量(T+1规则)"""
    today = datetime.now().strftime("%Y-%m-%d")
    frozen = account.get("frozen_sells", [])
    
    holding_qty, _, _ = get_holding_value(account, code)
    frozen_qty = sum(f["quantity"] for f in frozen if f["code"] == code and f["buy_date"] == today)
    
    return max(0, holding_qty - frozen_qty)

def score_stock(code: str, realtime: Dict, klines: List[Dict], sentiment: Dict) -> Dict:
    """给股票打分"""
    score = 50  # 基础分
    reasons = []
    
    if not klines or len(klines) < 20:
        return {"score": 0, "action": "skip", "reasons": ["数据不足"]}
    
    # 1. 技术分析信号
    signals = generate_signals(klines)
    
    if signals["action"] == "buy":
        score += 20
        reasons.extend([f"技术买入: {r}" for r in signals["reasons"][:2]])
    elif signals["action"] == "weak_buy":
        score += 10
        reasons.extend([f"弱买入: {r}" for r in signals["reasons"][:2]])
    elif signals["action"] == "sell":
        score -= 20
        reasons.extend([f"技术卖出: {r}" for r in signals["reasons"][:2]])
    elif signals["action"] == "weak_sell":
        score -= 10
        reasons.extend([f"弱卖出: {r}" for r in signals["reasons"][:2]])
    
    # 2. 趋势分析
    closes = [k["close"] for k in klines]
    trend = analyze_trend(closes)
    
    if trend["trend"] == "strong_bullish":
        score += 15
        reasons.append("强势上涨趋势")
    elif trend["trend"] == "bullish":
        score += 8
        reasons.append("上涨趋势")
    elif trend["trend"] == "strong_bearish":
        score -= 15
        reasons.append("强势下跌趋势")
    elif trend["trend"] == "bearish":
        score -= 8
        reasons.append("下跌趋势")
    
    # 3. 量价关系
    if realtime:
        current_price = realtime.get("price", 0)
        pre_close = realtime.get("pre_close", 0)
        volume = realtime.get("volume", 0)
        
        if pre_close > 0:
            change_pct = (current_price - pre_close) / pre_close * 100
            
            # 今日表现
            if change_pct > 3:
                score += 10
                reasons.append(f"今日强势+{change_pct:.1f}%")
            elif change_pct > 1:
                score += 5
            elif change_pct < -3:
                score -= 10
                reasons.append(f"今日弱势{change_pct:.1f}%")
            elif change_pct < -1:
                score -= 5
        
        # 量比
        volumes = [k["volume"] for k in klines]
        if volumes:
            avg_vol = sum(volumes[-5:]) / 5
            if avg_vol > 0:
                vol_ratio = volume / avg_vol
                if vol_ratio > 2:
                    if change_pct > 0:
                        score += 8
                        reasons.append(f"放量上涨(量比{vol_ratio:.1f})")
                    else:
                        score -= 8
                        reasons.append(f"放量下跌(量比{vol_ratio:.1f})")
    
    # 4. 新闻情绪
    if sentiment:
        stock_mentions = sentiment.get("stock_mentions", {})
        if code in stock_mentions:
            stock_sentiment = stock_mentions[code]["sentiment"]
            if stock_sentiment > 2:
                score += 10
                reasons.append(f"新闻正面({stock_sentiment})")
            elif stock_sentiment < -2:
                score -= 10
                reasons.append(f"新闻负面({stock_sentiment})")
    
    # 5. 大盘环境
    overall = sentiment.get("overall_sentiment", 0) if sentiment else 0
    if overall > 5:
        score += 5
        reasons.append("市场情绪乐观")
    elif overall < -5:
        score -= 5
        reasons.append("市场情绪悲观")
    
    # 判断动作
    if score >= 70:
        action = "strong_buy"
    elif score >= 60:
        action = "buy"
    elif score <= 30:
        action = "strong_sell"
    elif score <= 40:
        action = "sell"
    else:
        action = "hold"
    
    return {
        "score": score,
        "action": action,
        "reasons": reasons,
        "signals": signals,
        "trend": trend["trend"]
    }

def generate_trade_decisions(account: Dict, watchlist: Dict, sentiment: Dict = None) -> List[Dict]:
    """生成交易决策"""
    decisions = []
    
    # 获取所有关注股票代码
    codes = [s["code"] for s in watchlist.get("stocks", [])]
    
    # 添加持仓股票
    for h in account.get("holdings", []):
        if h["code"] not in codes:
            codes.append(h["code"])
    
    if not codes:
        return decisions
    
    # 获取实时数据
    realtime = fetch_realtime_sina(codes)
    
    # 获取可用资金
    available_cash = get_available_cash(account)
    total_value = account.get("total_value", 1000000)
    current_position_pct = 1 - (available_cash / total_value)
    
    for code in codes:
        rt = realtime.get(code, {})
        if not rt or rt.get("price", 0) == 0:
            continue
        
        # 获取K线数据
        klines = fetch_kline(code, period="101", limit=60)
        
        # 打分
        analysis = score_stock(code, rt, klines, sentiment)
        
        decision = {
            "code": code,
            "name": rt.get("name", ""),
            "price": rt["price"],
            "score": analysis["score"],
            "action": analysis["action"],
            "reasons": analysis.get("reasons", []),
            "trend": analysis.get("trend", "unknown"),
            "timestamp": datetime.now().isoformat()
        }
        
        # 检查持仓
        holding_qty, cost_price, _ = get_holding_value(account, code)
        
        if holding_qty > 0:
            # 有持仓，检查止盈止损
            pnl_pct = (rt["price"] - cost_price) / cost_price
            decision["holding_qty"] = holding_qty
            decision["cost_price"] = cost_price
            decision["pnl_pct"] = round(pnl_pct * 100, 2)
            
            if pnl_pct <= TRADING_RULES["stop_loss_pct"]:
                decision["action"] = "stop_loss"
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
                decision["reasons"].append(f"触发止损({decision['pnl_pct']:.1f}%)")
            elif pnl_pct >= TRADING_RULES["take_profit_full_pct"]:
                decision["action"] = "take_profit_full"
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
                decision["reasons"].append(f"触发止盈清仓({decision['pnl_pct']:.1f}%)")
            elif pnl_pct >= TRADING_RULES["take_profit_pct"] and analysis["action"] in ["sell", "strong_sell"]:
                decision["action"] = "take_profit_partial"
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code) // 2
                decision["reasons"].append(f"止盈减仓({decision['pnl_pct']:.1f}%)")
            elif analysis["action"] in ["strong_sell"]:
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
        else:
            # 无持仓，考虑买入
            if analysis["action"] in ["buy", "strong_buy"]:
                if current_position_pct < TRADING_RULES["max_total_position"]:
                    max_amount = min(
                        available_cash * 0.3,  # 单次最多用30%可用资金
                        total_value * TRADING_RULES["max_position_pct"]  # 单只最大15%仓位
                    )
                    if max_amount >= TRADING_RULES["min_buy_amount"]:
                        quantity = int(max_amount / rt["price"] / 100) * 100  # 整百股
                        if quantity >= 100:
                            decision["trade_type"] = "buy"
                            decision["quantity"] = quantity
                            decision["amount"] = round(quantity * rt["price"], 2)
        
        decisions.append(decision)
    
    # 按分数排序
    decisions.sort(key=lambda x: x["score"], reverse=True)
    
    return decisions

def execute_trade(account: Dict, decision: Dict) -> Dict:
    """执行交易(模拟)"""
    if "trade_type" not in decision or "quantity" not in decision:
        return {"success": False, "reason": "无交易指令"}
    
    trade_type = decision["trade_type"]
    code = decision["code"]
    name = decision.get("name", code)
    price = decision["price"]
    quantity = decision["quantity"]
    
    if quantity <= 0:
        return {"success": False, "reason": "数量无效"}
    
    amount = quantity * price
    cost = calculate_trade_cost(amount, is_sell=(trade_type == "sell"))
    
    trade_record = {
        "trade_id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{code}",
        "code": code,
        "name": name,
        "type": trade_type,
        "price": price,
        "quantity": quantity,
        "amount": amount,
        "cost": cost,
        "timestamp": datetime.now().isoformat(),
        "reasons": decision.get("reasons", [])
    }
    
    if trade_type == "buy":
        total_cost = amount + cost
        if total_cost > account["current_cash"]:
            return {"success": False, "reason": "现金不足"}
        
        account["current_cash"] -= total_cost
        
        # 更新持仓
        found = False
        for h in account["holdings"]:
            if h["code"] == code:
                # 加仓，计算新成本
                old_cost = h["cost_price"] * h["quantity"]
                h["quantity"] += quantity
                h["cost_price"] = round((old_cost + amount) / h["quantity"], 3)
                h["last_buy_date"] = datetime.now().strftime("%Y-%m-%d")
                found = True
                break
        
        if not found:
            account["holdings"].append({
                "code": code,
                "name": name,
                "quantity": quantity,
                "cost_price": price,
                "last_buy_date": datetime.now().strftime("%Y-%m-%d")
            })
        
        # 记录今日买入(T+1冻结)
        account.setdefault("frozen_sells", []).append({
            "code": code,
            "quantity": quantity,
            "buy_date": datetime.now().strftime("%Y-%m-%d")
        })
        
        trade_record["net_amount"] = -total_cost
        
    elif trade_type == "sell":
        holding_qty, cost_price, _ = get_holding_value(account, code)
        if quantity > holding_qty:
            quantity = holding_qty
            trade_record["quantity"] = quantity
            amount = quantity * price
            trade_record["amount"] = amount
        
        sellable = can_sell_today(account, code)
        if quantity > sellable:
            return {"success": False, "reason": f"今日可卖{sellable}股(T+1限制)"}
        
        net_receive = amount - cost
        account["current_cash"] += net_receive
        
        # 更新持仓
        for i, h in enumerate(account["holdings"]):
            if h["code"] == code:
                h["quantity"] -= quantity
                if h["quantity"] <= 0:
                    account["holdings"].pop(i)
                break
        
        trade_record["net_amount"] = net_receive
        trade_record["pnl"] = round((price - cost_price) * quantity - cost, 2)
    
    # 保存交易记录
    tx_file = BASE_DIR / "transactions.json"
    if tx_file.exists():
        with open(tx_file, 'r') as f:
            transactions = json.load(f)
    else:
        transactions = []
    
    transactions.append(trade_record)
    with open(tx_file, 'w') as f:
        json.dump(transactions, f, ensure_ascii=False, indent=2)
    
    # 更新账户
    save_account(account)
    
    return {"success": True, "trade": trade_record}

def run_trading_cycle():
    """运行一次交易周期"""
    print(f"\n{'='*60}")
    print(f"交易周期开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print('='*60)
    
    # 1. 加载数据
    account = load_account()
    watchlist = load_watchlist()
    
    print(f"\n[账户状态]")
    print(f"  现金: ¥{account['current_cash']:,.2f}")
    print(f"  持仓: {len(account.get('holdings', []))}只")
    
    # 2. 获取市场情绪
    print("\n[获取市场情绪...]")
    try:
        sentiment = get_market_sentiment()
        print(f"  整体情绪: {sentiment['overall_label']} ({sentiment['overall_sentiment']:+d})")
    except Exception as e:
        print(f"  获取失败: {e}")
        sentiment = None
    
    # 3. 获取大盘
    print("\n[大盘指数]")
    market = fetch_market_overview()
    for code, info in list(market.items())[:3]:
        emoji = "🟢" if info.get("change_pct", 0) > 0 else "🔴"
        print(f"  {emoji} {info['name']}: {info['price']} ({info.get('change_pct', 0):+.2f}%)")
    
    # 4. 生成交易决策
    print("\n[分析股票...]")
    decisions = generate_trade_decisions(account, watchlist, sentiment)
    
    # 5. 执行交易
    trades_executed = []
    
    for d in decisions:
        emoji = "🟢" if d["score"] >= 60 else ("🔴" if d["score"] <= 40 else "⚪")
        print(f"\n{emoji} {d['name']}({d['code']})")
        print(f"   价格: ¥{d['price']}  分数: {d['score']}  动作: {d['action']}")
        print(f"   理由: {', '.join(d['reasons'][:3])}")
        
        if "trade_type" in d and d.get("quantity", 0) > 0:
            result = execute_trade(account, d)
            if result["success"]:
                trade = result["trade"]
                action_emoji = "📈" if trade["type"] == "buy" else "📉"
                print(f"   {action_emoji} 执行{trade['type'].upper()}: {trade['quantity']}股 @ ¥{trade['price']}")
                trades_executed.append(trade)
            else:
                print(f"   ⚠️ 未执行: {result['reason']}")
    
    # 6. 更新账户市值
    account = load_account()  # 重新加载
    holdings_value = 0
    realtime = fetch_realtime_sina([h["code"] for h in account.get("holdings", [])])
    
    for h in account.get("holdings", []):
        price = realtime.get(h["code"], {}).get("price", h["cost_price"])
        h["market_value"] = round(price * h["quantity"], 2)
        h["current_price"] = price
        h["pnl_pct"] = round((price - h["cost_price"]) / h["cost_price"] * 100, 2)
        holdings_value += h["market_value"]
    
    account["total_value"] = round(account["current_cash"] + holdings_value, 2)
    account["total_pnl"] = round(account["total_value"] - account["initial_capital"], 2)
    account["total_pnl_pct"] = round(account["total_pnl"] / account["initial_capital"] * 100, 2)
    save_account(account)
    
    # 7. 生成报告
    print(f"\n{'='*60}")
    print("[账户总览]")
    print(f"  总市值: ¥{account['total_value']:,.2f}")
    print(f"  现金: ¥{account['current_cash']:,.2f}")
    print(f"  持仓市值: ¥{holdings_value:,.2f}")
    print(f"  总盈亏: ¥{account['total_pnl']:+,.2f} ({account['total_pnl_pct']:+.2f}%)")
    
    if account.get("holdings"):
        print("\n[持仓明细]")
        for h in account["holdings"]:
            emoji = "🟢" if h.get("pnl_pct", 0) >= 0 else "🔴"
            print(f"  {emoji} {h['name']}({h['code']}): {h['quantity']}股 @ ¥{h.get('current_price', h['cost_price'])}")
            print(f"      成本¥{h['cost_price']} 盈亏{h.get('pnl_pct', 0):+.2f}%")
    
    print(f"\n本次交易: {len(trades_executed)}笔")
    print('='*60)
    
    return {
        "timestamp": datetime.now().isoformat(),
        "account": account,
        "trades": trades_executed,
        "decisions_count": len(decisions)
    }


# ============ T+0 增强功能 ============

# 初始化策略实例
t0_strategy = T0Strategy()
factor_model = FactorModel()

def run_t0_check(account: Dict = None) -> List[Dict]:
    """
    运行 T+0 策略检查
    检查持仓股票是否有日内交易机会
    """
    if account is None:
        account = load_account()
    
    t0_signals = []
    holdings = account.get("holdings", [])
    
    if not holdings:
        return t0_signals
    
    # 获取持仓股票实时数据
    codes = [h["code"] for h in holdings]
    realtime = fetch_realtime_sina(codes)
    
    for h in holdings:
        code = h["code"]
        rt = realtime.get(code, {})
        
        if not rt or rt.get("price", 0) == 0:
            continue
        
        # 检查可卖数量 (T+1: 只能卖昨日持仓)
        sellable_qty = can_sell_today(account, code)
        
        if sellable_qty <= 0:
            continue  # 今日买入的不能卖
        
        # 获取今日已卖出情况
        today = datetime.now().strftime("%Y-%m-%d")
        today_sells = [t for t in account.get("transactions", []) 
                      if t.get("date") == today and t.get("code") == code and t.get("type") == "t0_sell"]
        already_sold = sum(t.get("quantity", 0) for t in today_sells)
        sold_avg_price = sum(t["price"] * t["quantity"] for t in today_sells) / already_sold if already_sold > 0 else 0
        
        # 生成 T+0 信号
        signal = t0_strategy.generate_t0_signal(
            code=code,
            current_price=rt["price"],
            pre_close=rt.get("pre_close", 0),
            open_price=rt.get("open", 0),
            high_price=rt.get("high", 0),
            low_price=rt.get("low", 0),
            available_sell_qty=sellable_qty - already_sold,
            cost_price=h["cost_price"],
            already_sold_today=already_sold,
            sold_avg_price=sold_avg_price
        )
        
        if signal:
            signal["name"] = h["name"]
            t0_signals.append(signal)
    
    return t0_signals


def score_with_factor_model(code: str, klines: List[Dict], realtime: Dict = None,
                           signals: Dict = None, sentiment: Dict = None,
                           market: Dict = None) -> Dict:
    """
    使用多因子模型评分
    """
    return factor_model.calculate_composite_score(
        klines=klines,
        realtime=realtime,
        signals=signals,
        sentiment=sentiment,
        market=market
    )


def run_enhanced_trading_cycle():
    """
    增强版交易周期
    整合 T+0 策略和多因子模型
    """
    print(f"\n{'='*60}")
    print(f"[增强版交易周期] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print('='*60)
    
    # 检查交易时间
    is_trading, session = t0_strategy.is_trading_time()
    if not is_trading:
        print(f"⏰ 非交易时间 (状态: {session})")
        return None
    
    account = load_account()
    watchlist = load_watchlist()
    
    # 1. 获取市场数据
    print("\n[1] 获取市场数据...")
    market = fetch_market_overview()
    sentiment = get_market_sentiment()
    
    # 2. T+0 检查 (优先处理)
    print("\n[2] T+0 策略检查...")
    t0_signals = run_t0_check(account)
    
    t0_trades = []
    for signal in t0_signals:
        print(f"  💫 T+0 信号: {signal['name']}({signal['code']})")
        print(f"     动作: {signal['action']} | 原因: {signal['reason']}")
        print(f"     价格: ¥{signal['price']} | 数量: {signal['quantity']}股")
        
        # 执行 T+0 交易
        if signal["action"] == "t0_sell":
            result = execute_trade(account, {
                "code": signal["code"],
                "name": signal["name"],
                "price": signal["price"],
                "trade_type": "sell",
                "quantity": signal["quantity"],
                "t0": True
            })
            if result["success"]:
                t0_trades.append(result["trade"])
                print(f"     ✅ T+0 卖出成功")
        elif signal["action"] == "t0_buy":
            result = execute_trade(account, {
                "code": signal["code"],
                "name": signal["name"],
                "price": signal["price"],
                "trade_type": "buy",
                "quantity": signal["quantity"],
                "t0": True
            })
            if result["success"]:
                t0_trades.append(result["trade"])
                print(f"     ✅ T+0 买回成功")
    
    # 3. 多因子选股分析
    print("\n[3] 多因子模型分析...")
    all_codes = [s["code"] for s in watchlist.get("stocks", [])]
    all_codes.extend([h["code"] for h in account.get("holdings", []) if h["code"] not in all_codes])
    
    factor_scores = []
    for code in all_codes[:20]:  # 限制数量避免太慢
        klines = fetch_kline(code, period="101", limit=60)
        if len(klines) < 20:
            continue
        
        realtime = fetch_realtime_sina([code]).get(code, {})
        signals = generate_signals(klines)
        
        result = score_with_factor_model(
            code=code,
            klines=klines,
            realtime=realtime,
            signals=signals,
            sentiment=sentiment,
            market=market
        )
        
        factor_scores.append({
            "code": code,
            "name": realtime.get("name", ""),
            "price": realtime.get("price", 0),
            "score": result["total_score"],
            "recommendation": result["recommendation"],
            "action_cn": result["action_cn"]
        })
    
    # 排序
    factor_scores.sort(key=lambda x: x["score"], reverse=True)
    
    print("\n  [多因子排名 Top 5]")
    for i, fs in enumerate(factor_scores[:5], 1):
        emoji = "🔥" if fs["score"] >= 70 else ("✅" if fs["score"] >= 60 else "⚪")
        print(f"  {i}. {emoji} {fs['name']}({fs['code']}): {fs['score']:.1f}分 - {fs['action_cn']}")
    
    # 4. 常规交易决策 (基于多因子得分)
    print("\n[4] 交易决策执行...")
    regular_trades = []
    
    # 买入逻辑：高分股票
    for fs in factor_scores:
        if fs["score"] >= 65 and fs["recommendation"] in ["buy", "strong_buy"]:
            holding_qty, _, _ = get_holding_value(account, fs["code"])
            if holding_qty == 0:  # 未持仓
                available_cash = get_available_cash(account)
                if available_cash > TRADING_RULES["min_buy_amount"]:
                    max_amount = min(
                        available_cash * 0.25,
                        account.get("total_value", 1000000) * TRADING_RULES["max_position_pct"]
                    )
                    quantity = int(max_amount / fs["price"] / 100) * 100
                    if quantity >= 100:
                        result = execute_trade(account, {
                            "code": fs["code"],
                            "name": fs["name"],
                            "price": fs["price"],
                            "trade_type": "buy",
                            "quantity": quantity
                        })
                        if result["success"]:
                            regular_trades.append(result["trade"])
                            print(f"  📈 买入 {fs['name']}: {quantity}股 @ ¥{fs['price']}")
    
    # 卖出逻辑：低分持仓
    for h in account.get("holdings", []):
        code = h["code"]
        score_info = next((fs for fs in factor_scores if fs["code"] == code), None)
        
        if score_info and score_info["score"] <= 35:
            sellable = can_sell_today(account, code)
            if sellable > 0:
                result = execute_trade(account, {
                    "code": code,
                    "name": h["name"],
                    "price": score_info["price"],
                    "trade_type": "sell",
                    "quantity": sellable
                })
                if result["success"]:
                    regular_trades.append(result["trade"])
                    print(f"  📉 卖出 {h['name']}: {sellable}股 @ ¥{score_info['price']} (低分清仓)")
    
    # 5. 更新账户
    account = load_account()
    holdings_value = 0
    if account.get("holdings"):
        realtime = fetch_realtime_sina([h["code"] for h in account["holdings"]])
        for h in account["holdings"]:
            price = realtime.get(h["code"], {}).get("price", h["cost_price"])
            h["market_value"] = round(price * h["quantity"], 2)
            h["current_price"] = price
            h["pnl_pct"] = round((price - h["cost_price"]) / h["cost_price"] * 100, 2)
            holdings_value += h["market_value"]
    
    account["total_value"] = round(account["current_cash"] + holdings_value, 2)
    account["total_pnl"] = round(account["total_value"] - account["initial_capital"], 2)
    account["total_pnl_pct"] = round(account["total_pnl"] / account["initial_capital"] * 100, 2)
    save_account(account)
    
    # 6. 汇总报告
    all_trades = t0_trades + regular_trades
    
    print(f"\n{'='*60}")
    print(f"[交易汇总]")
    print(f"  T+0 交易: {len(t0_trades)}笔")
    print(f"  常规交易: {len(regular_trades)}笔")
    print(f"  总资产: ¥{account['total_value']:,.2f}")
    print(f"  盈亏: ¥{account['total_pnl']:+,.2f} ({account['total_pnl_pct']:+.2f}%)")
    print('='*60)
    
    return {
        "timestamp": datetime.now().isoformat(),
        "t0_trades": t0_trades,
        "regular_trades": regular_trades,
        "factor_scores": factor_scores[:10],
        "account": account
    }


if __name__ == "__main__":
    run_trading_cycle()
