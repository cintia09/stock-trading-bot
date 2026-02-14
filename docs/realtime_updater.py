#!/usr/bin/env python3
"""dashboard/realtime_updater.py

每隔一段时间读取 stock-trading 下的多个 JSON 数据源，合并生成 dashboard/data.json 和 dashboard/data.js。

特性：
- 定时更新（交易时间 10s；非交易时间 60s 自动降频）
- 文件变更检测：所有源文件 mtime 都未变化则复用上次解析结果（但实时行情每次都会拉取）
- 日志：/tmp/dashboard_updater.log（成功更新仅一行时间戳；错误写详细堆栈）
- SIGTERM/SIGINT 优雅退出

运行方式：
  nohup python3 realtime_updater.py >/dev/null 2>&1 &
"""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any


DASHBOARD_DIR = Path(__file__).resolve().parent
BASE_DIR = (DASHBOARD_DIR.parent / "stock-trading").resolve()

OUTPUT_JSON = DASHBOARD_DIR / "data.json"
OUTPUT_JS = DASHBOARD_DIR / "data.js"

# 舆情数据目录
SENTIMENT_DATA_DIR = (DASHBOARD_DIR.parent / "stock-trading" / "sentiment-data" / "daily").resolve()

LOG_FILE = Path("/tmp/dashboard_updater.log")

# 刷新频率：交易时间 10s；非交易时间 60s
TRADING_INTERVAL_SECONDS = 10
OFF_HOURS_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class Source:
    name: str
    path: Path
    description: str


SOURCES: list[Source] = [
    Source("account", BASE_DIR / "account.json", "股票持仓与账户信息"),
    Source("transactions", BASE_DIR / "transactions.json", "交易记录"),
    Source("strategy_params", BASE_DIR / "strategy_params.json", "策略参数"),
    Source("watchlist", BASE_DIR / "watchlist.json", "关注列表"),
    Source("cb_opportunities", BASE_DIR / "data" / "cb_opportunities.json", "可转债套利机会"),
    Source("tomorrow_plan", BASE_DIR / "tomorrow_plan.json", "明日交易计划"),
]


_STOP = False

# 行情拉取失败时，保留最近一次成功报价（按新浪前缀代码：sh/sz + 6位）
_LAST_QUOTES: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_ok() -> None:
    # 每次成功更新只写一行时间戳
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{_now_iso()}\n")


def log_error(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{_now_iso()} ERROR {msg}\n")


def handle_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def get_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None
    except Exception:
        return None


def load_json_safe(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_trading_time(now: datetime | None = None) -> bool:
    """A股常规交易时间粗略判断：工作日 09:15-15:15。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False

    t = now.time()
    return dtime(9, 15) <= t <= dtime(15, 15)


def to_sina_stock_code(code: str) -> str | None:
    """把 6位股票代码转为新浪行情前缀格式：shXXXXXX / szXXXXXX。

    已是 sh/sz 前缀则原样返回。
    """
    c = (code or "").strip().lower()
    if not c:
        return None

    if c.startswith("sh") or c.startswith("sz"):
        return c

    # 只保留数字
    digits = re.sub(r"\D", "", c)
    if len(digits) != 6:
        return None

    if digits.startswith(("6", "68")):
        return f"sh{digits}"
    if digits.startswith(("0", "3")):
        return f"sz{digits}"

    return None


def to_sina_bond_code(bond_code: str) -> str | None:
    """可转债代码转新浪行情前缀。

    规则（按需求 + 常见编码兜底）：
    - 11xxxx -> sh
    - 12xxxx -> sz
    - 123/127/128 开头（常见深市转债）-> sz
    """
    c = (bond_code or "").strip().lower()
    if not c:
        return None

    if c.startswith("sh") or c.startswith("sz"):
        return c

    digits = re.sub(r"\D", "", c)
    if len(digits) != 6:
        return None

    if digits.startswith("11"):
        return f"sh{digits}"
    if digits.startswith("12"):
        return f"sz{digits}"

    if digits.startswith(("123", "127", "128")):
        return f"sz{digits}"

    # 兜底：按股票逻辑尝试
    return to_sina_stock_code(digits)


def fetch_realtime_quotes(codes: list[str]) -> dict[str, float]:
    """从新浪行情 API 批量获取实时价格。

    codes: 形如 ['sh601318','sz300896', ...]；也兼容传 6 位纯数字。

    返回：{sina_code: current_price}
    - 解析 "var hq_str_sh600000=\"名称,开盘,昨收,当前价,...\";"
    - 第3个字段(index 2) 为昨收；第4个字段(index 3) 为当前价
    - 当前价为 0 时回退到昨收价

    失败时：不抛异常，尽量返回 _LAST_QUOTES 中上次值。
    """

    sina_codes: list[str] = []
    for c in codes or []:
        sc = to_sina_stock_code(c) or to_sina_bond_code(c)
        if sc:
            sina_codes.append(sc)

    # 去重并保持顺序
    uniq: list[str] = []
    seen: set[str] = set()
    for sc in sina_codes:
        if sc not in seen:
            uniq.append(sc)
            seen.add(sc)

    if not uniq:
        return {}

    # 新浪接口单次可多码，这里保守分批，避免 URL 过长
    batch_size = 50
    result: dict[str, float] = {}

    try:
        for i in range(0, len(uniq), batch_size):
            batch = uniq[i : i + batch_size]
            url = "https://hq.sinajs.cn/list=" + ",".join(batch)
            req = urllib.request.Request(
                url,
                headers={
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0",
                },
                method="GET",
            )

            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()

            text = raw.decode("gbk", errors="ignore")
            for line in text.splitlines():
                # var hq_str_sh600000="...";
                if "hq_str_" not in line:
                    continue

                m = re.search(r"var\s+hq_str_(?P<code>sh\d{6}|sz\d{6})=\"(?P<body>.*)\";", line)
                if not m:
                    continue

                sina_code = m.group("code")
                body = m.group("body")
                if not body:
                    continue

                fields = body.split(",")
                if len(fields) < 4:
                    continue

                try:
                    prev_close = float(fields[2]) if fields[2] else 0.0
                except Exception:
                    prev_close = 0.0

                try:
                    current = float(fields[3]) if fields[3] else 0.0
                except Exception:
                    current = 0.0

                price = current if current > 0 else prev_close
                if price > 0:
                    result[sina_code] = price

        if result:
            _LAST_QUOTES.update(result)

    except Exception:
        # 拉取失败不崩，返回尽可能多的旧值
        pass

    # 补齐缺失的使用上次值
    for sc in uniq:
        if sc in _LAST_QUOTES and sc not in result:
            result[sc] = _LAST_QUOTES[sc]

    return result


def build_dashboard_data() -> dict[str, Any]:
    generated_at = _now_iso()

    dashboard_data: dict[str, Any] = {
        "_meta": {
            "generated_at": generated_at,
            "generator": "realtime_updater.py",
            "version": "1.2",
        },
        "sources": {},
    }

    for s in SOURCES:
        data = load_json_safe(s.path)
        mtime = get_mtime(s.path)
        last_updated = datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None

        dashboard_data["sources"][s.name] = {
            "data": data,
            "description": s.description,
            "last_updated": last_updated if data is not None else None,
            "available": data is not None,
        }

    return dashboard_data


def _update_daily_stats(account_data: dict[str, Any]) -> None:
    """从 transactions.json 计算当日盈亏和交易笔数，分别统计股票和转债。"""
    try:
        txn_path = BASE_DIR / "transactions.json"
        if not txn_path.exists():
            return
        with txn_path.open("r", encoding="utf-8") as f:
            txns = json.load(f)

        records = txns if isinstance(txns, list) else txns.get("records", txns.get("transactions", []))
        today_str = datetime.now().strftime("%Y-%m-%d")

        today_txns = [t for t in records if isinstance(t, dict) and today_str in str(t.get("timestamp", ""))]
        account_data["trade_count"] = len(today_txns)

        # 当日已实现盈亏 = 卖出交易的 (卖价 - 成本) * 数量
        # 分别计算股票和转债
        stock_daily_realized = 0.0
        cb_daily_realized = 0.0
        
        for t in today_txns:
            action = str(t.get("action", "")).lower()
            if action in ("sell", "卖出"):
                price = float(t.get("price", 0) or 0)
                cost = float(t.get("cost_price", t.get("avg_cost", 0)) or 0)
                qty = abs(float(t.get("quantity", t.get("shares", 0)) or 0))
                if cost > 0 and price > 0:
                    pnl = (price - cost) * qty
                    # 判断是转债还是股票：有 bond_code 字段或 code 以 11/12 开头
                    code = str(t.get("code", t.get("bond_code", "")) or "")
                    is_cb = t.get("bond_code") or code.startswith("11") or code.startswith("12")
                    if is_cb:
                        cb_daily_realized += pnl
                    else:
                        stock_daily_realized += pnl

        # 当日浮动盈亏 = 持仓当前盈亏
        stock_daily_unrealized = 0.0
        cb_daily_unrealized = 0.0
        
        for h in account_data.get("holdings", []):
            if isinstance(h, dict):
                stock_daily_unrealized += float(h.get("profit_loss", 0) or 0)
        for cb in account_data.get("cb_holdings", []):
            if isinstance(cb, dict):
                cb_daily_unrealized += float(cb.get("profit_loss", 0) or 0)

        # 分类当日盈亏
        stock_daily_pnl = stock_daily_realized + stock_daily_unrealized
        cb_daily_pnl = cb_daily_realized + cb_daily_unrealized
        total_daily_pnl = stock_daily_pnl + cb_daily_pnl
        
        account_data["stock_daily_pnl"] = round(stock_daily_pnl, 2)
        account_data["cb_daily_pnl"] = round(cb_daily_pnl, 2)
        account_data["daily_pnl"] = round(total_daily_pnl, 2)
        
        initial = float(account_data.get("initial_capital", 0) or 0)
        if initial > 0:
            account_data["daily_pnl_pct"] = round(total_daily_pnl / initial * 100, 2)
            # 分类百分比也计算一下（相对于各自持仓价值）
            stock_value = float(account_data.get("stock_holdings_value", 0) or 0)
            cb_value = float(account_data.get("cb_holdings_value", 0) or 0)
            if stock_value > 0:
                account_data["stock_daily_pnl_pct"] = round(stock_daily_pnl / stock_value * 100, 2)
            if cb_value > 0:
                account_data["cb_daily_pnl_pct"] = round(cb_daily_pnl / cb_value * 100, 2)
    except Exception:
        pass


def _update_account_realtime(account_data: Any) -> None:
    if not isinstance(account_data, dict):
        return

    holdings = account_data.get("holdings")
    cb_holdings = account_data.get("cb_holdings")
    if not holdings and not cb_holdings:
        return

    # holdings 兼容 list / dict 两种结构
    holding_items: list[dict[str, Any]] = []
    if isinstance(holdings, list):
        holding_items = [h for h in holdings if isinstance(h, dict)]
    elif isinstance(holdings, dict):
        # {"601318": {..}} -> list with code
        for code, info in holdings.items():
            if isinstance(info, dict):
                item = {"code": str(code), **info}
                holding_items.append(item)

    sina_codes: list[str] = []
    by_sina_code: dict[str, list[dict[str, Any]]] = {}
    for h in holding_items:
        code = str(h.get("code") or "").strip()
        sc = to_sina_stock_code(code)
        if not sc:
            continue
        sina_codes.append(sc)
        by_sina_code.setdefault(sc, []).append(h)

    quotes = fetch_realtime_quotes(sina_codes)

    # 股票部分统计
    stock_mkt_value = 0.0
    stock_cost_total = 0.0
    stock_pnl = 0.0
    
    for sc, hs in by_sina_code.items():
        price = quotes.get(sc)
        if price is None:
            continue

        for h in hs:
            qty = h.get("shares")
            if qty is None:
                qty = h.get("quantity")
            try:
                shares = float(qty)
            except Exception:
                shares = 0.0

            try:
                cost_price = float(h.get("cost_price") or 0)
            except Exception:
                cost_price = 0.0

            market_value = float(price) * shares
            profit_loss = (float(price) - cost_price) * shares
            profit_pct = ((float(price) - cost_price) / cost_price * 100) if cost_price > 0 else 0.0

            h["current_price"] = round(float(price), 4)
            h["market_value"] = round(market_value, 2)
            h["profit_loss"] = round(profit_loss, 2)
            h["profit_pct"] = round(profit_pct, 4)
            # 兼容旧字段
            h["pnl_pct"] = round(profit_pct, 4)

            stock_mkt_value += market_value
            stock_cost_total += cost_price * shares
            stock_pnl += profit_loss

    # --- cb_holdings realtime ---
    cb_items: list[dict[str, Any]] = []
    if isinstance(cb_holdings, list):
        cb_items = [h for h in cb_holdings if isinstance(h, dict)]

    cb_sina_codes: list[str] = []
    cb_by_sina: dict[str, list[dict[str, Any]]] = {}
    for h in cb_items:
        bond_code = str(h.get("bond_code") or "").strip()
        sc = to_sina_bond_code(bond_code)
        if not sc:
            continue
        cb_sina_codes.append(sc)
        cb_by_sina.setdefault(sc, []).append(h)

    # 转债部分统计
    cb_mkt_value = 0.0
    cb_cost_total = 0.0
    cb_pnl = 0.0

    cb_quotes = fetch_realtime_quotes(cb_sina_codes) if cb_sina_codes else {}
    for sc, hs in cb_by_sina.items():
        price = cb_quotes.get(sc)
        if price is None:
            continue
        for h in hs:
            try:
                shares = float(h.get("shares", 0) or 0)
            except Exception:
                shares = 0.0
            try:
                cost_price = float(h.get("cost_price") or 0)
            except Exception:
                cost_price = 0.0

            market_value = float(price) * shares
            profit_loss = (float(price) - cost_price) * shares
            profit_pct = ((float(price) - cost_price) / cost_price * 100) if cost_price > 0 else 0.0

            h["current_price"] = round(float(price), 4)
            h["market_value"] = round(market_value, 2)
            h["profit_loss"] = round(profit_loss, 2)
            h["profit_pct"] = round(profit_pct, 4)
            h["pnl_pct"] = round(profit_pct, 4)

            cb_mkt_value += market_value
            cb_cost_total += cost_price * shares
            cb_pnl += profit_loss

    total_mkt_value = stock_mkt_value + cb_mkt_value

    # 现金字段兼容：cash / current_cash
    cash = account_data.get("cash")
    if cash is None:
        cash = account_data.get("current_cash")

    try:
        cash_value = float(cash or 0)
    except Exception:
        cash_value = 0.0

    total_value = cash_value + total_mkt_value
    account_data["total_value"] = round(total_value, 2)

    # 重算盈亏（基于 initial_capital）
    initial_capital = float(account_data.get("initial_capital", 0) or 0)
    if initial_capital > 0:
        total_pnl = total_value - initial_capital
        account_data["total_pnl"] = round(total_pnl, 2)
        account_data["total_pnl_pct"] = round(total_pnl / initial_capital * 100, 2)

    # 分类统计写入 account_data
    account_data["stock_holdings_value"] = round(stock_mkt_value, 2)
    account_data["stock_cost"] = round(stock_cost_total, 2)
    account_data["stock_pnl"] = round(stock_pnl, 2)
    account_data["stock_count"] = len(holding_items)
    
    account_data["cb_holdings_value"] = round(cb_mkt_value, 2)
    account_data["cb_cost"] = round(cb_cost_total, 2)
    account_data["cb_pnl"] = round(cb_pnl, 2)
    account_data["cb_count"] = len(cb_items)

    account_data["last_updated"] = datetime.now().isoformat(timespec="seconds")

    # 计算当日盈亏和交易笔数（从 holdings 的 cost vs current 推算）
    _update_daily_stats(account_data)


def _update_cb_realtime(cb_data: Any) -> None:
    if not isinstance(cb_data, dict):
        return

    opps = cb_data.get("opportunities")
    if not isinstance(opps, list) or not opps:
        return

    sina_codes: list[str] = []
    by_sina: dict[str, list[dict[str, Any]]] = {}

    for opp in opps:
        if not isinstance(opp, dict):
            continue
        bond_code = str(opp.get("bond_code") or "").strip()
        sc = to_sina_bond_code(bond_code)
        if not sc:
            continue
        sina_codes.append(sc)
        by_sina.setdefault(sc, []).append(opp)

    quotes = fetch_realtime_quotes(sina_codes)
    for sc, items in by_sina.items():
        price = quotes.get(sc)
        if price is None:
            continue
        for opp in items:
            opp["bond_price"] = round(float(price), 4)


# 舆情数据缓存
_sentiment_cache: dict[str, Any] = {"mtime": None, "data": None}


def _get_today_sentiment_path() -> Path:
    """获取今天的舆情数据文件路径"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    return SENTIMENT_DATA_DIR / f"{today_str}.json"


def _load_sentiment_data() -> dict[str, Any] | None:
    """读取今天的舆情数据，带 mtime 缓存"""
    path = _get_today_sentiment_path()
    current_mtime = get_mtime(path)

    if current_mtime is None:
        return None

    # 文件没变化，用缓存
    if _sentiment_cache["mtime"] == current_mtime and _sentiment_cache["data"] is not None:
        return _sentiment_cache["data"]

    # 重新读取
    data = load_json_safe(path)
    if data is not None:
        _sentiment_cache["mtime"] = current_mtime
        _sentiment_cache["data"] = data

    return data


def _update_news_briefing(sources: dict[str, Any]) -> None:
    """更新 news_briefing 数据源（从舆情数据文件读取最新一小时）"""
    sentiment_records = _load_sentiment_data()

    if not sentiment_records or not isinstance(sentiment_records, list) or len(sentiment_records) == 0:
        sources["news_briefing"] = {
            "data": None,
            "description": "舆情分析简报",
            "last_updated": None,
            "available": False,
        }
        return

    # 取最新一条记录
    latest = sentiment_records[-1]

    sources["news_briefing"] = {
        "data": {
            "updated_at": latest.get("timestamp"),
            "social_temperature": latest.get("social_temperature"),
            "market_sentiment": latest.get("market_sentiment"),
            "fear_greed": latest.get("fear_greed_estimate"),
            "consumer_confidence": latest.get("consumer_confidence"),
            "key_signals": latest.get("key_signals", []),
            "portfolio_alerts": latest.get("portfolio_alerts", []),
            "social_highlights": latest.get("social_highlights", []),
            "items": latest.get("items", []),
        },
        "description": "舆情分析简报",
        "last_updated": latest.get("timestamp"),
        "available": True,
    }


def apply_realtime_updates(dashboard_data: dict[str, Any]) -> None:
    """在不改变文件变更检测逻辑的前提下，每次循环都做实时行情更新。"""

    # 更新 meta
    dashboard_data.setdefault("_meta", {})
    dashboard_data["_meta"]["generated_at"] = _now_iso()

    sources = dashboard_data.get("sources")
    if not isinstance(sources, dict):
        return

    account = sources.get("account", {}).get("data")
    _update_account_realtime(account)

    cb = sources.get("cb_opportunities", {}).get("data")
    _update_cb_realtime(cb)

    # 更新舆情简报
    _update_news_briefing(sources)


def write_outputs(dashboard_data: dict[str, Any]) -> None:
    # data.json（推荐给前端 fetch）
    atomic_write_text(OUTPUT_JSON, json.dumps(dashboard_data, ensure_ascii=False, indent=2) + "\n")

    # data.js（兼容旧版引用方式）
    js_content = (
        "// 投资看板数据文件 - 自动生成，请勿手动编辑\n"
        f"// 生成时间: {dashboard_data['_meta']['generated_at']}\n"
        "\n"
        f"window.DASHBOARD_DATA = {json.dumps(dashboard_data, ensure_ascii=False, indent=2)};\n"
    )
    atomic_write_text(OUTPUT_JS, js_content)


def main() -> int:
    # 信号处理
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_mtimes: dict[str, float | None] | None = None
    cached_base_data: dict[str, Any] | None = None

    while not _STOP:
        interval = TRADING_INTERVAL_SECONDS if is_trading_time() else OFF_HOURS_INTERVAL_SECONDS

        try:
            current_mtimes = {str(s.path): get_mtime(s.path) for s in SOURCES}

            # 文件变更检测逻辑保留：仅当源文件有变化时才重新 load/parse
            if cached_base_data is None or last_mtimes is None or current_mtimes != last_mtimes:
                cached_base_data = build_dashboard_data()
                last_mtimes = current_mtimes

            # 实时行情：每次都拉取，覆盖到输出数据中（不影响缓存的 base）
            dashboard_data = copy.deepcopy(cached_base_data)
            apply_realtime_updates(dashboard_data)

            write_outputs(dashboard_data)
            log_ok()

        except Exception as e:
            # 错误写详细堆栈，便于排障
            log_error(str(e))
            try:
                with LOG_FILE.open("a", encoding="utf-8") as f:
                    f.write(traceback.format_exc() + "\n")
            except Exception:
                pass

        time.sleep(interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
