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
try:
    from technical_analysis import calculate_hybrid_atr, calculate_atr
except ImportError:
    calculate_hybrid_atr = None
    calculate_atr = None
from news_sentiment import get_market_sentiment
from t0_strategy import T0Strategy, IntradayMomentum, VWAPStrategy
from factor_model import FactorModel, StockScreener

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# 交易规则配置
TRADING_RULES = {
    "min_buy_amount": 5000,       # 最小买入金额
    "max_position_pct": 0.15,     # 单只最大仓位15%
    "max_total_position": 0.50,   # 最大总仓位50%（节前轻仓）
    "stop_loss_pct": -0.05,       # 止损-5%（收紧）
    "take_profit_pct": 0.04,      # 止盈+4%减仓（更早触发）
    "take_profit_full_pct": 0.08, # 止盈+8%全出（更早触发）
    "commission_rate": 0.00025,   # 佣金万2.5
    "min_commission": 5,          # 最低佣金5元
    "stamp_tax": 0.001,           # 印花税千1(卖出)
    "transfer_fee": 0.00002,      # 过户费万0.2
    "underperform_alert_pct": -0.015,  # 逆市下跌预警阈值
    "clearance_first_batch_pct": 0.7,  # 清仓时首批卖出比例
}

# 从策略参数文件动态加载（如有）
def _load_strategy_params():
    params_file = BASE_DIR / "strategy_params.json"
    if params_file.exists():
        import json as _json
        with open(params_file, 'r') as f:
            params = _json.load(f)
        # v2兼容参数
        for key in ["stop_loss_pct", "take_profit_pct", "take_profit_full_pct",
                     "max_position_pct", "max_total_position", "min_buy_amount",
                     "underperform_alert_pct", "clearance_first_batch_pct"]:
            if key in params:
                TRADING_RULES[key] = params[key]
        # v3新参数
        for key in ["take_profit_atr_multiplier", "take_profit_full_atr_multiplier",
                     "trailing_stop_atr_multiplier", "trailing_stop_trigger_atr_multiplier",
                     "trailing_stop_sell_pct", "passive_overweight_tolerance",
                     "residual_clear_threshold_pct", "residual_clear_max_hold_days",
                     "limit_up_filter_daily_pct", "limit_up_filter_daily_soft_pct",
                     "limit_up_filter_soft_min_score", "limit_up_filter_3day_pct",
                     "atr_period", "atr_fast_period", "atr_use_hybrid",
                     "underperform_consecutive_days_to_act", "underperform_reduce_pct",
                     "min_score",
                     "max_daily_buys", "same_day_rebuy_ban", "buy_reasons_required",
                     "min_position_pct", "first_buy_max_pct",
                     "ineffective_position_pct", "intraday_high_zone_pct"]:
            if key in params:
                TRADING_RULES[key] = params[key]

_load_strategy_params()

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


def get_today_stop_loss_codes() -> set:
    """获取今日止损卖出的股票代码（24h内禁止买回）"""
    today = datetime.now().strftime("%Y-%m-%d")
    tx_file = BASE_DIR / "transactions.json"
    if not tx_file.exists():
        return set()
    try:
        with open(tx_file, 'r') as f:
            transactions = json.load(f)
        stop_loss_codes = set()
        for t in transactions:
            if (t.get("type") == "sell" and
                t.get("timestamp", "").startswith(today) and
                any("止损" in r for r in t.get("reasons", []))):
                stop_loss_codes.add(t["code"])
        return stop_loss_codes
    except Exception:
        return set()


def get_today_buy_count() -> int:
    """获取今日已买入的股票数量（不同代码去重）"""
    today = datetime.now().strftime("%Y-%m-%d")
    tx_file = BASE_DIR / "transactions.json"
    if not tx_file.exists():
        return 0
    try:
        with open(tx_file, 'r') as f:
            transactions = json.load(f)
        buy_codes = set()
        for t in transactions:
            if (t.get("type") == "buy" and
                t.get("timestamp", "").startswith(today)):
                buy_codes.add(t["code"])
        return len(buy_codes)
    except Exception:
        return 0

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
    
    # === P0: MA5均线过滤（10次复盘提出，终于入码！） ===
    if len(closes) >= 5:
        ma5 = sum(closes[-5:]) / 5
        current_close = closes[-1]
        if realtime and realtime.get("price", 0) > 0:
            current_close = realtime["price"]
        if current_close < ma5:
            score -= 20
            reasons.append(f"⚠️均线过滤: 价格{current_close:.2f}<MA5({ma5:.2f})")
        elif current_close > ma5 * 1.02:
            score += 5
            reasons.append(f"价格站上MA5({ma5:.2f})+2%")
    
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
            
            # === P1: 日内跌幅过滤（当日跌>2%额外扣30分，防止买入当日暴跌股） ===
            if change_pct <= -2:
                score -= 30
                reasons.append(f"⚠️日内跌幅过滤: 今日{change_pct:.1f}%(<=-2%)扣30分")
            
            # === P1: 日内高位过滤（冲高回落区降权，防止追高买入） ===
            high = rt.get("high", 0)
            low = rt.get("low", 0)
            intraday_range = high - low
            high_zone_pct = TRADING_RULES.get("intraday_high_zone_pct", 0.75)
            if intraday_range > 0 and high > 0:
                position_in_range = (current_price - low) / intraday_range
                if position_in_range >= high_zone_pct and change_pct > 2:
                    score -= 15
                    reasons.append(f"⚠️日内高位: 价格在振幅{position_in_range*100:.0f}%位置(>{high_zone_pct*100:.0f}%)且涨{change_pct:.1f}%，降权15分")
        
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
    
    # === P0: A股特色因子（连板 + 融资融券） ===
    try:
        from china_factors import score_china_factors
        china_result = score_china_factors(code)
        score += china_result['score']
        reasons.extend(china_result['reasons'])
    except Exception:
        pass  # 不影响原有流程
    
    # ============ 新增：Qlib LightGBM ML打分 ============
    # 影子模式：qlib_enabled=false时只记录不影响打分
    ml_score = None
    try:
        _sp_file = Path(__file__).parent.parent / "strategy_params.json"
        _sp = {}
        if _sp_file.exists():
            with open(_sp_file, 'r') as _f:
                _sp = json.load(_f)
        qlib_enabled = _sp.get("qlib_enabled", False)
        qlib_weight = _sp.get("qlib_weight", 0.4)

        from qlib_scorer import get_ml_scores
        _ml_results = get_ml_scores([code])
        if code in _ml_results:
            ml_score = _ml_results[code]
            if qlib_enabled:
                rule_score = score
                score = rule_score * (1 - qlib_weight) + ml_score * qlib_weight
                reasons.append(f"🤖ML混合: 规则{rule_score:.0f}*{1-qlib_weight:.0%} + ML{ml_score:.0f}*{qlib_weight:.0%} = {score:.0f}")
            else:
                reasons.append(f"🤖ML影子: ml_score={ml_score:.0f}(未启用,仅记录)")
    except Exception as _e:
        import traceback as _tb
        logging.getLogger("qlib_scorer").debug(f"ML打分跳过: {_e}")

    # ============ 新增：AI增强情绪因子（权重15%） ============
    # 说明：不改变既有接口，仅在 score_stock 内追加融合逻辑。
    # - 个股情绪 analyze_stock_sentiment: [-10, +10] -> 映射到 [0, 100]
    # - 最终分数做加权融合：score = score*0.85 + sentiment_score*0.15
    try:
        from sentiment_enhanced import analyze_stock_sentiment, calculate_fear_greed

        stock_name = (realtime or {}).get("name") or code
        raw_sent = analyze_stock_sentiment(code, stock_name)  # [-10, +10]
        mapped_sent = (float(raw_sent) + 10.0) / 20.0 * 100.0
        mapped_sent = max(0.0, min(100.0, mapped_sent))

        score_before = score
        score = score * 0.85 + mapped_sent * 0.15
        reasons.append(f"AI情绪{raw_sent:+.1f} -> {mapped_sent:.0f}分(权重15%)")

        # 恐贪指数用于动态阈值（更贴近逆向/获利了结）
        fg = calculate_fear_greed()
        fg_score = int(fg.get("score", 50)) if isinstance(fg, dict) else 50

        buy_shift = -5 if fg_score < 30 else 0
        sell_shift = 5 if fg_score > 70 else 0  # 更容易卖出：提高卖出触发阈值

        strong_buy_th = 70 + buy_shift
        buy_th = 60 + buy_shift
        strong_sell_th = 30 + sell_shift
        sell_th = 40 + sell_shift

        if fg_score < 30:
            reasons.append(f"恐贪{fg_score}(<30)：买入阈值下调5分")
        elif fg_score > 70:
            reasons.append(f"恐贪{fg_score}(>70)：卖出阈值下调5分(更易卖出)")

    except Exception:
        # 任何异常都不影响原流程
        fg_score = 50
        strong_buy_th = 70
        buy_th = 60
        strong_sell_th = 30
        sell_th = 40

    # 判断动作（结合恐贪阈值动态调整）
    if score >= strong_buy_th:
        action = "strong_buy"
    elif score >= buy_th:
        action = "buy"
    elif score <= strong_sell_th:
        action = "strong_sell"
    elif score <= sell_th:
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
            
            # === v3: ATR自适应止盈 ===
            atr_pct = 0.02  # 默认2%
            if calculate_hybrid_atr and klines:
                atr_pct = calculate_hybrid_atr(klines, rt)
            
            tp_atr_mult = TRADING_RULES.get("take_profit_atr_multiplier", 2.0)
            tp_full_atr_mult = TRADING_RULES.get("take_profit_full_atr_multiplier", 4.0)
            atr_tp = atr_pct * tp_atr_mult  # ATR止盈减仓
            atr_tp_full = atr_pct * tp_full_atr_mult  # ATR止盈全出
            
            # 取ATR止盈和固定止盈中更大的，避免低波蓝筹阈值太小
            effective_tp = max(atr_tp, TRADING_RULES.get("take_profit_pct", 0.04))
            effective_tp_full = max(atr_tp_full, TRADING_RULES.get("take_profit_full_pct", 0.08))
            
            # === v3: 追踪止盈 ===
            trailing_trigger = atr_pct * TRADING_RULES.get("trailing_stop_trigger_atr_multiplier", 2.0)
            trailing_drawdown = atr_pct * TRADING_RULES.get("trailing_stop_atr_multiplier", 1.5)
            trailing_sell_pct = TRADING_RULES.get("trailing_stop_sell_pct", 0.6)
            
            # 更新持仓最高价记录
            for h in account.get("holdings", []):
                if h["code"] == code:
                    if "high_since_entry" not in h:
                        h["high_since_entry"] = max(rt["price"], cost_price)
                    if rt["price"] > h["high_since_entry"]:
                        h["high_since_entry"] = rt["price"]
                    high_since = h["high_since_entry"]
                    break
            else:
                high_since = rt["price"]
            
            # === v3: 残仓自动清理 ===
            residual_threshold = TRADING_RULES.get("residual_clear_threshold_pct", 0.005)
            holding_value = holding_qty * rt["price"]
            is_residual = (holding_value / total_value) < residual_threshold if total_value > 0 else False
            
            if pnl_pct <= TRADING_RULES["stop_loss_pct"]:
                decision["action"] = "stop_loss"
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
                # ATR自适应止损：使用max(固定止损, -2×ATR)，高波动股用更宽止损
                fixed_sl = TRADING_RULES["stop_loss_pct"]
                atr_sl = -(atr_pct * 2)  # 2倍ATR作为止损线
                effective_sl = min(fixed_sl, atr_sl)  # 取更宽的（更负的值）
                if pnl_pct <= effective_sl:
                    decision["reasons"].append(f"触发ATR止损({decision['pnl_pct']:.1f}% <= {effective_sl*100:.1f}%, ATR={atr_pct*100:.1f}%)")
                else:
                    # 固定止损触发但ATR止损未触发 → 仍止损但标注
                    decision["reasons"].append(f"触发固定止损({decision['pnl_pct']:.1f}% <= {fixed_sl*100:.1f}%, ATR止损线={atr_sl*100:.1f}%)")
            elif is_residual and holding_qty <= 300:
                # v3: 残仓清理（<总资产0.5%且<=300股）
                decision["action"] = "residual_clear"
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
                decision["reasons"].append(f"残仓清理: {holding_qty}股 市值¥{holding_value:.0f} (<{residual_threshold*100:.1f}%)")
            elif pnl_pct >= trailing_trigger and high_since > 0:
                # v3: 追踪止盈检查
                drawdown_from_high = (high_since - rt["price"]) / high_since if high_since > 0 else 0
                if drawdown_from_high >= trailing_drawdown:
                    sell_qty = int(can_sell_today(account, code) * trailing_sell_pct / 100) * 100
                    if sell_qty >= 100:
                        decision["action"] = "trailing_stop"
                        decision["trade_type"] = "sell"
                        decision["quantity"] = sell_qty
                        decision["reasons"].append(f"追踪止盈: 从最高{high_since:.2f}回撤{drawdown_from_high*100:.1f}%>={trailing_drawdown*100:.1f}%")
            elif pnl_pct >= effective_tp_full:
                decision["action"] = "take_profit_full"
                decision["trade_type"] = "sell"
                sellable = can_sell_today(account, code)
                first_batch = TRADING_RULES.get("clearance_first_batch_pct", 0.6)
                decision["quantity"] = int(sellable * first_batch / 100) * 100 or sellable
                decision["reasons"].append(f"ATR止盈清仓({decision['pnl_pct']:.1f}% >= {effective_tp_full*100:.1f}%)")
            elif pnl_pct >= effective_tp and analysis["action"] in ["sell", "strong_sell", "hold"]:
                decision["action"] = "take_profit_partial"
                decision["trade_type"] = "sell"
                sellable = can_sell_today(account, code)
                first_batch = TRADING_RULES.get("clearance_first_batch_pct", 0.6)
                decision["quantity"] = int(sellable * first_batch / 100) * 100 or (sellable // 2)
                decision["reasons"].append(f"ATR止盈减仓({decision['pnl_pct']:.1f}% >= {effective_tp*100:.1f}%, ATR={atr_pct*100:.1f}%)")
            elif analysis["action"] in ["strong_sell"]:
                decision["trade_type"] = "sell"
                decision["quantity"] = can_sell_today(account, code)
        else:
            # 无持仓，考虑买入
            if analysis["action"] in ["buy", "strong_buy"]:
                # === v3: 涨停过滤 ===
                pre_close = rt.get("pre_close", 0)
                if pre_close > 0:
                    daily_change_pct = (rt["price"] - pre_close) / pre_close
                    # 3日累计涨幅过滤
                    kline_3d_change = 0
                    if klines and len(klines) >= 4:
                        close_3d_ago = klines[-4]["close"]
                        kline_3d_change = (rt["price"] - close_3d_ago) / close_3d_ago if close_3d_ago > 0 else 0
                    
                    limit_daily = TRADING_RULES.get("limit_up_filter_daily_pct", 0.07)
                    limit_daily_soft = TRADING_RULES.get("limit_up_filter_daily_soft_pct", 0.05)
                    limit_soft_score = TRADING_RULES.get("limit_up_filter_soft_min_score", 80)
                    limit_3day = TRADING_RULES.get("limit_up_filter_3day_pct", 0.12)
                    
                    if daily_change_pct >= limit_daily:
                        decision["reasons"].append(f"⛔涨停过滤: 涨幅{daily_change_pct*100:.1f}%>={limit_daily*100:.0f}%")
                        decisions.append(decision)
                        continue
                    if daily_change_pct >= limit_daily_soft and analysis["score"] < limit_soft_score:
                        decision["reasons"].append(f"⛔追高过滤: 涨幅{daily_change_pct*100:.1f}%且评分{analysis['score']:.0f}<{limit_soft_score}")
                        decisions.append(decision)
                        continue
                    if kline_3d_change >= limit_3day:
                        decision["reasons"].append(f"⛔3日累计过滤: 涨幅{kline_3d_change*100:.1f}%>={limit_3day*100:.0f}%")
                        decisions.append(decision)
                        continue
                
                # === v3: 仓位硬阻断 ===
                max_total = TRADING_RULES.get("max_total_position", 0.50)
                if current_position_pct >= max_total:
                    decision["reasons"].append(f"⛔仓位硬阻断: 当前仓位{current_position_pct*100:.0f}%>={max_total*100:.0f}%")
                    decisions.append(decision)
                    continue
                
                # === P1: 新仓分批制 + 最小有效建仓阈值 ===
                first_buy_max = TRADING_RULES.get("first_buy_max_pct", 0.07)
                min_position = TRADING_RULES.get("min_position_pct", 0.05)
                max_amount = min(
                    available_cash * 0.3,
                    total_value * first_buy_max,  # 首笔上限(默认7%，而非12%)
                    total_value * (max_total - current_position_pct)  # v3: 不超过仓位上限
                )
                min_amount = total_value * min_position  # 最小有效建仓金额(5%)
                if max_amount >= min_amount and max_amount >= TRADING_RULES["min_buy_amount"]:
                    quantity = int(max_amount / rt["price"] / 100) * 100
                    if quantity >= 100:
                        actual_amount = quantity * rt["price"]
                        if actual_amount >= min_amount:
                            decision["trade_type"] = "buy"
                            decision["quantity"] = quantity
                            decision["amount"] = round(actual_amount, 2)
                        else:
                            decision["reasons"].append(f"⛔最小仓位过滤: ¥{actual_amount:.0f}<{min_position*100:.0f}%总资产(¥{min_amount:.0f})")
                else:
                    if max_amount < min_amount:
                        decision["reasons"].append(f"⛔最小仓位过滤: 可用¥{max_amount:.0f}<{min_position*100:.0f}%总资产(¥{min_amount:.0f})")
        
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
        # === P0: 止损后同日禁买 ===
        stop_loss_codes = get_today_stop_loss_codes()
        if code in stop_loss_codes:
            return {"success": False, "reason": f"⛔止损后同日禁买: {name}({code})今日已止损"}

        # === P0: reasons空阻断 ===
        if not decision.get("reasons") and not decision.get("reason"):
            return {"success": False, "reason": f"⛔reasons空阻断: {name}({code})无买入理由"}

        # === P0: max_daily_buys限制 ===
        max_daily_buys = TRADING_RULES.get("max_daily_buys", 2)
        today_buys = get_today_buy_count()
        # 检查是否是新股（今天还没买过这只）
        today = datetime.now().strftime("%Y-%m-%d")
        tx_file = BASE_DIR / "transactions.json"
        already_bought_today = False
        if tx_file.exists():
            try:
                with open(tx_file, 'r') as f:
                    txns = json.load(f)
                already_bought_today = any(
                    t.get("type") == "buy" and t.get("code") == code and t.get("timestamp", "").startswith(today)
                    for t in txns
                )
            except Exception:
                pass
        if not already_bought_today and today_buys >= max_daily_buys:
            return {"success": False, "reason": f"⛔日买入限制: 今日已买{today_buys}只(上限{max_daily_buys})"}

        total_cost = amount + cost
        if total_cost > account["current_cash"]:
            return {"success": False, "reason": "现金不足"}
        
        account["current_cash"] -= total_cost
        
        # 更新持仓
        found = False
        for h in account["holdings"]:
            if h["code"] == code:
                # 加仓，计算新成本（含手续费）
                old_cost = h["cost_price"] * h["quantity"]
                h["quantity"] += quantity
                h["cost_price"] = round((old_cost + amount + cost) / h["quantity"], 3)
                h["last_buy_date"] = datetime.now().strftime("%Y-%m-%d")
                found = True
                break
        
        if not found:
            account["holdings"].append({
                "code": code,
                "name": name,
                "quantity": quantity,
                "cost_price": round((amount + cost) / quantity, 3),
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
    
    # 1.5 风控检查：回撤熔断 + 组合风险
    try:
        from risk_manager import check_drawdown_circuit_breaker, calculate_portfolio_risk
        
        cb = check_drawdown_circuit_breaker(account, max_dd=0.10)
        if cb.get("triggered"):
            print(f"\n🚨 [回撤熔断触发] 回撤 {cb.get('drawdown_pct', 0)*100:.1f}% > 10%")
            print(f"   动作: {cb.get('action')} — 暂停所有买入，仅允许减仓")
            # 保存更新后的 peak_value
            save_account(account)
        else:
            dd_pct = cb.get('drawdown_pct', 0) * 100
            print(f"\n✅ [风控] 回撤 {dd_pct:.1f}% (阈值10%)  峰值 ¥{cb.get('peak_value', 0):,.0f}")
        
        risk = calculate_portfolio_risk(account)
        risk_level = risk.get("overall_risk", "unknown")
        risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk_level, "⚪")
        print(f"   {risk_emoji} 组合风险: {risk_level}  仓位: {risk.get('position_pct', 0)*100:.0f}%")
        for w in risk.get("warnings", []):
            print(f"   ⚠️ {w}")
    except Exception as e:
        print(f"\n⚠️ [风控检查异常] {e}")
        cb = {}
    
    # 1.6 仓位再平衡：单只>max_position_pct自动减仓至10%
    rebalance_trades = []
    try:
        total_val = account.get("total_value", 1000000)
        max_single_pct = TRADING_RULES.get("max_position_pct", 0.12)
        target_pct = 0.10  # 减仓目标：10%
        realtime_rb = fetch_realtime_sina([h["code"] for h in account.get("holdings", [])])
        for h in account.get("holdings", []):
            rt = realtime_rb.get(h["code"], {})
            price = rt.get("price", h.get("current_price", h["cost_price"]))
            if price <= 0:
                continue
            holding_value = h["quantity"] * price
            weight = holding_value / total_val if total_val > 0 else 0
            if weight > max_single_pct:
                target_value = total_val * target_pct
                excess_value = holding_value - target_value
                sell_qty = int(excess_value / price / 100) * 100
                sellable = can_sell_today(account, h["code"])
                sell_qty = min(sell_qty, sellable)
                if sell_qty >= 100:
                    print(f"\n⚖️ [仓位再平衡] {h['name']}({h['code']}) 占比{weight*100:.1f}%>{max_single_pct*100:.0f}%，减{sell_qty}股至~{target_pct*100:.0f}%")
                    result = execute_trade(account, {
                        "code": h["code"],
                        "name": h["name"],
                        "price": price,
                        "trade_type": "sell",
                        "quantity": sell_qty,
                        "reasons": [f"仓位再平衡: {weight*100:.1f}%>{max_single_pct*100:.0f}%，减至{target_pct*100:.0f}%"]
                    })
                    if result["success"]:
                        rebalance_trades.append(result["trade"])
                        account = load_account()
                    else:
                        print(f"   ⚠️ 再平衡未执行: {result['reason']}")
    except Exception as e:
        print(f"\n⚠️ [仓位再平衡异常] {e}")

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
            # 熔断时跳过买入
            if cb.get("triggered") and d.get("trade_type") == "buy":
                print(f"   🚫 熔断中，跳过买入")
                continue
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
    
    # 6.5 残仓+无效仓位自动清理
    # 残仓: <0.5%总资产且<=300股 → 立即清理
    # 无效仓位: <3%总资产 → 立即清理（复盘9次提出，终于入码！）
    residual_threshold = TRADING_RULES.get("residual_clear_threshold_pct", 0.005)
    ineffective_threshold = TRADING_RULES.get("ineffective_position_pct", 0.03)
    total_val = account.get("total_value", 1000000)
    for h in list(account.get("holdings", [])):
        rt_price = realtime.get(h["code"], {}).get("price", h.get("current_price", h["cost_price"]))
        h_value = h["quantity"] * rt_price
        weight = h_value / total_val if total_val > 0 else 0
        
        # 残仓清理（<0.5%且<=300股）
        is_residual = weight < residual_threshold and h["quantity"] <= 300
        # 无效仓位清理（<3%总资产）
        is_ineffective = weight < ineffective_threshold and not is_residual
        
        if is_residual or is_ineffective:
            sellable = can_sell_today(account, h["code"])
            if sellable > 0:
                label = "残仓" if is_residual else "无效仓位"
                print(f"\n🧹 [{label}清理] {h['name']}({h['code']}) {h['quantity']}股 市值¥{h_value:.0f} (占比{weight*100:.1f}%<{(residual_threshold if is_residual else ineffective_threshold)*100:.1f}%)")
                result = execute_trade(account, {
                    "code": h["code"],
                    "name": h["name"],
                    "price": rt_price,
                    "trade_type": "sell",
                    "quantity": sellable,
                    "reasons": [f"{label}自动清理: {h['quantity']}股 市值¥{h_value:.0f} (占比{weight*100:.1f}%<{(residual_threshold if is_residual else ineffective_threshold)*100:.1f}%)"]
                })
                if result["success"]:
                    trades_executed.append(result["trade"])
                    account = load_account()
                    print(f"   ✅ 已清理")
                else:
                    print(f"   ⚠️ 清理失败: {result['reason']}")

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
    
    all_trades = rebalance_trades + trades_executed
    print(f"\n本次交易: {len(all_trades)}笔 (再平衡{len(rebalance_trades)}笔 + 常规{len(trades_executed)}笔)")
    print('='*60)
    
    return {
        "timestamp": datetime.now().isoformat(),
        "account": account,
        "trades": all_trades,
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
                # === v3: 仓位硬阻断 ===
                total_val = account.get("total_value", 1000000)
                cash_now = get_available_cash(account)
                pos_pct = 1 - (cash_now / total_val) if total_val > 0 else 1
                max_total = TRADING_RULES.get("max_total_position", 0.50)
                if pos_pct >= max_total:
                    print(f"  ⛔ 仓位硬阻断: {fs['name']} 当前仓位{pos_pct*100:.0f}%>={max_total*100:.0f}%")
                    continue
                
                available_cash = cash_now
                if available_cash > TRADING_RULES["min_buy_amount"]:
                    first_buy_max = TRADING_RULES.get("first_buy_max_pct", 0.07)
                    min_pos = TRADING_RULES.get("min_position_pct", 0.05)
                    min_amount = total_val * min_pos
                    max_amount = min(
                        available_cash * 0.25,
                        total_val * first_buy_max,  # 首笔上限7%
                        total_val * (max_total - pos_pct)  # v3: 不超仓位上限
                    )
                    quantity = int(max_amount / fs["price"] / 100) * 100
                    if quantity >= 100:
                        actual_amount = quantity * fs["price"]
                        if actual_amount < min_amount:
                            print(f"  ⛔ 最小仓位过滤: {fs['name']} ¥{actual_amount:.0f}<{min_pos*100:.0f}%总资产")
                            continue
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
