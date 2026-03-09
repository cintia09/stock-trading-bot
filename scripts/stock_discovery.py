#!/usr/bin/env python3
"""股票自动发现模块 - 发现潜力股票并更新关注列表

增强内容：
- 新增 fetch_strong_stocks(): 近 3 天连续上涨且每日至少 +2%（通过今日涨幅榜 Top50 + 3 日 K 线验证）
- 新增 fetch_institution_holdings(): 近期机构/主力增持（按主力净流入 f62 排序，筛选 >5000 万的前 10）
- 新增 fetch_ai_infra_stocks(): 读取AI基础设施股票研究结果，给高共识AI基础设施股加分(+20)
- discover_stocks() 评分加入：连涨 +10 分，机构增持 +15 分，AI基础设施 +20 分

约束：不修改既有对外接口，只新增函数/在原逻辑末尾追加。
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

BASE_DIR = Path(__file__).parent.parent

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json,text/plain,*/*",
}

# --- Reliability knobs (env overrides for testing) ---
EASTMONEY_TIMEOUT = float(os.getenv("EASTMONEY_TIMEOUT", "12"))
EASTMONEY_RETRIES = int(os.getenv("EASTMONEY_RETRIES", "3"))
EASTMONEY_FORCE_FAIL = os.getenv("EASTMONEY_FORCE_FAIL", "0") == "1"  # smoke-test helper
FORCE_DISCOVERY_EMPTY = os.getenv("FORCE_DISCOVERY_EMPTY", "0") == "1"  # smoke-test helper
FORCE_BAOSTOCK_POOL = os.getenv("FORCE_BAOSTOCK_POOL", "0") == "1"  # smoke-test helper

LAST_GOOD_MAX_TRADING_DAYS = int(os.getenv("LAST_GOOD_MAX_TRADING_DAYS", "3"))
LAST_GOOD_MAX_REUSE = int(os.getenv("LAST_GOOD_MAX_REUSE", "2"))

BAOSTOCK_POOL_MAX_CODES = int(os.getenv("BAOSTOCK_POOL_MAX_CODES", "260"))


def _em_get_json(name: str, url: str, params: dict, timeout: float = EASTMONEY_TIMEOUT, retries: int = EASTMONEY_RETRIES) -> Optional[dict]:
    """Eastmoney GET with timeout + retry + clearer logs."""
    if EASTMONEY_FORCE_FAIL:
        raise RuntimeError("EASTMONEY_FORCE_FAIL=1 (smoke test)")

    last_err: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                time.sleep(0.6 * attempt)
            resp = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            et = type(e).__name__
            msg = str(e)
            print(f"[eastmoney][{name}] attempt={attempt}/{retries} failed: {et}: {msg}")
    print(f"[eastmoney][{name}] giving up after {retries} attempts: {type(last_err).__name__}: {last_err}")
    return None


def _trading_day_threshold(days: int) -> datetime:
    """Return a datetime threshold that is N trading days ago (approx: weekday-based)."""
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    left = max(0, int(days))
    while left > 0:
        d = d - timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            left -= 1
    return d


def _load_json(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_top_gainers(limit: int = 20) -> List[Dict]:
    """获取涨幅榜"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": limit, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21"
    }

    try:
        data = _em_get_json("top_gainers", url, params)

        if data and data.get("data") and data["data"].get("diff"):
            return [{
                "code": str(item.get("f12", "")).zfill(6),
                "name": item.get("f14", ""),
                "price": item.get("f2", 0),
                "change_pct": item.get("f3", 0),
                "volume": item.get("f5", 0),
                "amount": item.get("f6", 0),
                "amplitude": item.get("f7", 0),
                "turnover": item.get("f8", 0),
                "pe": item.get("f9", 0),
                "pb": item.get("f10", 0),
                "market_cap": item.get("f20", 0),
                "source": "涨幅榜"
            } for item in data["data"]["diff"]]
    except Exception as e:
        print(f"涨幅榜获取失败: {e}")
    return []


def fetch_top_volume(limit: int = 20) -> List[Dict]:
    """获取成交额榜"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": limit, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f6",  # 按成交额排序
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21"
    }

    try:
        data = _em_get_json("top_volume", url, params)

        if data and data.get("data") and data["data"].get("diff"):
            return [{
                "code": str(item.get("f12", "")).zfill(6),
                "name": item.get("f14", ""),
                "price": item.get("f2", 0),
                "change_pct": item.get("f3", 0),
                "amount": item.get("f6", 0),
                "turnover": item.get("f8", 0),
                "pe": item.get("f9", 0),
                "market_cap": item.get("f20", 0),
                "source": "成交额榜"
            } for item in data["data"]["diff"]]
    except Exception as e:
        print(f"成交额榜获取失败: {e}")
    return []


def fetch_sector_leaders() -> List[Dict]:
    """获取板块龙头"""
    leaders = []

    # 获取行业板块
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 10, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3",
        "fs": "m:90+t:2",  # 行业板块
        "fields": "f2,f3,f12,f14"
    }

    try:
        data = _em_get_json("sector_list", url, params)

        if data and data.get("data") and data["data"].get("diff"):
            for sector in data["data"]["diff"][:5]:  # 前5热门板块
                sector_code = sector.get("f12", "")
                sector_name = sector.get("f14", "")

                # 获取板块成分股
                member_params = {
                    "pn": 1, "pz": 3, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f6",
                    "fs": f"b:{sector_code}",
                    "fields": "f2,f3,f6,f12,f14,f20"
                }

                member_data = _em_get_json(f"sector_members:{sector_code}", url, member_params)

                if member_data and member_data.get("data") and member_data["data"].get("diff"):
                    for item in member_data["data"]["diff"][:2]:  # 每板块取前2
                        leaders.append({
                            "code": str(item.get("f12", "")).zfill(6),
                            "name": item.get("f14", ""),
                            "price": item.get("f2", 0),
                            "change_pct": item.get("f3", 0),
                            "amount": item.get("f6", 0),
                            "market_cap": item.get("f20", 0),
                            "sector": sector_name,
                            "source": f"{sector_name}龙头"
                        })
    except Exception as e:
        print(f"板块龙头获取失败: {e}")

    return leaders


def fetch_northbound_top() -> List[Dict]:
    """获取北向资金净买入榜"""
    stocks = []

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 20, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f62",  # 按北向资金排序（原有实现）
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f6,f12,f14,f62,f184,f66"
    }

    try:
        data = _em_get_json("northbound", url, params)

        if data and data.get("data") and data["data"].get("diff"):
            for item in data["data"]["diff"][:10]:
                f62 = item.get("f62", 0)
                try:
                    f62 = float(f62)
                except (ValueError, TypeError):
                    f62 = 0
                if f62 > 0:  # 净买入为正
                    stocks.append({
                        "code": str(item.get("f12", "")).zfill(6),
                        "name": item.get("f14", ""),
                        "price": item.get("f2", 0),
                        "change_pct": item.get("f3", 0),
                        "amount": item.get("f6", 0),
                        "north_net": item.get("f62", 0),  # 北向净买入(万)
                        "source": "北向资金"
                    })
    except Exception as e:
        print(f"北向资金数据获取失败: {e}")

    return stocks


# ============ 新增发现渠道 ============

def fetch_strong_stocks() -> List[Dict]:
    """获取近 3 天连续上涨且每日至少 +2% 的股票。

    实现：
    1) 获取今日涨幅榜 Top50
    2) 对每只取近 5 日K，验证最近 3 日 change_pct>2 且收盘价连续走高

    返回的 source 标注为 "三连涨"，便于 discover_stocks() 加分。
    """
    strong: List[Dict] = []
    try:
        # 取 Top50 作为候选池
        candidates = fetch_top_gainers(50)
        if not candidates:
            return []

        # 延迟导入，避免模块加载失败影响 discover
        try:
            from fetch_stock_data import fetch_kline  # when run as script
        except Exception:
            try:
                from scripts.fetch_stock_data import fetch_kline  # when imported as package
            except Exception:
                fetch_kline = None

        if not fetch_kline:
            return []

        for s in candidates:
            code = s.get("code", "")
            name = s.get("name", "")
            if not code:
                continue
            try:
                kl = fetch_kline(code, period="101", limit=8)
                if not kl or len(kl) < 3:
                    continue
                last3 = kl[-3:]

                # 连续上涨：收盘价逐日上升
                closes = [float(k.get("close", 0)) for k in last3]
                if not (closes[0] < closes[1] < closes[2]):
                    continue

                # 且每日日涨幅 > 2%
                cpcts = [float(k.get("change_pct", 0)) for k in last3]
                if not all(c > 2.0 for c in cpcts):
                    continue

                strong.append({
                    **s,
                    "source": "三连涨",
                })
            except Exception:
                continue

        return strong[:20]
    except Exception:
        return []


def fetch_institution_holdings() -> List[Dict]:
    """获取近期机构/主力增持股票。

    需求：
    - 东财接口 clist/get
    - fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23
    - fid=f62 按主力净流入排序
    - 取主力净流入 > 5000万 的前 10 只

    注：接口字段单位在不同版本可能是“万”。这里按旧实现习惯使用“万”为单位：阈值 5000。
    """
    stocks: List[Dict] = []
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1,
            "pz": 50,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f3,f6,f12,f14,f62,f20",
        }

        data = _em_get_json("institution_holdings", url, params)
        diff = (((data or {}).get("data") or {}).get("diff")) or []
        if not isinstance(diff, list):
            return []

        for item in diff:
            net = item.get("f62", 0)
            try:
                net = float(net)
            except (ValueError, TypeError):
                net = 0

            if net <= 5000:  # >5000万（按“万”为单位的阈值）
                continue

            stocks.append({
                "code": str(item.get("f12", "")).zfill(6),
                "name": item.get("f14", ""),
                "price": item.get("f2", 0),
                "change_pct": item.get("f3", 0),
                "amount": item.get("f6", 0),
                "market_cap": item.get("f20", 0),
                "main_net_inflow": item.get("f62", 0),
                "source": "机构增持",
            })

            if len(stocks) >= 10:
                break

        return stocks
    except Exception as e:
        print(f"机构增持数据获取失败: {e}")
        return []


def fetch_ai_infra_stocks() -> List[Dict]:
    """读取AI基础设施股票研究结果（每日05:00三模型并行研究+交叉质询）。

    数据来源: stock-trading/ai-infra-tracking/daily/YYYY-MM-DD.json
    读取最新一天的top10_final，返回标准格式的股票列表。
    AI基础设施股在选股中获得额外加分(+20)，体现投资偏好。
    """
    stocks: List[Dict] = []
    try:
        tracking_dir = BASE_DIR / "ai-infra-tracking" / "daily"
        if not tracking_dir.exists():
            print("  AI基础设施跟踪目录不存在")
            return []

        # 找最新的文件
        files = sorted(tracking_dir.glob("*.json"), reverse=True)
        if not files:
            print("  无AI基础设施研究数据")
            return []

        latest_file = files[0]
        # 只用最近3天的数据
        file_date = latest_file.stem  # "2026-02-12"
        try:
            from datetime import timedelta
            fd = datetime.strptime(file_date, "%Y-%m-%d")
            if (datetime.now() - fd).days > 3:
                print(f"  AI基础设施数据过旧({file_date})，跳过")
                return []
        except ValueError:
            pass

        with open(latest_file, 'r') as f:
            data = json.load(f)

        top10 = data.get("top10_final", [])
        print(f"  读取AI基础设施研究({file_date}): {len(top10)}只股票")

        for item in top10:
            code = str(item.get("code", "")).zfill(6)
            ai_score = item.get("ai_score", 0)
            consensus = item.get("consensus", "")

            stocks.append({
                "code": code,
                "name": item.get("name", ""),
                "price": 0,  # 实时价从其他源获取
                "change_pct": 0,
                "ai_infra_score": ai_score,
                "ai_infra_category": item.get("category", ""),
                "ai_infra_consensus": consensus,
                "ai_infra_reason": item.get("reason", ""),
                "source": "AI基础设施研究",
            })

        return stocks
    except Exception as e:
        print(f"AI基础设施数据获取失败: {e}")
        return []


# ============ BaoStock 兜底候选池（强硬修复） ============

def _baostock_index_constituents(index_name: str) -> List[str]:
    """Fetch index constituents via BaoStock. Returns 6-digit codes."""
    try:
        import baostock as bs

        lg = bs.login()
        if lg.error_code != '0':
            print(f"[baostock] login failed: {lg.error_msg}")
            return []

        if index_name == "hs300":
            rs = bs.query_hs300_stocks()
        elif index_name == "zz500":
            rs = bs.query_zz500_stocks()
        elif index_name == "sz50":
            rs = bs.query_sz50_stocks()
        else:
            return []

        codes: List[str] = []
        while rs.next():
            row = rs.get_row_data()
            # row usually: [updateDate, code]
            if len(row) >= 2 and row[1]:
                codes.append(str(row[1]).split(".")[-1].zfill(6))

        bs.logout()
        return codes
    except Exception as e:
        print(f"[baostock] index constituents failed ({index_name}): {type(e).__name__}: {e}")
        return []


def fetch_candidate_pool_baostock(max_codes: int = BAOSTOCK_POOL_MAX_CODES) -> List[str]:
    """Generate a stable candidate pool using indices to avoid full-market scanning."""
    codes = []
    # Prefer broad+liquid pools
    codes.extend(_baostock_index_constituents("hs300"))
    codes.extend(_baostock_index_constituents("zz500"))
    codes.extend(_baostock_index_constituents("sz50"))

    # de-dup while preserving order
    seen = set()
    uniq: List[str] = []
    for c in codes:
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        uniq.append(c)

    if max_codes and len(uniq) > max_codes:
        uniq = uniq[:max_codes]

    print(f"[baostock] candidate pool size={len(uniq)} (cap={max_codes})")
    return uniq


def fetch_baostock_pool_picks(max_codes: int = BAOSTOCK_POOL_MAX_CODES) -> List[Dict]:
    """Fallback discovery using BaoStock daily K-lines.

    Strategy: use index pool (HS300+ZZ500+SZ50) → compute simple momentum scores.
    """
    try:
        # 延迟导入，避免主流程因 baostock 不可用而崩
        try:
            from fetch_stock_data import fetch_kline_baostock  # when run as script
        except Exception:
            from scripts.fetch_stock_data import fetch_kline_baostock  # when imported as package
    except Exception as e:
        print(f"[baostock] cannot import fetch_kline_baostock: {e}")
        return []

    pool = fetch_candidate_pool_baostock(max_codes=max_codes)
    if not pool:
        return []

    picks: List[Dict] = []
    scanned = 0
    for code in pool:
        scanned += 1
        try:
            kl = fetch_kline_baostock(code, limit=30)
            if not kl or len(kl) < 6:
                continue
            last = kl[-1]
            last_close = float(last.get("close", 0) or 0)
            last_cpct = float(last.get("change_pct", 0) or 0)

            # Simple momentum: last day + recent trend
            last5 = kl[-5:]
            last10 = kl[-10:] if len(kl) >= 10 else kl

            # 5d sum pct + close slope
            sum5 = sum(float(x.get("change_pct", 0) or 0) for x in last5)
            closes10 = [float(x.get("close", 0) or 0) for x in last10]
            slope = closes10[-1] - closes10[0] if closes10 else 0

            score = 0
            score += max(-9.9, min(9.9, last_cpct))
            score += 0.6 * sum5
            score += 0.02 * slope

            # Avoid limit-up/down and illiquid junk by soft rules
            if last_close <= 0:
                continue
            if abs(last_cpct) >= 9.9:
                continue

            picks.append({
                "code": str(code).zfill(6),
                "name": str(code).zfill(6),  # name may be missing in BaoStock; keep non-empty to avoid downstream blanks
                "price": last_close,
                "change_pct": last_cpct,
                "volume": last.get("volume", 0),
                "amount": last.get("amount", 0),
                "market_cap": 0,
                "source": "BaoStock指数池",
                "_baostock_score": score,
            })
        except Exception:
            continue

    picks = sorted(picks, key=lambda x: x.get("_baostock_score", 0), reverse=True)
    for p in picks:
        p.pop("_baostock_score", None)

    print(f"[baostock] scanned={scanned}, got={len(picks)}")
    return picks[:40]


# ============ 原有逻辑 ============

def filter_quality_stocks(stocks: List[Dict]) -> List[Dict]:
    """过滤高质量股票"""
    filtered = []
    seen_codes = set()

    for s in stocks:
        code = s.get("code", "")

        # 跳过已添加
        if code in seen_codes:
            continue

        # 过滤ST股
        name = s.get("name", "")
        if "ST" in name or "退" in name:
            continue

        # 过滤涨停/跌停 (可能无法买入)
        try:
            change_pct = float(s.get("change_pct", 0))
        except (ValueError, TypeError):
            change_pct = 0
        if abs(change_pct) >= 9.9:
            continue

        # 过滤低价股 (< 5元) — AI基础设施研究来源豁免（price=0是因为没实时数据）
        try:
            price = float(s.get("price", 0))
        except (ValueError, TypeError):
            price = 0
        if price < 5 and s.get("source") != "AI基础设施研究":
            continue

        # 过滤市值过小 (< 100亿) — AI基础设施研究来源豁免
        try:
            market_cap = float(s.get("market_cap", 0))
        except (ValueError, TypeError):
            market_cap = 0
        if market_cap > 0 and market_cap < 10000000000 and s.get("source") != "AI基础设施研究":  # 100亿
            continue

        seen_codes.add(code)
        filtered.append(s)

    return filtered


def discover_stocks() -> Dict:
    """发现潜力股票"""
    print("🔍 开始股票发现...")

    all_stocks = []

    # 1. 涨幅榜
    print("  获取涨幅榜...")
    gainers = fetch_top_gainers(20)
    all_stocks.extend(gainers)

    # 2. 成交额榜
    print("  获取成交额榜...")
    volume = fetch_top_volume(20)
    all_stocks.extend(volume)

    # 3. 板块龙头
    print("  获取板块龙头...")
    leaders = fetch_sector_leaders()
    all_stocks.extend(leaders)

    # 4. 北向资金
    print("  获取北向资金...")
    north = fetch_northbound_top()
    all_stocks.extend(north)

    # P0 强硬修复：当东财榜单整体不可用时，直接启用 BaoStock 指数成分池兜底
    eastmoney_lists_empty = (len(gainers) + len(volume) + len(leaders) + len(north) == 0)
    if FORCE_BAOSTOCK_POOL or eastmoney_lists_empty:
        reason = "FORCE_BAOSTOCK_POOL=1" if FORCE_BAOSTOCK_POOL else "eastmoney_lists_empty"
        print(f"  [P0] 启用 BaoStock 指数池兜底: {reason}")
        bs_picks = fetch_baostock_pool_picks(max_codes=BAOSTOCK_POOL_MAX_CODES)
        all_stocks.extend(bs_picks)

    # 5. 新增：近3天连涨
    print("  获取三连涨股票...")
    strong = fetch_strong_stocks()
    all_stocks.extend(strong)

    # 6. 新增：机构/主力增持
    print("  获取机构增持股票...")
    inst = fetch_institution_holdings()
    all_stocks.extend(inst)

    # 7. 新增：AI基础设施研究（投资偏好）
    print("  获取AI基础设施研究...")
    ai_infra = fetch_ai_infra_stocks()
    all_stocks.extend(ai_infra)

    # 过滤
    print("  过滤质量股票...")
    quality = filter_quality_stocks(all_stocks)

    strong_set = {s.get("code") for s in strong if s.get("code")}
    inst_set = {s.get("code") for s in inst if s.get("code")}
    ai_infra_map = {s.get("code"): s for s in ai_infra if s.get("code")}

    # 去重并评分
    stock_scores = {}
    for s in quality:
        code = s["code"]
        if code not in stock_scores:
            stock_scores[code] = {
                **s,
                "discovery_score": 0,
                "sources": [],
                "_bonus_strong": False,
                "_bonus_inst": False,
                "_bonus_ai_infra": False,
            }

        # 来源越多分数越高
        stock_scores[code]["sources"].append(s.get("source", ""))
        stock_scores[code]["discovery_score"] += 10

        # 涨幅加分
        try:
            cpct = float(s.get("change_pct", 0))
        except (ValueError, TypeError):
            cpct = 0
        if 0 < cpct < 5:
            stock_scores[code]["discovery_score"] += 5

        # 北向资金加分
        try:
            nn = float(s.get("north_net", 0))
        except (ValueError, TypeError):
            nn = 0
        if nn > 10000:  # 净买入>1亿
            stock_scores[code]["discovery_score"] += 15

        # 新增：连涨加分（每只只加一次）
        if (code in strong_set) and (not stock_scores[code].get("_bonus_strong")):
            stock_scores[code]["discovery_score"] += 10
            stock_scores[code]["_bonus_strong"] = True

        # 新增：机构增持加分（每只只加一次）
        if (code in inst_set) and (not stock_scores[code].get("_bonus_inst")):
            stock_scores[code]["discovery_score"] += 15
            stock_scores[code]["_bonus_inst"] = True

        # 新增：AI基础设施研究加分（投资偏好，+20分）
        if (code in ai_infra_map) and (not stock_scores[code].get("_bonus_ai_infra")):
            infra_data = ai_infra_map[code]
            ai_score = infra_data.get("ai_infra_score", 0)
            # 基础加分20，高共识(3/3)额外+5，高AI评分(>=9)额外+5
            bonus = 20
            if "3/3" in str(infra_data.get("ai_infra_consensus", "")):
                bonus += 5
            if ai_score >= 9:
                bonus += 5
            stock_scores[code]["discovery_score"] += bonus
            stock_scores[code]["ai_infra_category"] = infra_data.get("ai_infra_category", "")
            stock_scores[code]["ai_infra_reason"] = infra_data.get("ai_infra_reason", "")
            stock_scores[code]["_bonus_ai_infra"] = True

    # 清理内部字段
    for v in stock_scores.values():
        v.pop("_bonus_strong", None)
        v.pop("_bonus_inst", None)
        v.pop("_bonus_ai_infra", None)

    # 排序
    ranked = sorted(stock_scores.values(), key=lambda x: x["discovery_score"], reverse=True)

    # smoke-test helper: simulate discovery=0
    if FORCE_DISCOVERY_EMPTY:
        print("[test] FORCE_DISCOVERY_EMPTY=1 -> ranked forced empty")
        ranked = []

    data_dir = BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    discovered_path = data_dir / "discovered_stocks.json"
    last_good_path = data_dir / "discovered_stocks_last_good.json"

    fallback_used: Optional[str] = None

    # --- P0 兜底链路：last_good(<=3交易日且<=M次) -> watchlist -> block ---
    if not ranked:
        last_good = _load_json(last_good_path) or {}
        lg_picks = last_good.get("top_picks") or []
        lg_reuse = int(last_good.get("reuse_count") or 0)
        lg_at = last_good.get("discovered_at") or ""

        ok_date = False
        if lg_at:
            try:
                lg_dt = datetime.fromisoformat(lg_at)
                ok_date = lg_dt >= _trading_day_threshold(LAST_GOOD_MAX_TRADING_DAYS)
            except Exception:
                ok_date = False

        if lg_picks and ok_date and lg_reuse < LAST_GOOD_MAX_REUSE:
            fallback_used = "last_good"
            ranked = lg_picks
            last_good["reuse_count"] = lg_reuse + 1
            last_good.setdefault("reuse_history", []).append(datetime.now().isoformat())
            _save_json(last_good_path, last_good)
            print(f"🚨 [P0] stock_discovery=0 -> fallback=last_good (reuse_count={last_good['reuse_count']}/{LAST_GOOD_MAX_REUSE}, discovered_at={lg_at})")
        else:
            # fallback: watchlist
            watchlist = _load_json(BASE_DIR / "watchlist.json") or {"stocks": []}
            wl = watchlist.get("stocks") or []
            if wl:
                fallback_used = "watchlist"
                ranked = [{
                    "code": str(s.get("code", "")).zfill(6),
                    "name": s.get("name", ""),
                    "price": s.get("latest_price", 0),
                    "change_pct": s.get("change_pct", 0),
                    "market_cap": 0,
                    "discovery_score": 0,
                    "sources": ["watchlist_fallback"],
                } for s in wl][:20]
                print(f"🚨 [P0] stock_discovery=0 -> fallback=watchlist (n={len(ranked)})")

    if not ranked:
        # Still empty: hard block + clear alarm
        print("🚨🚨 [P0] stock_discovery=0：last_good/watchlist 均为空或不可用，已阻断下游（需要人工介入修复数据源）")

    # Assemble result and persist
    result = {
        "discovered_at": datetime.now().isoformat(),
        "total_scanned": len(all_stocks),
        "quality_stocks": len(ranked),
        "fallback_used": fallback_used,
        "top_picks": ranked[:20]
    }

    _save_json(discovered_path, result)

    # Maintain last_good when we have a non-empty discovery (not from fallback)
    if result.get("top_picks") and not fallback_used:
        last_good_blob = {
            **result,
            "reuse_count": 0,
            "reuse_history": [],
        }
        _save_json(last_good_path, last_good_blob)

    if len(result.get("top_picks") or []) == 0:
        print("🚨 [P0] stock_discovery=0：本次发现结果为空（数据源可能异常），后续将阻断 watchlist 更新/新标的流转")
    else:
        print(f"✅ 发现 {len(result.get('top_picks') or [])} 只候选 (fallback={fallback_used or 'none'})")

    return result


def update_watchlist_from_discovery():
    """根据发现结果更新关注列表"""
    # 加载现有关注列表
    watchlist_file = BASE_DIR / "watchlist.json"
    if watchlist_file.exists():
        with open(watchlist_file, 'r') as f:
            watchlist = json.load(f)
    else:
        watchlist = {"stocks": []}

    existing_codes = {s["code"] for s in watchlist.get("stocks", [])}

    # 加载发现结果
    discovered_file = BASE_DIR / "data" / "discovered_stocks.json"
    if not discovered_file.exists():
        discover_stocks()

    with open(discovered_file, 'r') as f:
        discovered = json.load(f)

    # P0: stock_discovery=0 阻断 —— 发现为空则不更新watchlist，避免后续新标的流转
    top_picks = discovered.get("top_picks") or []
    if not top_picks:
        print("🚨 [P0] stock_discovery=0：discover_stocks() 返回为空，已阻断 watchlist 更新")
        return {
            "added": [],
            "total_watchlist": len(watchlist.get("stocks", []) or []),
            "blocked": True,
        }

    # 添加新发现的股票(最多保持20只)
    added = []
    for stock in discovered.get("top_picks", [])[:10]:
        if stock["code"] not in existing_codes and len(watchlist["stocks"]) < 20:
            watchlist["stocks"].append({
                "code": stock["code"],
                "name": stock["name"],
                "market": "SH" if stock["code"].startswith("6") else "SZ",
                "latest_price": stock.get("price"),
                "price_date": datetime.now().strftime("%Y-%m-%d"),
                "change_pct": stock.get("change_pct"),
                "reason": ", ".join(stock.get("sources", [])),
                "priority": "A" if stock["discovery_score"] >= 30 else "B",
                "added_at": datetime.now().isoformat()
            })
            added.append(stock["name"])

    watchlist["last_updated"] = datetime.now().isoformat()

    with open(watchlist_file, 'w') as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)

    return {
        "added": added,
        "total_watchlist": len(watchlist["stocks"])
    }


if __name__ == "__main__":
    result = discover_stocks()

    print("\n📊 Top 10 发现:")
    for i, s in enumerate(result["top_picks"][:10], 1):
        print(f"{i}. {s['name']}({s['code']}) ¥{s['price']} {s['change_pct']:+.2f}%")
        print(f"   来源: {', '.join(s['sources'])} | 分数: {s['discovery_score']}")

    print("\n更新关注列表...")
    update = update_watchlist_from_discovery()
    print(f"新增: {update['added']}")
    print(f"关注列表总数: {update['total_watchlist']}")
