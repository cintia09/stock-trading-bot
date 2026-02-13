#!/usr/bin/env python3
"""cb_trading_engine.py

可转债套利模拟交易引擎（纯脚本自动执行）：
- 读取 cb_scanner 扫描到的机会（bond_price / premium_rate / can_convert / score / strategy）
- 根据规则自动买入 / 卖出 / 模拟转股
- 更新 stock-trading/account.json 的 cb_holdings 与 current_cash
- 交易记录追加到 stock-trading/transactions.json（asset_type: "cb"）

注意：
- account.json 若缺少 cb_holdings 字段，必须 setdefault。
- 所有网络请求由 cb_scanner 负责；本模块只做计算与写盘。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# cb_scanner 内部已做 timeout/try-except，本引擎再做一层兜底
from cb_scanner import fetch_cb_list, scan  # type: ignore

BASE_DIR = Path(__file__).parent.parent
ACCOUNT_FILE = BASE_DIR / "account.json"
TRANSACTIONS_FILE = BASE_DIR / "transactions.json"


# ------------------------- json helpers -------------------------

def _safe_load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------- risk / sizing -------------------------

@dataclass
class CBPositionRules:
    max_cb_total_pct: float = 0.20   # 可转债总仓位 <= 总资产 20%
    max_single_pct: float = 0.05     # 单只转债 <= 总资产 5%
    min_buy_amount: float = 10_000.0
    max_buy_amount: float = 50_000.0


def _get_total_assets(account: Dict[str, Any]) -> float:
    """尽量用 account['total_value']；否则自己计算现金+股票/转债市值。"""
    tv = account.get("total_value")
    try:
        tvf = float(tv)
        if tvf > 0:
            return tvf
    except Exception:
        pass

    cash = float(account.get("current_cash", 0) or 0)
    stock_mv = 0.0
    for h in account.get("holdings", []) or []:
        try:
            stock_mv += float(h.get("market_value", 0) or 0)
        except Exception:
            pass

    cb_mv = 0.0
    for h in account.get("cb_holdings", []) or []:
        try:
            cb_mv += float(h.get("market_value", 0) or 0)
        except Exception:
            pass

    return cash + stock_mv + cb_mv


def _get_cb_market_value(account: Dict[str, Any]) -> float:
    mv = 0.0
    for h in account.get("cb_holdings", []) or []:
        try:
            mv += float(h.get("market_value", 0) or 0)
        except Exception:
            pass
    return mv


def _get_cb_holding(account: Dict[str, Any], bond_code: str) -> Optional[Dict[str, Any]]:
    for h in account.get("cb_holdings", []) or []:
        if str(h.get("bond_code") or "").strip() == str(bond_code).strip():
            return h
    return None


def _buy_amount_by_score(score: float, rules: CBPositionRules) -> float:
    """按评分将单次买入金额映射到 1-5万。"""
    try:
        s = float(score)
    except Exception:
        s = 0.0

    s = max(0.0, min(100.0, s))
    amt = rules.min_buy_amount + (rules.max_buy_amount - rules.min_buy_amount) * (s / 100.0)
    # 保留整数元
    return float(int(amt))


# ------------------------- trading execution -------------------------

def execute_cb_trade(
    account: Dict[str, Any],
    action: str,
    bond_code: str,
    bond_name: str,
    quantity: int,
    price: float,
    strategy: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """执行可转债模拟交易

    action: "buy" / "sell" / "convert"（转股）
    - 更新 account.json 的 cb_holdings 和 current_cash
    - 追加到 transactions.json（asset_type: "cb"）
    """

    account.setdefault("cb_holdings", [])

    bond_code = str(bond_code).strip()
    bond_name = str(bond_name or bond_code).strip()

    try:
        qty = int(quantity)
    except Exception:
        qty = 0

    try:
        px = float(price)
    except Exception:
        px = 0.0

    if qty <= 0 or px <= 0:
        return {"success": False, "reason": "invalid quantity/price"}

    amount = round(qty * px, 4)

    # 可转债手续费/税费：这里用 0（简单模拟），需要可再加
    fees = float(kwargs.get("fees") or 0.0)

    tx = {
        "trade_id": f"{datetime.now().strftime('%Y%m%d%H%M%S')}_CB_{bond_code}",
        "asset_type": "cb",
        "bond_code": bond_code,
        "bond_name": bond_name,
        "type": action,
        "price": px,
        "quantity": qty,
        "amount": amount,
        "fees": fees,
        "timestamp": _now_iso(),
        "strategy": strategy,
    }

    # 附加信息（溢价率、转股价、正股等）
    for k in [
        "premium_rate",
        "can_convert",
        "score",
        "target_stock_code",
        "transfer_price",
        "stock_price",
        "convert_value",
    ]:
        if k in kwargs:
            tx[k] = kwargs[k]

    if action == "buy":
        total_cost = amount + fees
        cash = float(account.get("current_cash", 0) or 0)
        if total_cost > cash:
            return {"success": False, "reason": "cash not enough"}

        account["current_cash"] = cash - total_cost

        h = _get_cb_holding(account, bond_code)
        if h:
            # 加仓：按金额加权成本
            old_shares = float(h.get("shares", 0) or 0)
            old_cost = float(h.get("cost_price", 0) or 0)
            old_total = old_shares * old_cost
            new_shares = old_shares + qty
            new_cost = (old_total + amount + fees) / new_shares
            h["shares"] = int(new_shares)
            h["cost_price"] = round(float(new_cost), 4)
            # 仍保留最早买入时间
        else:
            holding = {
                "bond_code": bond_code,
                "bond_name": bond_name,
                "shares": qty,
                "cost_price": round((amount + fees) / qty, 4),
                "buy_time": _now_iso(),
                "strategy": strategy,
                "target_stock_code": kwargs.get("target_stock_code"),
                "transfer_price": kwargs.get("transfer_price"),
                "current_price": px,
                "market_value": round(amount, 2),
                "pnl_pct": 0.0,
            }
            account["cb_holdings"].append(holding)

        tx["net_amount"] = -total_cost

    elif action in ("sell", "convert"):
        h = _get_cb_holding(account, bond_code)
        if not h:
            return {"success": False, "reason": "no holding"}

        try:
            hold_shares = int(h.get("shares", 0) or 0)
        except Exception:
            hold_shares = 0

        if qty > hold_shares:
            qty = hold_shares
            tx["quantity"] = qty
            amount = round(qty * px, 4)
            tx["amount"] = amount

        if qty <= 0:
            return {"success": False, "reason": "no shares"}

        cash = float(account.get("current_cash", 0) or 0)
        net_receive = amount - fees
        account["current_cash"] = cash + net_receive

        # pnl
        try:
            cost_price = float(h.get("cost_price", 0) or 0)
        except Exception:
            cost_price = 0.0
        tx["pnl"] = round((px - cost_price) * qty - fees, 2)

        # 减仓 / 清仓
        remaining = hold_shares - qty
        if remaining <= 0:
            account["cb_holdings"] = [x for x in account.get("cb_holdings", []) if str(x.get("bond_code")) != bond_code]
        else:
            h["shares"] = remaining

        tx["net_amount"] = net_receive

        # 转股：额外标记
        if action == "convert":
            tx["convert_note"] = "模拟转股：按转股价值记录收益，现金侧按卖出转债处理"

    else:
        return {"success": False, "reason": f"unknown action {action}"}

    # append transactions
    txns = _safe_load_json(TRANSACTIONS_FILE, [])
    if not isinstance(txns, list):
        txns = []
    txns.append(tx)
    _safe_write_json(TRANSACTIONS_FILE, txns)

    # update account total_value (粗略)
    account["last_updated"] = _now_iso()
    _safe_write_json(ACCOUNT_FILE, account)

    return {"success": True, "trade": tx}


# ------------------------- decision logic -------------------------


def should_buy(op: Dict[str, Any]) -> bool:
    """按需求规则判定是否满足买入条件。"""
    try:
        premium = float(op.get("premium_rate"))
    except Exception:
        premium = 999.0
    try:
        price = float(op.get("bond_price"))
    except Exception:
        price = 0.0
    can_convert = bool(op.get("can_convert"))

    # 负溢价转股套利
    if premium < -2 and can_convert:
        return True
    # 低溢价套利
    if premium < 0 and can_convert:
        return True
    # 深度折价
    if price < 90 and premium < 30:
        return True
    return False


def should_sell_or_convert(
    holding: Dict[str, Any],
    realtime_op: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], str]:
    """返回 (action, reason)；action in {"sell","convert",None}"""

    now = now or datetime.now()

    strategy = str(holding.get("strategy") or "").strip()

    # 价格与溢价率
    try:
        cur_price = float(holding.get("current_price") or 0)
    except Exception:
        cur_price = 0.0

    premium = None
    can_convert = None
    convert_value = None
    if realtime_op:
        try:
            premium = float(realtime_op.get("premium_rate"))
        except Exception:
            premium = None
        can_convert = bool(realtime_op.get("can_convert"))
        try:
            convert_value = float(realtime_op.get("convert_value"))
        except Exception:
            convert_value = None

        try:
            cur_price = float(realtime_op.get("bond_price"))
        except Exception:
            pass

    # 止损：任何转债亏损 > 3% -> 卖出
    try:
        cost = float(holding.get("cost_price") or 0)
    except Exception:
        cost = 0.0

    pnl_pct = ((cur_price - cost) / cost * 100.0) if cost > 0 else 0.0
    if pnl_pct <= -3.0:
        return "sell", f"止损: pnl {pnl_pct:.2f}%"

    # 负溢价套利：溢价率回到 > 0% -> 卖出
    if strategy == "负溢价转股套利" and premium is not None:
        if premium > 0:
            return "sell", f"溢价回正({premium:.2f}%)套利消失"

        # T+1 可转股，且仍负溢价 -> convert
        if premium < 0 and can_convert:
            buy_time_raw = holding.get("buy_time")
            try:
                buy_dt = datetime.fromisoformat(str(buy_time_raw))
            except Exception:
                buy_dt = None
            if buy_dt and now.date() >= (buy_dt + timedelta(days=1)).date():
                return "convert", f"T+1仍负溢价({premium:.2f}%) 模拟转股"

    # 低价低溢价：价格>105 或 溢价>10% -> 卖出
    if strategy == "低价低溢价":
        if cur_price > 105:
            return "sell", f"价格>105({cur_price:.2f})"
        if premium is not None and premium > 10:
            return "sell", f"溢价>10%({premium:.2f}%)"

    # 深度折价：价格回到 >100 -> 卖出
    if strategy == "深度折价" and cur_price > 100:
        return "sell", f"回归面值>100({cur_price:.2f})"

    return None, ""


def process_cb_trading(
    account: Dict[str, Any],
    opportunities: List[Dict[str, Any]],
    rules: Optional[CBPositionRules] = None,
) -> List[Dict[str, Any]]:
    """对现有持仓做卖出/转股检查，并对新机会尝试买入。

    返回：已执行交易记录列表（trade dict）。
    """

    rules = rules or CBPositionRules()
    account.setdefault("cb_holdings", [])

    # 建立机会索引（包括已持仓标的可能被扫描器过滤掉的情况：这里用 bond_code 直接匹配）
    opp_by_code: Dict[str, Dict[str, Any]] = {}
    for op in opportunities or []:
        if isinstance(op, dict) and op.get("bond_code"):
            opp_by_code[str(op["bond_code"]).strip()] = op

    executed: List[Dict[str, Any]] = []

    # 1) 卖出/转股
    for h in list(account.get("cb_holdings", []) or []):
        bond_code = str(h.get("bond_code") or "").strip()
        op = opp_by_code.get(bond_code)
        action, reason = should_sell_or_convert(h, op)
        if not action:
            continue

        try:
            px = float(op.get("bond_price")) if op else float(h.get("current_price") or 0)
        except Exception:
            px = float(h.get("current_price") or 0)

        try:
            qty = int(h.get("shares", 0) or 0)
        except Exception:
            qty = 0

        if qty <= 0 or px <= 0:
            continue

        res = execute_cb_trade(
            account,
            action=action,
            bond_code=bond_code,
            bond_name=str(h.get("bond_name") or bond_code),
            quantity=qty,
            price=px,
            strategy=str(h.get("strategy") or ""),
            reason=reason,
            premium_rate=(op.get("premium_rate") if op else None),
            can_convert=(op.get("can_convert") if op else None),
            target_stock_code=(h.get("target_stock_code") or (op.get("stock_code") if op else None)),
            transfer_price=(h.get("transfer_price") or (op.get("transfer_price") if op else None)),
            stock_price=(op.get("stock_price") if op else None),
            convert_value=(op.get("convert_value") if op else None),
        )

        if res.get("success") and res.get("trade"):
            trade = res["trade"]
            trade["reason"] = reason
            executed.append(trade)

            # execute_cb_trade 会写盘，但 account 是同一个 dict，这里确保 cb_holdings 已刷新
            account = _safe_load_json(ACCOUNT_FILE, account)

    # 2) 买入：按 score 从高到低，依次尝试
    total_assets = _get_total_assets(account)
    cb_mv = _get_cb_market_value(account)

    # 剩余可用转债仓位
    cb_budget_left = max(0.0, total_assets * rules.max_cb_total_pct - cb_mv)

    cash = float(account.get("current_cash", 0) or 0)

    opps_sorted = sorted([op for op in (opportunities or []) if isinstance(op, dict)], key=lambda x: float(x.get("score", 0) or 0), reverse=True)

    for op in opps_sorted:
        bond_code = str(op.get("bond_code") or "").strip()
        if not bond_code:
            continue

        if _get_cb_holding(account, bond_code):
            continue

        if not should_buy(op):
            continue

        try:
            px = float(op.get("bond_price") or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            continue

        # 单只上限
        single_limit_left = max(0.0, total_assets * rules.max_single_pct)

        # 计算本次拟买金额（1-5万按评分）
        amount = _buy_amount_by_score(float(op.get("score", 0) or 0), rules)

        # 约束：不超过剩余可转债预算 / 不超过单只上限 / 不超过现金
        amount = min(amount, cb_budget_left, single_limit_left, cash)

        # 最小买入金额
        if amount < rules.min_buy_amount:
            continue

        qty = int(amount / px)
        if qty <= 0:
            continue

        # 可转债通常 10 张一手，这里做个保守对齐（避免 1 张碎单）
        qty = (qty // 10) * 10
        if qty <= 0:
            continue

        res = execute_cb_trade(
            account,
            action="buy",
            bond_code=bond_code,
            bond_name=str(op.get("bond_name") or bond_code),
            quantity=qty,
            price=px,
            strategy=str(op.get("strategy") or ""),
            premium_rate=op.get("premium_rate"),
            can_convert=op.get("can_convert"),
            score=op.get("score"),
            target_stock_code=op.get("stock_code"),
            transfer_price=op.get("transfer_price"),
            stock_price=op.get("stock_price"),
            convert_value=op.get("convert_value"),
        )
        if res.get("success") and res.get("trade"):
            trade = res["trade"]
            executed.append(trade)

            # 更新预算/现金
            cash = float(account.get("current_cash", 0) or 0)
            cb_mv = _get_cb_market_value(account)
            cb_budget_left = max(0.0, total_assets * rules.max_cb_total_pct - cb_mv)

    return executed


def run_full_cb_scan_and_trade() -> List[Dict[str, Any]]:
    """方便外部调用：全量扫描一次并执行交易。"""
    account = _safe_load_json(ACCOUNT_FILE, {})
    if not isinstance(account, dict):
        account = {}
    account.setdefault("cb_holdings", [])

    cb_list = []
    opportunities: List[Dict[str, Any]] = []
    try:
        cb_list = fetch_cb_list()
        opportunities = scan(cb_list) if cb_list else []
    except Exception:
        opportunities = []

    return process_cb_trading(account, opportunities)
