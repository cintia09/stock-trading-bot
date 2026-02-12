#!/usr/bin/env python3
"""sentiment_enhanced.py

AI 增强情绪分析模块（轻量版，多维度融合）。

目标：在不引入重依赖/不破坏既有接口的情况下，扩展原有 news_sentiment.py 的情绪能力。

- 复用 news_sentiment.py 的新闻源（东财 7x24 + 新浪滚动）与情绪词典/打分函数
- 新增：东财个股新闻搜索接口（jsonp）
- 新增：市场恐贪指数 Fear & Greed (0-100)
- 新增：个股情绪分 analyze_stock_sentiment ( -10 ~ +10 )

重要：所有函数均做 try/except 保护，任何 API 失败都返回默认值，不崩溃。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple

import requests

# 复用旧模块的数据源与情绪分析能力
try:
    import news_sentiment
except Exception:  # pragma: no cover
    news_sentiment = None

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.eastmoney.com/",
}


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


def _parse_jsonp(text: str) -> Any:
    """解析 JSONP: jQueryxxx({...}) -> dict"""
    try:
        # 找到第一个 '(' 和最后一个 ')'
        l = text.find('(')
        r = text.rfind(')')
        if l == -1 or r == -1 or r <= l:
            return None
        payload = text[l + 1 : r]
        return json.loads(payload)
    except Exception:
        return None


def fetch_stock_news_eastmoney_search(stock_name: str, page_size: int = 10) -> List[Dict]:
    """东财搜索接口获取个股相关新闻（jsonp）。

    URL 模板（需求给定）：
    https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param={...}

    返回字段尽量对齐 news_sentiment 的结构：title/content/time/source/url。
    """
    items: List[Dict] = []
    try:
        if not stock_name:
            return items

        url = "https://search-api-web.eastmoney.com/search/jsonp"
        param = {
            "uid": "",
            "keyword": stock_name,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": int(page_size),
                }
            },
        }
        params = {
            "cb": "jQuery",
            "param": json.dumps(param, ensure_ascii=False),
            "_": int(time.time() * 1000),
        }

        resp = requests.get(url, params=params, headers=HEADERS, timeout=12)
        data = _parse_jsonp(resp.text)
        if not data:
            return items

        # 尝试兼容不同返回结构
        # 实际返回: data["result"]["cmsArticleWebOld"] 直接是列表
        node = None
        for path in [
            ("result", "cmsArticleWebOld"),
            ("result", "cmsArticleWebOld", "data"),
            ("result", "cmsArticleWebOld", "datas"),
            ("result", "data"),
            ("data", "result"),
        ]:
            cur = data
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok:
                node = cur
                break

        # node 可能是 dict(list=[]) 或直接 list
        if isinstance(node, dict):
            lst = node.get("list") or node.get("data") or node.get("datas") or []
        else:
            lst = node or []

        if not isinstance(lst, list):
            return items

        for it in lst:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or it.get("Title") or "").strip()
            content = (it.get("content") or it.get("digest") or it.get("Summary") or "").strip()
            pub = it.get("date") or it.get("publishTime") or it.get("showTime") or it.get("time") or ""
            link = it.get("url") or it.get("link") or it.get("shareurl") or ""
            if not title and not content:
                continue
            items.append(
                {
                    "title": title,
                    "content": content,
                    "time": pub,
                    "source": "东方财富-搜索",
                    "url": link,
                }
            )
    except Exception:
        # 失败直接返回空
        return []

    return items


def analyze_stock_sentiment(code: str, name: str) -> float:
    """分析个股情绪：返回 [-10, +10]。

    数据：东财搜索新闻（stock_name），并复用 news_sentiment.analyze_sentiment。
    聚合：按新闻条目累加得分，缩放并截断到 [-10, 10]。
    """
    try:
        if not name:
            return 0.0

        news = fetch_stock_news_eastmoney_search(name, page_size=10)
        if not news:
            return 0.0

        # 如果旧模块不可用，则退化为简单关键词计数
        total = 0.0
        count = 0
        for n in news:
            text = f"{n.get('title','')} {n.get('content','')}".strip()
            if not text:
                continue
            if news_sentiment and hasattr(news_sentiment, "analyze_sentiment"):
                s = news_sentiment.analyze_sentiment(text)
                total += _safe_float(s.get("score", 0), 0.0)
            else:
                # 极简 fallback
                pos = len(re.findall(r"上涨|利好|增持|突破|新高|主力|资金流入", text))
                neg = len(re.findall(r"下跌|利空|减持|破位|新低|资金流出|违规", text))
                total += float(pos - neg)
            count += 1

        if count <= 0:
            return 0.0

        avg = total / count
        # 缩放：平均每条 1 分左右较常见，做一个平滑增强
        # 将 avg 映射到 [-10, 10]，用 tanh-like 近似（避免依赖 math.tanh 的也可）
        # 这里采用分段压缩：
        raw = avg * 3.0
        score = _clamp(raw, -10.0, 10.0)
        return float(score)
    except Exception:
        return 0.0


def _fetch_market_sample(pages: int = 3, page_size: int = 500) -> Tuple[int, int, int, int, float]:
    """抓取市场样本（A股）用于涨跌家数、涨停跌停与成交额。

    返回: (up_cnt, down_cnt, limit_up_cnt, limit_down_cnt, total_amount)
    amount 采用字段 f6（成交额），东财单位通常为元或金额（接口常为元），本函数仅用于相对比较。
    """
    up = down = limit_up = limit_down = 0
    total_amount = 0.0

    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        fields = "f3,f6,f12,f14"  # 涨跌幅、成交额、代码、名称
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

        for pn in range(1, int(pages) + 1):
            params = {
                "pn": pn,
                "pz": int(page_size),
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": fields,
                "_": int(time.time() * 1000),
            }
            resp = requests.get(url, params=params, timeout=12, headers=HEADERS)
            data = resp.json()
            diff = ((data.get("data") or {}).get("diff")) or []
            if not isinstance(diff, list) or not diff:
                break

            for it in diff:
                cpct = _safe_float(it.get("f3"), 0.0)
                amt = _safe_float(it.get("f6"), 0.0)
                total_amount += max(0.0, amt)

                if cpct > 0:
                    up += 1
                elif cpct < 0:
                    down += 1

                if cpct >= 9.9:
                    limit_up += 1
                elif cpct <= -9.9:
                    limit_down += 1

        return up, down, limit_up, limit_down, float(total_amount)
    except Exception:
        return 0, 0, 0, 0, 0.0


def _load_fg_history() -> Dict:
    try:
        fp = DATA_DIR / "fear_greed_history.json"
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"amount_history": []}


def _save_fg_history(obj: Dict) -> None:
    try:
        fp = DATA_DIR / "fear_greed_history.json"
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        return


def calculate_fear_greed() -> Dict:
    """计算市场恐贪指数 Fear & Greed（0-100）。

    维度与权重：
    - 涨跌比（上涨家数/下跌家数） 0.25
    - 涨停跌停比 0.15
    - 成交额 vs 近 5 日均值（量比） 0.20
    - 新闻情绪得分 0.20
    - 北向资金净流入方向 0.20

    返回 dict：{"score": int, "components": {...}, "computed_at": ...}
    """
    try:
        weights = {
            "advance_decline": 0.25,
            "limit_up_down": 0.15,
            "amount_ratio": 0.20,
            "news_sentiment": 0.20,
            "northbound": 0.20,
        }

        up, down, lu, ld, total_amount = _fetch_market_sample(pages=3, page_size=500)

        # 1) 涨跌比 -> 0~100
        ad_ratio = (up / max(1, down)) if (up + down) > 0 else 1.0
        # ad_ratio=1 -> 50；>1 偏贪；<1 偏恐
        ad_score = _clamp(50 + (ad_ratio - 1.0) * 25, 0, 100)

        # 2) 涨停跌停比
        lud_ratio = (lu / max(1, ld)) if (lu + ld) > 0 else 1.0
        lud_score = _clamp(50 + (lud_ratio - 1.0) * 30, 0, 100)

        # 3) 成交额量比：今日总成交额 vs 最近 5 次记录均值
        hist = _load_fg_history()
        ah = hist.get("amount_history", []) if isinstance(hist, dict) else []
        if not isinstance(ah, list):
            ah = []

        # 写入本次 amount（按日期去重）
        today = datetime.now().strftime("%Y-%m-%d")
        ah = [x for x in ah if isinstance(x, dict) and x.get("date") != today]
        ah.append({"date": today, "amount": total_amount})
        ah = ah[-10:]
        hist["amount_history"] = ah
        _save_fg_history(hist)

        last5 = [x.get("amount", 0.0) for x in ah[-5:] if isinstance(x, dict)]
        avg5 = sum(last5) / max(1, len(last5))
        amt_ratio = (total_amount / avg5) if avg5 > 0 else 1.0
        # ratio=1 -> 50; 0.7 -> 35; 1.3 -> 65
        amt_score = _clamp(50 + (amt_ratio - 1.0) * 50, 0, 100)

        # 4) 新闻情绪得分：复用 news_sentiment.get_market_sentiment overall_sentiment
        news_score = 50.0
        overall_raw = 0.0
        if news_sentiment and hasattr(news_sentiment, "get_market_sentiment"):
            try:
                ms = news_sentiment.get_market_sentiment()
                overall_raw = _safe_float(ms.get("overall_sentiment", 0.0), 0.0)
                # overall_raw 大致可能在 [-30,30] 波动，裁剪映射
                overall_clip = _clamp(overall_raw, -20, 20)
                news_score = (overall_clip + 20) / 40 * 100
            except Exception:
                news_score = 50.0

        # 5) 北向资金方向：使用 clist/get 拉取 f62，取净和符号
        north_score = 50.0
        north_net = 0.0
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
                "fields": "f12,f14,f62",
                "_": int(time.time() * 1000),
            }
            resp = requests.get(url, params=params, timeout=12, headers=HEADERS)
            data = resp.json()
            diff = ((data.get("data") or {}).get("diff")) or []
            for it in diff:
                north_net += _safe_float(it.get("f62"), 0.0)

            # 仅用方向：正=偏贪、负=偏恐
            if north_net > 0:
                north_score = 70.0
            elif north_net < 0:
                north_score = 30.0
            else:
                north_score = 50.0
        except Exception:
            north_score = 50.0

        score = (
            ad_score * weights["advance_decline"]
            + lud_score * weights["limit_up_down"]
            + amt_score * weights["amount_ratio"]
            + news_score * weights["news_sentiment"]
            + north_score * weights["northbound"]
        )

        result = {
            "score": int(round(_clamp(score, 0, 100))),
            "components": {
                "advance_decline": {
                    "up": up,
                    "down": down,
                    "ratio": round(ad_ratio, 3),
                    "score": round(ad_score, 2),
                    "weight": weights["advance_decline"],
                },
                "limit_up_down": {
                    "limit_up": lu,
                    "limit_down": ld,
                    "ratio": round(lud_ratio, 3),
                    "score": round(lud_score, 2),
                    "weight": weights["limit_up_down"],
                },
                "amount_ratio": {
                    "today_amount": round(total_amount, 2),
                    "avg5_amount": round(avg5, 2),
                    "ratio": round(amt_ratio, 3),
                    "score": round(amt_score, 2),
                    "weight": weights["amount_ratio"],
                },
                "news_sentiment": {
                    "overall_sentiment": overall_raw,
                    "score": round(news_score, 2),
                    "weight": weights["news_sentiment"],
                },
                "northbound": {
                    "net": round(north_net, 2),
                    "score": round(north_score, 2),
                    "weight": weights["northbound"],
                },
            },
            "computed_at": datetime.now().isoformat(),
        }

        return result
    except Exception:
        return {
            "score": 50,
            "components": {},
            "computed_at": datetime.now().isoformat(),
            "error": "calculate_failed",
        }


if __name__ == "__main__":
    fg = calculate_fear_greed()
    print("Fear&Greed:", fg.get("score"), fg.get("components", {}).get("amount_ratio", {}))
    print("Stock sentiment demo:", analyze_stock_sentiment("000001", "平安银行"))
