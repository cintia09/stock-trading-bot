#!/usr/bin/env python3
"""
FinRL 强化学习仓位管理 — 基准测试
用PPO训练仓位管理agent，对比等权和凯利公式策略。
"""

import os, sys, time, warnings
import numpy as np
import pandas as pd
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
import baostock as bs

warnings.filterwarnings("ignore")

# ============ 配置 ============
TOP_N = 10  # 用前N只股票（减少训练时间）
TRAIN_START = "2020-01-01"
TRAIN_END = "2023-12-31"
TEST_START = "2024-01-01"
TEST_END = "2026-01-31"
INITIAL_CASH = 1_000_000
TOTAL_TIMESTEPS = 50_000
MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
MODEL_PATH = os.path.join(MODEL_DIR, "finrl_ppo.zip")

# ============ 数据获取 ============
def get_hs300_stocks(n=TOP_N):
    """获取沪深300部分成分股代码"""
    # 选一些流动性好的大盘股
    stocks = [
        "sh.600519", "sh.601318", "sh.600036", "sh.600276", "sh.601166",
        "sh.600900", "sh.601398", "sh.600028", "sh.601288", "sh.600809",
        "sh.601012", "sh.600030", "sh.601088", "sh.600000", "sh.601668",
    ]
    return stocks[:n]


def fetch_data(stocks, start, end):
    """用BaoStock获取日线数据"""
    lg = bs.login()
    all_data = []
    for code in stocks:
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,volume,amount",
            start_date=start, end_date=end,
            frequency="d", adjustflag="2"  # 前复权
        )
        rows = []
        while (rs.error_code == '0') and rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        if len(df) > 0:
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(subset=["close"], inplace=True)
            all_data.append(df)
    bs.logout()
    if not all_data:
        raise ValueError("No data fetched!")
    data = pd.concat(all_data, ignore_index=True)
    data.sort_values(["date", "code"], inplace=True)
    return data


def add_indicators(df):
    """添加MACD、RSI技术指标"""
    result = []
    for code, grp in df.groupby("code"):
        g = grp.copy().sort_values("date").reset_index(drop=True)
        close = g["close"]
        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        g["macd"] = ema12 - ema26
        g["macd_signal"] = g["macd"].ewm(span=9).mean()
        # RSI 14
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / (loss + 1e-10)
        g["rsi"] = 100 - 100 / (1 + rs)
        # Normalize volume
        g["vol_norm"] = g["volume"] / (g["volume"].rolling(20).mean() + 1e-10)
        result.append(g)
    out = pd.concat(result, ignore_index=True)
    out.dropna(inplace=True)
    return out


# ============ 交易环境 ============
class PositionTradingEnv(gym.Env):
    """
    仓位管理环境
    状态: 各股票的 [close_norm, macd, rsi, vol_norm, current_weight]
    动作: 各股票的目标仓位比例 (0~1), 会被归一化
    """
    metadata = {"render_modes": []}

    def __init__(self, data, stock_codes, initial_cash=INITIAL_CASH):
        super().__init__()
        self.initial_cash = initial_cash
        self.stock_codes = stock_codes
        self.n_stocks = len(stock_codes)

        # 按日期pivot
        self.dates = sorted(data["date"].unique())
        self.data_by_date = {}
        for d in self.dates:
            day = data[data["date"] == d].set_index("code")
            self.data_by_date[d] = day

        self.action_space = spaces.Box(low=0, high=1, shape=(self.n_stocks,), dtype=np.float32)
        # state: per stock [close_norm, macd, rsi, vol_norm, weight]
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf,
            shape=(self.n_stocks * 5,), dtype=np.float32
        )
        self.reset()

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = 0
        self.portfolio_value = self.initial_cash
        self.weights = np.zeros(self.n_stocks, dtype=np.float32)
        self.prev_prices = None
        return self._get_obs(), {}

    def _get_obs(self):
        date = self.dates[self.current_step]
        day = self.data_by_date[date]
        obs = []
        for i, code in enumerate(self.stock_codes):
            if code in day.index:
                row = day.loc[code]
                obs.extend([
                    row["close"] / 100.0,  # rough normalize
                    row["macd"] / 10.0,
                    row["rsi"] / 100.0,
                    row["vol_norm"],
                    self.weights[i]
                ])
            else:
                obs.extend([0, 0, 0.5, 1.0, self.weights[i]])
        return np.array(obs, dtype=np.float32)

    def step(self, action):
        # Normalize action to sum=1
        action = np.clip(action, 0, 1)
        total = action.sum()
        if total > 0:
            new_weights = action / total
        else:
            new_weights = np.ones(self.n_stocks, dtype=np.float32) / self.n_stocks

        # Calculate returns
        date = self.dates[self.current_step]
        day = self.data_by_date[date]
        prices = np.array([
            day.loc[code]["close"] if code in day.index else 0
            for code in self.stock_codes
        ])

        if self.prev_prices is not None and np.all(self.prev_prices > 0):
            returns = (prices - self.prev_prices) / self.prev_prices
            portfolio_return = np.dot(self.weights, returns)
            self.portfolio_value *= (1 + portfolio_return)
        else:
            portfolio_return = 0.0

        # Turnover penalty
        turnover = np.sum(np.abs(new_weights - self.weights))
        reward = portfolio_return - 0.001 * turnover

        self.weights = new_weights.astype(np.float32)
        self.prev_prices = prices.copy()
        self.current_step += 1

        terminated = self.current_step >= len(self.dates) - 1
        truncated = False

        return self._get_obs(), reward, terminated, truncated, {
            "portfolio_value": self.portfolio_value,
            "date": date,
        }


# ============ 基准策略 ============
def backtest_equal_weight(data, stock_codes, initial_cash=INITIAL_CASH):
    """等权策略回测"""
    dates = sorted(data["date"].unique())
    n = len(stock_codes)
    weights = np.ones(n) / n
    portfolio_value = initial_cash
    prev_prices = None
    values = []

    for date in dates:
        day = data[data["date"] == date].set_index("code")
        prices = np.array([
            day.loc[code]["close"] if code in day.index else 0
            for code in stock_codes
        ])
        if prev_prices is not None and np.all(prev_prices > 0):
            returns = (prices - prev_prices) / prev_prices
            portfolio_value *= (1 + np.dot(weights, returns))
        prev_prices = prices.copy()
        values.append({"date": date, "value": portfolio_value})

    return pd.DataFrame(values)


def backtest_kelly(data, stock_codes, initial_cash=INITIAL_CASH, lookback=60):
    """简化凯利公式策略"""
    dates = sorted(data["date"].unique())
    n = len(stock_codes)
    portfolio_value = initial_cash
    prev_prices = None
    price_history = []
    values = []

    for date in dates:
        day = data[data["date"] == date].set_index("code")
        prices = np.array([
            day.loc[code]["close"] if code in day.index else 0
            for code in stock_codes
        ])

        if prev_prices is not None and np.all(prev_prices > 0):
            ret = (prices - prev_prices) / prev_prices
            price_history.append(ret)

            if len(price_history) >= lookback:
                hist = np.array(price_history[-lookback:])
                means = hist.mean(axis=0)
                stds = hist.std(axis=0) + 1e-10
                # Kelly fraction: f = mu / sigma^2
                kelly = means / (stds ** 2)
                kelly = np.clip(kelly, 0, 2)  # cap
                total = kelly.sum()
                if total > 0:
                    weights = kelly / total
                else:
                    weights = np.ones(n) / n
            else:
                weights = np.ones(n) / n

            portfolio_value *= (1 + np.dot(weights, ret))

        prev_prices = prices.copy()
        values.append({"date": date, "value": portfolio_value})

    return pd.DataFrame(values)


def backtest_ppo(model, data, stock_codes, initial_cash=INITIAL_CASH):
    """PPO agent回测"""
    env = PositionTradingEnv(data, stock_codes, initial_cash)
    obs, _ = env.reset()
    values = []
    done = False
    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, terminated, truncated, info = env.step(action)
        values.append({"date": info["date"], "value": info["portfolio_value"]})
        done = terminated or truncated
    return pd.DataFrame(values)


# ============ 评估指标 ============
def calc_metrics(df):
    """计算夏普、最大回撤、年化收益"""
    df = df.copy()
    df["return"] = df["value"].pct_change().fillna(0)
    total_days = len(df)
    total_return = df["value"].iloc[-1] / df["value"].iloc[0] - 1
    years = total_days / 252
    annual_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1

    # Sharpe (annualized, rf=0)
    daily_std = df["return"].std()
    sharpe = (df["return"].mean() / (daily_std + 1e-10)) * np.sqrt(252)

    # Max drawdown
    cummax = df["value"].cummax()
    drawdown = (df["value"] - cummax) / cummax
    max_dd = drawdown.min()

    return {
        "年化收益": f"{annual_return:.2%}",
        "夏普比率": f"{sharpe:.3f}",
        "最大回撤": f"{max_dd:.2%}",
        "总收益": f"{total_return:.2%}",
    }


# ============ 主流程 ============
def main():
    print("=" * 60)
    print("FinRL 强化学习仓位管理 — 基准测试")
    print("=" * 60)

    stocks = get_hs300_stocks(TOP_N)
    print(f"\n📊 股票池: {len(stocks)} 只")

    # 获取数据
    print("\n📥 获取训练数据...")
    train_data = fetch_data(stocks, TRAIN_START, TRAIN_END)
    train_data = add_indicators(train_data)
    print(f"   训练集: {train_data['date'].nunique()} 个交易日, {train_data['code'].nunique()} 只股票")

    print("\n📥 获取测试数据...")
    test_data = fetch_data(stocks, TEST_START, TEST_END)
    test_data = add_indicators(test_data)
    print(f"   测试集: {test_data['date'].nunique()} 个交易日, {test_data['code'].nunique()} 只股票")

    # 确保stock_codes一致
    common_codes = sorted(set(train_data["code"].unique()) & set(test_data["code"].unique()))
    print(f"   共同股票: {len(common_codes)} 只")

    if len(common_codes) == 0:
        print("❌ 没有共同股票，退出")
        return

    train_data = train_data[train_data["code"].isin(common_codes)]
    test_data = test_data[test_data["code"].isin(common_codes)]

    # 训练PPO
    print(f"\n🤖 训练PPO agent ({TOTAL_TIMESTEPS} steps)...")
    train_env = DummyVecEnv([lambda: PositionTradingEnv(train_data, common_codes)])

    t0 = time.time()
    model = PPO(
        "MlpPolicy", train_env,
        learning_rate=3e-4,
        n_steps=1024,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        verbose=0,
        device="cpu",
    )
    model.learn(total_timesteps=TOTAL_TIMESTEPS)
    train_time = time.time() - t0
    print(f"   训练耗时: {train_time:.1f}s")

    # 保存模型
    os.makedirs(MODEL_DIR, exist_ok=True)
    model.save(MODEL_PATH)
    print(f"   模型已保存: {MODEL_PATH}")

    # 回测
    print("\n📈 回测对比 (测试集)...")
    ppo_result = backtest_ppo(model, test_data, common_codes)
    eq_result = backtest_equal_weight(test_data, common_codes)
    kelly_result = backtest_kelly(test_data, common_codes)

    ppo_m = calc_metrics(ppo_result)
    eq_m = calc_metrics(eq_result)
    kelly_m = calc_metrics(kelly_result)

    # 输出结果
    print("\n" + "=" * 60)
    print("📊 回测结果对比")
    print("=" * 60)
    header = f"{'策略':<12} {'年化收益':>10} {'夏普比率':>10} {'最大回撤':>10} {'总收益':>10}"
    print(header)
    print("-" * 60)
    for name, m in [("PPO Agent", ppo_m), ("等权策略", eq_m), ("凯利公式", kelly_m)]:
        print(f"{name:<12} {m['年化收益']:>10} {m['夏普比率']:>10} {m['最大回撤']:>10} {m['总收益']:>10}")
    print("=" * 60)
    print(f"\n训练耗时: {train_time:.1f}s | 模型: {MODEL_PATH}")


if __name__ == "__main__":
    main()
