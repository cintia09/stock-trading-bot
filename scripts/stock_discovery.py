#!/usr/bin/env python3
"""
股票自动发现模块 - 发现潜力股票并更新关注列表
"""

import json
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict

BASE_DIR = Path(__file__).parent.parent

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

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
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("diff"):
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
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("diff"):
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
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("diff"):
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
                
                member_resp = requests.get(url, params=member_params, timeout=10)
                member_data = member_resp.json()
                
                if member_data.get("data") and member_data["data"].get("diff"):
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
        "fid": "f62",  # 按北向资金排序
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f6,f12,f14,f62,f184,f66"
    }
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("diff"):
            for item in data["data"]["diff"][:10]:
                if item.get("f62", 0) > 0:  # 净买入为正
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
        change_pct = s.get("change_pct", 0)
        if abs(change_pct) >= 9.9:
            continue
        
        # 过滤低价股 (< 5元)
        price = s.get("price", 0)
        if price < 5:
            continue
        
        # 过滤市值过小 (< 100亿)
        market_cap = s.get("market_cap", 0)
        if market_cap > 0 and market_cap < 10000000000:  # 100亿
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
    
    # 过滤
    print("  过滤质量股票...")
    quality = filter_quality_stocks(all_stocks)
    
    # 去重并评分
    stock_scores = {}
    for s in quality:
        code = s["code"]
        if code not in stock_scores:
            stock_scores[code] = {
                **s,
                "discovery_score": 0,
                "sources": []
            }
        
        # 来源越多分数越高
        stock_scores[code]["sources"].append(s.get("source", ""))
        stock_scores[code]["discovery_score"] += 10
        
        # 涨幅加分
        if 0 < s.get("change_pct", 0) < 5:
            stock_scores[code]["discovery_score"] += 5
        
        # 北向资金加分
        if s.get("north_net", 0) > 10000:  # 净买入>1亿
            stock_scores[code]["discovery_score"] += 15
    
    # 排序
    ranked = sorted(stock_scores.values(), key=lambda x: x["discovery_score"], reverse=True)
    
    result = {
        "discovered_at": datetime.now().isoformat(),
        "total_scanned": len(all_stocks),
        "quality_stocks": len(ranked),
        "top_picks": ranked[:20]
    }
    
    # 保存
    with open(BASE_DIR / "data" / "discovered_stocks.json", 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 发现 {len(ranked)} 只优质股票")
    
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
