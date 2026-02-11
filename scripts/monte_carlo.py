#!/usr/bin/env python3
"""
蒙特卡洛模拟模块 — 对交易系统的历史交易序列做随机重排模拟，评估策略的稳健性。

原理：
  1. 从 transactions.json 中提取每笔卖出交易的收益率（pnl / amount）
  2. 对这些收益率做 N 次随机重排（bootstrap），每次生成一条资金曲线
  3. 统计模拟结果的分布，与实际表现对比，评估策略是否依赖特定交易顺序
"""

import json
import numpy as np
from pathlib import Path


def run_monte_carlo(
    transactions_file: str = None,
    n_simulations: int = 1000,
    initial_capital: float = 1_000_000,
) -> dict:
    """
    蒙特卡洛随机重排模拟。

    Parameters
    ----------
    transactions_file : str
        transactions.json 路径，默认自动查找 ../transactions.json
    n_simulations : int
        模拟次数，默认 1000
    initial_capital : float
        初始资金，默认 100万

    Returns
    -------
    dict  模拟结果摘要
    """
    try:
        # ---------- 1. 读取交易数据 ----------
        if transactions_file is None:
            base_dir = Path(__file__).parent.parent
            transactions_file = str(base_dir / "transactions.json")

        with open(transactions_file, "r", encoding="utf-8") as f:
            transactions = json.load(f)

        if not transactions:
            return _default_result("无交易数据")

        # ---------- 2. 提取每笔交易的收益/亏损 ----------
        # 买入交易记录为负现金流（支出），卖出记录为正现金流（收入+pnl）
        # 我们关注的是每笔完整交易的 pnl
        trade_pnls = []
        trade_amounts = []  # 对应的交易金额（用于计算收益率）

        for t in transactions:
            if t.get("type") == "sell" and "pnl" in t:
                pnl = float(t["pnl"])
                amount = float(t.get("amount", 0))
                if amount > 0:
                    trade_pnls.append(pnl)
                    trade_amounts.append(amount)

        if len(trade_pnls) < 2:
            return _default_result(f"有效卖出交易不足（仅{len(trade_pnls)}笔），无法模拟")

        trade_pnls = np.array(trade_pnls)
        trade_amounts = np.array(trade_amounts)
        n_trades = len(trade_pnls)

        # 每笔交易的收益率 = pnl / 交易金额
        trade_returns = trade_pnls / trade_amounts

        # ---------- 3. 计算实际表现 ----------
        actual_equity = _simulate_equity_curve(trade_pnls, initial_capital)
        actual_final = actual_equity[-1]
        actual_return_pct = (actual_final / initial_capital - 1) * 100
        actual_max_dd = _max_drawdown(actual_equity)

        # ---------- 4. 蒙特卡洛随机重排 ----------
        rng = np.random.default_rng(42)
        sim_final_returns = np.empty(n_simulations)
        sim_max_drawdowns = np.empty(n_simulations)
        sim_sharpes = np.empty(n_simulations)

        for i in range(n_simulations):
            # 随机重排交易顺序
            idx = rng.permutation(n_trades)
            shuffled_pnls = trade_pnls[idx]
            shuffled_returns = trade_returns[idx]

            # 模拟资金曲线
            equity = _simulate_equity_curve(shuffled_pnls, initial_capital)
            final_val = equity[-1]

            sim_final_returns[i] = (final_val / initial_capital - 1) * 100
            sim_max_drawdowns[i] = _max_drawdown(equity)

            # 简化 Sharpe（基于交易收益率序列）
            if len(shuffled_returns) > 1 and np.std(shuffled_returns) > 1e-10:
                sim_sharpes[i] = (
                    np.mean(shuffled_returns) / np.std(shuffled_returns) * np.sqrt(252)
                )
            else:
                sim_sharpes[i] = 0.0

        # ---------- 5. 统计结果 ----------
        p5_ret = float(np.percentile(sim_final_returns, 5))
        p95_ret = float(np.percentile(sim_final_returns, 95))
        median_ret = float(np.median(sim_final_returns))
        prob_positive = float(np.mean(sim_final_returns > 0))
        prob_beat_actual = float(np.mean(sim_final_returns > actual_return_pct))

        dd_median = float(np.median(sim_max_drawdowns))
        dd_p95 = float(np.percentile(sim_max_drawdowns, 95))

        sharpe_median = float(np.median(sim_sharpes))
        sharpe_p5 = float(np.percentile(sim_sharpes, 5))
        sharpe_p95 = float(np.percentile(sim_sharpes, 95))

        is_robust = p5_ret > 0 and sharpe_p5 > 0.5

        # 生成摘要
        if is_robust:
            summary = (
                f"策略稳健：95%置信区间收益为[{p5_ret:.1f}%, {p95_ret:.1f}%]，"
                f"正收益概率{prob_positive*100:.0f}%"
            )
        elif prob_positive > 0.5:
            summary = (
                f"策略一般：正收益概率{prob_positive*100:.0f}%，"
                f"但最差情况收益{p5_ret:.1f}%，存在亏损风险"
            )
        else:
            summary = (
                f"策略较弱：正收益概率仅{prob_positive*100:.0f}%，"
                f"95%置信区间收益为[{p5_ret:.1f}%, {p95_ret:.1f}%]"
            )

        return {
            "n_simulations": n_simulations,
            "n_trades": n_trades,
            "actual_return_pct": round(actual_return_pct, 2),
            "median_return_pct": round(median_ret, 2),
            "p5_return_pct": round(p5_ret, 2),
            "p95_return_pct": round(p95_ret, 2),
            "prob_positive": round(prob_positive, 4),
            "prob_beat_actual": round(prob_beat_actual, 4),
            "max_drawdown_median": round(dd_median, 4),
            "max_drawdown_p95": round(dd_p95, 4),
            "sharpe_median": round(sharpe_median, 2),
            "sharpe_p5": round(sharpe_p5, 2),
            "sharpe_p95": round(sharpe_p95, 2),
            "is_robust": is_robust,
            "summary": summary,
        }

    except Exception as e:
        return _default_result(f"蒙特卡洛模拟出错: {e}")


def _simulate_equity_curve(pnls: np.ndarray, initial_capital: float) -> np.ndarray:
    """根据一系列 pnl 生成资金曲线。"""
    equity = np.empty(len(pnls) + 1)
    equity[0] = initial_capital
    for i, pnl in enumerate(pnls):
        equity[i + 1] = equity[i] + pnl
    return equity


def _max_drawdown(equity: np.ndarray) -> float:
    """计算资金曲线的最大回撤（比例）。"""
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, 1)
    return float(np.max(dd))


def _default_result(reason: str) -> dict:
    """返回默认结果（数据不足时）。"""
    return {
        "n_simulations": 0,
        "n_trades": 0,
        "actual_return_pct": 0.0,
        "median_return_pct": 0.0,
        "p5_return_pct": 0.0,
        "p95_return_pct": 0.0,
        "prob_positive": 0.0,
        "prob_beat_actual": 0.0,
        "max_drawdown_median": 0.0,
        "max_drawdown_p95": 0.0,
        "sharpe_median": 0.0,
        "sharpe_p5": 0.0,
        "sharpe_p95": 0.0,
        "is_robust": False,
        "summary": reason,
    }


if __name__ == "__main__":
    import pprint

    print("=" * 60)
    print("蒙特卡洛模拟 — 交易策略稳健性评估")
    print("=" * 60)

    result = run_monte_carlo()
    pprint.pprint(result, width=80)
