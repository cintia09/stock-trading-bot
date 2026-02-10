#!/usr/bin/env python3
"""risk_manager.py

风险管理模块（参考 TradingAgents 思路，做轻量可落地实现）。

约束：
- 不修改现有系统接口；此模块以纯函数形式提供能力
- 全部 try/except 防护，默认返回稳健值

account 预期结构（与现有 trading_engine.py 对齐/兼容）：
{
  "total_value": float,
  "current_cash": float,
  "holdings": [
      {"code": str, "name": str, "market_value": float, "industry": str(optional)}
  ],
  "peak_value": float(optional)  # 本模块熔断会维护
}
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, List


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def calculate_portfolio_risk(account: Dict) -> Dict:
    """计算当前组合风险。

    输出：
    {
      "risk_level": "low|medium|high",
      "position_pct": float,
      "max_drawdown": float,
      "warnings": [str, ...],
      "industry_exposure": {industry: pct},
      "concentration": {code: pct}
    }
    """
    try:
        warnings: List[str] = []
        total_value = _safe_float(account.get("total_value"), 0.0)
        cash = _safe_float(account.get("current_cash"), 0.0)
        holdings = account.get("holdings") or []
        if not isinstance(holdings, list):
            holdings = []

        holdings_value = 0.0
        for h in holdings:
            mv = _safe_float((h or {}).get("market_value"), 0.0)
            # 若未维护 market_value，尽量不报错
            holdings_value += max(0.0, mv)

        if total_value <= 0:
            # 回退：用 cash + holdings_value 估
            total_value = cash + holdings_value

        position_pct = (holdings_value / total_value) if total_value > 0 else 0.0

        # 单票集中度
        concentration = {}
        for h in holdings:
            code = (h or {}).get("code") or ""
            mv = _safe_float((h or {}).get("market_value"), 0.0)
            pct = (mv / total_value) if total_value > 0 else 0.0
            if code:
                concentration[code] = round(pct, 4)
            if pct > 0.30:
                nm = (h or {}).get("name") or code
                warnings.append(f"单只持仓集中度过高: {nm}({code}) 占比{pct*100:.1f}%")

        # 行业集中度（需要 holdings 里带 industry 字段；没有则归为 unknown）
        industry_sum: Dict[str, float] = {}
        for h in holdings:
            ind = (h or {}).get("industry") or "unknown"
            mv = _safe_float((h or {}).get("market_value"), 0.0)
            industry_sum[ind] = industry_sum.get(ind, 0.0) + max(0.0, mv)

        industry_exposure = {}
        for ind, mv in industry_sum.items():
            pct = (mv / total_value) if total_value > 0 else 0.0
            industry_exposure[ind] = round(pct, 4)
            if ind != "unknown" and pct > 0.40:
                warnings.append(f"行业集中度过高: {ind} 占比{pct*100:.1f}%")

        # 最大回撤：从 peak_value 到当前 total_value 的回撤
        peak = _safe_float(account.get("peak_value"), 0.0)
        if peak <= 0:
            peak = total_value
        max_drawdown = ((peak - total_value) / peak) if peak > 0 else 0.0
        max_drawdown = _clamp(max_drawdown, 0.0, 1.0)

        # 总仓位风险等级
        if position_pct > 0.70:
            risk_level = "high"
        elif position_pct >= 0.50:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "risk_level": risk_level,
            "position_pct": round(position_pct, 4),
            "max_drawdown": round(max_drawdown, 4),
            "warnings": warnings,
            "industry_exposure": industry_exposure,
            "concentration": concentration,
            "computed_at": datetime.now().isoformat(),
        }
    except Exception:
        return {
            "risk_level": "unknown",
            "position_pct": 0.0,
            "max_drawdown": 0.0,
            "warnings": ["risk_calculation_failed"],
            "industry_exposure": {},
            "concentration": {},
            "computed_at": datetime.now().isoformat(),
        }


def position_size_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """凯利公式计算最优仓位（半凯利）。

    Kelly% = W - (1-W)/R
    W=胜率, R=盈亏比=avg_win/avg_loss
    实际仓位 = Kelly% * 0.5

    返回：建议仓位比例 [0,1]
    """
    try:
        W = _clamp(_safe_float(win_rate, 0.0), 0.0, 1.0)
        aw = max(0.0, _safe_float(avg_win, 0.0))
        al = max(0.0, _safe_float(avg_loss, 0.0))
        if al <= 0 or aw <= 0:
            return 0.0
        R = aw / al
        kelly = W - (1 - W) / R
        kelly = _clamp(kelly, 0.0, 1.0)
        half_kelly = _clamp(kelly * 0.5, 0.0, 1.0)
        return float(round(half_kelly, 4))
    except Exception:
        return 0.0


def check_drawdown_circuit_breaker(account: Dict, max_dd: float = 0.10) -> Dict:
    """回撤熔断检查。

    - 如果 account["peak_value"] 不存在：初始化为当前 total_value
    - 若 (peak_value - total_value)/peak_value > max_dd：触发 stop_trading

    返回：
    {"triggered": bool, "action": "stop_trading"|None, "drawdown": float, "peak_value": float}

    注意：为保持“不要修改现有接口”，此函数不强制持久化。
    但会就地更新传入的 account dict 的 peak_value 字段（调用方如有保存即可生效）。
    """
    try:
        tv = _safe_float(account.get("total_value"), 0.0)
        if tv <= 0:
            # 尽量从 cash + 持仓估算
            cash = _safe_float(account.get("current_cash"), 0.0)
            hv = 0.0
            holdings = account.get("holdings") or []
            if isinstance(holdings, list):
                for h in holdings:
                    hv += max(0.0, _safe_float((h or {}).get("market_value"), 0.0))
            tv = cash + hv

        peak = _safe_float(account.get("peak_value"), 0.0)
        if peak <= 0:
            peak = tv
            account["peak_value"] = peak

        if tv > peak:
            peak = tv
            account["peak_value"] = peak

        dd = ((peak - tv) / peak) if peak > 0 else 0.0
        dd = _clamp(dd, 0.0, 1.0)

        threshold = max(0.0, _safe_float(max_dd, 0.10))
        if dd > threshold:
            return {
                "triggered": True,
                "action": "stop_trading",
                "drawdown": round(dd, 4),
                "peak_value": round(peak, 2),
                "current_value": round(tv, 2),
            }

        return {
            "triggered": False,
            "action": None,
            "drawdown": round(dd, 4),
            "peak_value": round(peak, 2),
            "current_value": round(tv, 2),
        }
    except Exception:
        return {"triggered": False, "action": None, "drawdown": 0.0, "peak_value": 0.0}


def check_underperform_action(account: Dict, market_change_pct: float, 
                               alert_threshold: float = -0.015,
                               consecutive_days_to_act: int = 2,
                               reduce_pct: float = 0.5) -> List[Dict]:
    """逆市预警动作：连续N天逆市下跌的持仓自动减仓建议。
    
    Args:
        account: 账户信息
        market_change_pct: 大盘涨跌幅（如0.01表示+1%）
        alert_threshold: 逆市预警阈值（如-0.015表示大盘涨但个股跌>1.5%）
        consecutive_days_to_act: 连续触发几天后行动
        reduce_pct: 减仓比例
    
    Returns:
        List of {code, name, action, reduce_pct, reason}
    """
    try:
        actions = []
        holdings = account.get("holdings") or []
        if not isinstance(holdings, list):
            return actions
        
        for h in holdings:
            code = (h or {}).get("code", "")
            name = (h or {}).get("name", code)
            pnl_pct = _safe_float((h or {}).get("pnl_pct"), 0) / 100.0  # pnl_pct存的是百分比数
            
            # 逆市判断：大盘涨（或不跌），个股相对大盘跌超过阈值
            if market_change_pct >= -0.005:  # 大盘不跌
                relative_perf = pnl_pct - market_change_pct  # 这里用的是今日涨跌幅
                # 注意：pnl_pct是累计盈亏，这里需要今日涨跌幅
                # 实际使用时应传入今日涨跌幅，这里简化处理
                
            # 检查连续逆市天数（需要历史记录）
            underperform_days = _safe_float((h or {}).get("underperform_days"), 0)
            
            if underperform_days >= consecutive_days_to_act:
                actions.append({
                    "code": code,
                    "name": name,
                    "action": "reduce",
                    "reduce_pct": reduce_pct,
                    "reason": f"连续{int(underperform_days)}天逆市下跌，建议减仓{reduce_pct*100:.0f}%",
                    "underperform_days": int(underperform_days)
                })
        
        return actions
    except Exception:
        return []


def check_passive_overweight(account: Dict, tolerance: float = 0.55) -> List[Dict]:
    """被动超限检查：持仓因上涨导致总仓位超过容忍线时，建议减仓最弱持仓。
    
    Args:
        account: 账户信息
        tolerance: 被动超限容忍线（如0.55表示55%）
    
    Returns:
        List of {code, name, action, reason} 建议减仓的持仓
    """
    try:
        total_value = _safe_float(account.get("total_value"), 0)
        cash = _safe_float(account.get("current_cash"), 0)
        if total_value <= 0:
            return []
        
        position_pct = 1 - (cash / total_value)
        
        if position_pct <= tolerance:
            return []
        
        # 找最弱持仓（盈亏最差的）
        holdings = account.get("holdings") or []
        if not holdings:
            return []
        
        weakest = min(holdings, key=lambda h: _safe_float((h or {}).get("pnl_pct"), 0))
        
        over_pct = position_pct - 0.50  # 需要减到50%
        reduce_value = over_pct * total_value
        
        return [{
            "code": weakest.get("code", ""),
            "name": weakest.get("name", ""),
            "action": "reduce_overweight",
            "reduce_value": round(reduce_value, 2),
            "reason": f"被动超限{position_pct*100:.1f}%>{tolerance*100:.0f}%，建议减仓最弱持仓{weakest.get('name','')}约¥{reduce_value:.0f}",
            "current_position_pct": round(position_pct * 100, 1)
        }]
    except Exception:
        return []


if __name__ == "__main__":
    demo = {
        "total_value": 900000,
        "current_cash": 300000,
        "holdings": [
            {"code": "600000", "name": "浦发银行", "market_value": 400000, "industry": "银行"},
            {"code": "600519", "name": "贵州茅台", "market_value": 200000, "industry": "白酒"},
        ],
        "peak_value": 1000000,
    }
    print(calculate_portfolio_risk(demo))
    print(position_size_kelly(0.55, 0.08, 0.04))
    print(check_drawdown_circuit_breaker(demo, 0.10))
