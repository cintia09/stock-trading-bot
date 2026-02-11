#!/usr/bin/env python3
"""
马尔可夫市场状态模型 — 判断当前大盘处于什么状态，不同状态建议不同的策略参数。

状态划分：
  - 牛市(bull): 20日均线 > 60日均线 + 近20日收益 > 0
  - 震荡(range): 均线纠缠 + 波动率适中
  - 熊市(bear): 20日均线 < 60日均线 + 近20日收益 < 0

可选：如果安装了 hmmlearn，使用隐马尔可夫模型做概率估计；否则纯规则判断。
"""

import sys
import numpy as np
from pathlib import Path
from datetime import datetime

# 确保能导入同目录下的模块
sys.path.insert(0, str(Path(__file__).parent))


# ===== 不同状态的策略参数建议 =====
REGIME_PARAMS = {
    "bull": {
        "max_total_position": 0.50,
        "stop_loss_factor": 1.0,
        "take_profit_factor": 1.2,
        "buy_score_threshold": 60,
    },
    "range": {
        "max_total_position": 0.40,
        "stop_loss_factor": 1.0,
        "take_profit_factor": 1.0,
        "buy_score_threshold": 65,
    },
    "bear": {
        "max_total_position": 0.30,
        "stop_loss_factor": 0.8,
        "take_profit_factor": 1.0,
        "buy_score_threshold": 75,
    },
}

REGIME_LABELS = {"bull": "牛市", "range": "震荡", "bear": "熊市"}


def detect_market_regime(
    index_code: str = "sh000001",
    lookback_days: int = 60,
) -> dict:
    """
    检测当前大盘所处的市场状态。

    Parameters
    ----------
    index_code : str
        指数代码，默认上证指数 "sh000001"
    lookback_days : int
        回溯天数，默认 60

    Returns
    -------
    dict  市场状态及策略建议
    """
    try:
        # ---------- 1. 获取K线数据 ----------
        closes, dates = _fetch_index_data(index_code, lookback_days)

        if closes is None or len(closes) < 60:
            return _default_result(
                f"K线数据不足（需要≥60根，实际{len(closes) if closes is not None else 0}根）"
            )

        # ---------- 2. 计算技术指标 ----------
        ma20 = _sma(closes, 20)
        ma60 = _sma(closes, 60)

        # 日收益率
        returns = np.diff(closes) / closes[:-1]
        recent_returns = returns[-20:]  # 近20日收益率

        # 近20日累计收益
        recent_cum_return = (closes[-1] / closes[-21] - 1) if len(closes) > 21 else 0.0

        # 近20日波动率（年化）
        recent_vol = float(np.std(recent_returns) * np.sqrt(252)) if len(recent_returns) > 1 else 0.0

        # 均线差距比率 = (MA20 - MA60) / MA60
        ma20_last = ma20[-1]
        ma60_last = ma60[-1]
        ma_spread = (ma20_last - ma60_last) / ma60_last if ma60_last > 0 else 0.0

        # ---------- 3. 尝试 HMM 概率估计 ----------
        hmm_result = _try_hmm(returns)

        # ---------- 4. 基于规则判断状态 ----------
        regime, confidence = _rule_based_regime(
            ma20_last, ma60_last, ma_spread, recent_cum_return, recent_vol, hmm_result
        )

        # ---------- 5. 计算状态持续天数 ----------
        duration = _calc_regime_duration(closes, ma20, ma60)

        # ---------- 6. 计算状态转移概率（基于历史） ----------
        transition_prob = _calc_transition_prob(closes, ma20, ma60, regime)

        # ---------- 7. 组装结果 ----------
        params = REGIME_PARAMS[regime]
        label = REGIME_LABELS[regime]

        summary = (
            f"当前{label}状态({confidence*100:.0f}%置信)，"
            f"持续{duration}天，"
        )
        if regime == "bull":
            summary += "建议维持正常仓位"
        elif regime == "range":
            summary += "建议适当降低仓位至40%"
        else:
            summary += "建议降低仓位至30%，收紧止损"

        return {
            "current_regime": regime,
            "confidence": round(confidence, 2),
            "regime_duration_days": duration,
            "transition_prob": {
                "to_bull": round(transition_prob.get("to_bull", 0.33), 2),
                "to_range": round(transition_prob.get("to_range", 0.34), 2),
                "to_bear": round(transition_prob.get("to_bear", 0.33), 2),
            },
            "suggested_adjustments": params,
            "indicators": {
                "ma20": round(ma20_last, 2),
                "ma60": round(ma60_last, 2),
                "ma_spread_pct": round(ma_spread * 100, 2),
                "recent_20d_return_pct": round(recent_cum_return * 100, 2),
                "recent_volatility_annual": round(recent_vol * 100, 2),
                "last_close": round(float(closes[-1]), 2),
                "date": dates[-1] if dates else "",
            },
            "summary": summary,
        }

    except Exception as e:
        return _default_result(f"市场状态检测出错: {e}")


def _fetch_index_data(index_code: str, lookback_days: int):
    """获取指数K线数据，返回 (closes_array, dates_list)。"""
    try:
        import requests
        import time

        # 指数代码映射到东方财富 secid
        # 上证指数系列: sh000001 → 1.000001
        # 深证指数系列: sz399001 → 0.399001
        INDEX_SECID_MAP = {
            "sh000001": "1.000001",  # 上证指数
            "sz399001": "0.399001",  # 深证成指
            "sz399006": "0.399006",  # 创业板指
            "sh000016": "1.000016",  # 上证50
            "sh000300": "1.000300",  # 沪深300
            "sh000905": "1.000905",  # 中证500
        }

        # 标准化代码
        code = index_code.lower()
        if code in INDEX_SECID_MAP:
            secid = INDEX_SECID_MAP[code]
        elif code.startswith("sh"):
            secid = f"1.{code[2:]}"
        elif code.startswith("sz"):
            secid = f"0.{code[2:]}"
        else:
            # 默认当上证处理
            secid = f"1.{code.replace('sh', '').replace('sz', '')}"

        # 直接调用东方财富API获取指数K线（绕过 fetch_kline 的股票代码映射）
        limit = max(lookback_days + 30, 120)
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "1",
            "lmt": limit,
            "end": "20500101",
            "_": int(time.time() * 1000),
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.eastmoney.com",
        }

        resp = requests.get(url, params=params, timeout=15, headers=headers)
        data = resp.json()

        if not data.get("data") or not data["data"].get("klines"):
            # 回退到 fetch_kline（可能是非标准代码）
            from fetch_stock_data import fetch_kline

            clean_code = code.replace("sh", "").replace("sz", "")
            klines = fetch_kline(clean_code, period="101", limit=limit)
            if not klines:
                return None, None
            closes = np.array([k["close"] for k in klines], dtype=float)
            dates = [k["date"] for k in klines]
            return closes, dates

        klines = []
        for line in data["data"]["klines"]:
            parts = line.split(",")
            klines.append({"date": parts[0], "close": float(parts[2])})

        closes = np.array([k["close"] for k in klines], dtype=float)
        dates = [k["date"] for k in klines]

        return closes, dates

    except Exception as e:
        print(f"获取指数数据失败: {e}")
        return None, None


def _sma(data: np.ndarray, window: int) -> np.ndarray:
    """简单移动平均线。"""
    if len(data) < window:
        return np.full_like(data, np.nan)
    cumsum = np.cumsum(data)
    cumsum[window:] = cumsum[window:] - cumsum[:-window]
    result = np.full_like(data, np.nan, dtype=float)
    result[window - 1 :] = cumsum[window - 1 :] / window
    return result


def _rule_based_regime(
    ma20: float,
    ma60: float,
    ma_spread: float,
    recent_return: float,
    recent_vol: float,
    hmm_result: dict = None,
) -> tuple:
    """
    基于规则的市场状态判断。

    Returns (regime, confidence)
    """
    # 如果 HMM 有结果，以 HMM 为主
    if hmm_result and hmm_result.get("regime") and hmm_result.get("confidence", 0) > 0.6:
        return hmm_result["regime"], hmm_result["confidence"]

    # ----- 纯规则判断 -----
    # 牛市: MA20 > MA60 且近20日正收益
    # 熊市: MA20 < MA60 且近20日负收益
    # 震荡: 其他

    # 均线差距阈值（0.5% 以内视为纠缠）
    ma_threshold = 0.005

    if ma_spread > ma_threshold and recent_return > 0:
        # 牛市信号
        # 置信度根据均线差距和收益率综合评估
        conf = min(0.95, 0.5 + abs(ma_spread) * 5 + recent_return * 2)
        return "bull", round(conf, 2)

    elif ma_spread < -ma_threshold and recent_return < 0:
        # 熊市信号
        conf = min(0.95, 0.5 + abs(ma_spread) * 5 + abs(recent_return) * 2)
        return "bear", round(conf, 2)

    else:
        # 震荡市
        # 越接近阈值，置信度越低
        conf = min(0.85, 0.5 + (ma_threshold - abs(ma_spread)) * 50)
        conf = max(0.4, conf)
        return "range", round(conf, 2)


def _try_hmm(returns: np.ndarray) -> dict:
    """
    尝试用隐马尔可夫模型(HMM)估计市场状态。
    如果 hmmlearn 未安装，返回空结果。
    """
    try:
        from hmmlearn.hmm import GaussianHMM

        if len(returns) < 30:
            return {}

        # 准备特征：收益率 + 波动率（滚动5日标准差）
        vol = np.array(
            [np.std(returns[max(0, i - 5) : i + 1]) for i in range(len(returns))]
        )
        X = np.column_stack([returns, vol])

        # 3状态 HMM
        model = GaussianHMM(
            n_components=3,
            covariance_type="diag",
            n_iter=100,
            random_state=42,
        )
        model.fit(X)
        hidden_states = model.predict(X)
        current_state = hidden_states[-1]

        # 根据每个状态的平均收益率排序，确定 bull/range/bear
        state_means = {}
        for s in range(3):
            mask = hidden_states == s
            if np.any(mask):
                state_means[s] = np.mean(returns[mask[: len(returns)]])
            else:
                state_means[s] = 0

        sorted_states = sorted(state_means.keys(), key=lambda s: state_means[s])
        state_map = {sorted_states[0]: "bear", sorted_states[1]: "range", sorted_states[2]: "bull"}

        regime = state_map[current_state]

        # 置信度 = 当前状态的后验概率
        proba = model.predict_proba(X)
        confidence = float(proba[-1, current_state])

        return {"regime": regime, "confidence": confidence}

    except ImportError:
        # hmmlearn 未安装，使用纯规则
        return {}
    except Exception:
        return {}


def _calc_regime_duration(
    closes: np.ndarray, ma20: np.ndarray, ma60: np.ndarray
) -> int:
    """计算当前状态持续天数（从最近的均线交叉算起）。"""
    try:
        # 找出 MA20 相对 MA60 的方向
        valid_start = 59  # MA60 需要至少60根数据
        if len(closes) <= valid_start:
            return 1

        # 当前 MA20 > MA60 还是 < MA60
        current_above = ma20[-1] > ma60[-1]

        # 从后往前找最近的交叉点
        duration = 0
        for i in range(len(closes) - 1, valid_start, -1):
            if np.isnan(ma20[i]) or np.isnan(ma60[i]):
                break
            above = ma20[i] > ma60[i]
            if above == current_above:
                duration += 1
            else:
                break

        return max(1, duration)

    except Exception:
        return 1


def _calc_transition_prob(
    closes: np.ndarray,
    ma20: np.ndarray,
    ma60: np.ndarray,
    current_regime: str,
) -> dict:
    """基于历史均线交叉统计状态转移概率。"""
    try:
        valid_start = 59
        if len(closes) <= valid_start + 20:
            # 数据不足，返回等概率
            return {"to_bull": 0.33, "to_range": 0.34, "to_bear": 0.33}

        # 将历史划分为状态序列
        regimes = []
        for i in range(valid_start, len(closes)):
            if np.isnan(ma20[i]) or np.isnan(ma60[i]):
                continue
            spread = (ma20[i] - ma60[i]) / ma60[i] if ma60[i] > 0 else 0
            # 简化：使用20日收益率
            ret_20 = (closes[i] / closes[max(0, i - 20)] - 1) if i >= 20 else 0

            if spread > 0.005 and ret_20 > 0:
                regimes.append("bull")
            elif spread < -0.005 and ret_20 < 0:
                regimes.append("bear")
            else:
                regimes.append("range")

        if len(regimes) < 10:
            return {"to_bull": 0.33, "to_range": 0.34, "to_bear": 0.33}

        # 统计从当前状态到各状态的转移次数
        trans = {"to_bull": 0, "to_range": 0, "to_bear": 0}
        total = 0
        for i in range(len(regimes) - 1):
            if regimes[i] == current_regime:
                next_r = regimes[i + 1]
                trans[f"to_{next_r}"] += 1
                total += 1

        if total > 0:
            for k in trans:
                trans[k] = trans[k] / total
        else:
            trans = {"to_bull": 0.33, "to_range": 0.34, "to_bear": 0.33}

        return trans

    except Exception:
        return {"to_bull": 0.33, "to_range": 0.34, "to_bear": 0.33}


def _default_result(reason: str) -> dict:
    """返回默认结果（震荡市）。"""
    return {
        "current_regime": "range",
        "confidence": 0.50,
        "regime_duration_days": 0,
        "transition_prob": {
            "to_bull": 0.33,
            "to_range": 0.34,
            "to_bear": 0.33,
        },
        "suggested_adjustments": REGIME_PARAMS["range"],
        "indicators": {},
        "summary": f"默认震荡市（{reason}）",
    }


if __name__ == "__main__":
    import pprint

    print("=" * 60)
    print("市场状态检测 — 马尔可夫状态模型")
    print("=" * 60)

    result = detect_market_regime()
    pprint.pprint(result, width=80)
