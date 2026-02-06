#!/usr/bin/env python3
"""
新闻与舆情分析模块 - 获取财经新闻并分析情绪
"""

import requests
import re
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict

BASE_DIR = Path(__file__).parent.parent
NEWS_DIR = BASE_DIR / "news"
NEWS_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# 情绪词典
POSITIVE_WORDS = [
    "上涨", "大涨", "涨停", "飙升", "暴涨", "突破", "新高", "利好", "增长", "盈利",
    "超预期", "强势", "反弹", "回暖", "看好", "推荐", "买入", "增持", "提升", "扩张",
    "创新高", "放量", "主力", "资金流入", "北向买入", "机构加仓", "业绩大增", "订单增长",
    "政策支持", "重大突破", "技术领先", "市场份额", "龙头"
]

NEGATIVE_WORDS = [
    "下跌", "大跌", "跌停", "暴跌", "破位", "新低", "利空", "亏损", "减持", "抛售",
    "业绩下滑", "弱势", "跳水", "回调", "看空", "卖出", "减仓", "下调", "收缩", "萎缩",
    "资金流出", "北向卖出", "机构减持", "业绩爆雷", "订单下滑", "监管处罚", "诉讼", "违规"
]

SECTOR_KEYWORDS = {
    "贵金属": ["黄金", "白银", "贵金属", "金价", "避险"],
    "新能源车": ["新能源", "电动车", "锂电", "充电桩", "比亚迪", "特斯拉"],
    "AI": ["人工智能", "AI", "大模型", "算力", "芯片", "GPU", "英伟达"],
    "消费": ["消费", "白酒", "茅台", "零售", "旅游", "免税"],
    "银行": ["银行", "金融", "利率", "降息", "存款"],
    "光伏": ["光伏", "太阳能", "硅料", "组件"],
    "医药": ["医药", "医疗", "创新药", "集采"],
    "房地产": ["房地产", "地产", "楼市", "房价", "住房"]
}

def fetch_eastmoney_news(limit: int = 50) -> List[Dict]:
    """获取东方财富财经新闻"""
    news_list = []
    
    # 东方财富7x24快讯
    url = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": "",
        "sortEnd": "",
        "pageSize": limit,
        "req_trace": str(int(datetime.now().timestamp() * 1000))
    }
    
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("fastNewsList"):
            for item in data["data"]["fastNewsList"]:
                news_list.append({
                    "title": item.get("title", ""),
                    "content": item.get("digest", ""),
                    "time": item.get("showTime", ""),
                    "source": "东方财富",
                    "url": f"https://finance.eastmoney.com/a/{item.get('code', '')}.html"
                })
    except Exception as e:
        print(f"东方财富新闻获取失败: {e}")
    
    return news_list

def fetch_sina_news(limit: int = 30) -> List[Dict]:
    """获取新浪财经新闻"""
    news_list = []
    
    url = "https://feed.mix.sina.com.cn/api/roll/get"
    params = {
        "pageid": 153,
        "lid": 2516,
        "k": "",
        "num": limit,
        "page": 1
    }
    
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        
        if data.get("result") and data["result"].get("data"):
            for item in data["result"]["data"]:
                news_list.append({
                    "title": item.get("title", ""),
                    "content": item.get("intro", ""),
                    "time": item.get("ctime", ""),
                    "source": "新浪财经",
                    "url": item.get("url", "")
                })
    except Exception as e:
        print(f"新浪财经新闻获取失败: {e}")
    
    return news_list

def analyze_sentiment(text: str) -> Dict:
    """分析文本情绪"""
    if not text:
        return {"score": 0, "label": "neutral", "positive": [], "negative": []}
    
    positive_found = []
    negative_found = []
    
    for word in POSITIVE_WORDS:
        if word in text:
            positive_found.append(word)
    
    for word in NEGATIVE_WORDS:
        if word in text:
            negative_found.append(word)
    
    score = len(positive_found) - len(negative_found)
    
    if score > 2:
        label = "very_positive"
    elif score > 0:
        label = "positive"
    elif score < -2:
        label = "very_negative"
    elif score < 0:
        label = "negative"
    else:
        label = "neutral"
    
    return {
        "score": score,
        "label": label,
        "positive": list(set(positive_found)),
        "negative": list(set(negative_found))
    }

def extract_stock_mentions(text: str, stock_dict: Dict[str, str]) -> List[str]:
    """提取新闻中提到的股票"""
    mentioned = []
    for code, name in stock_dict.items():
        if name in text or code in text:
            mentioned.append(code)
    return mentioned

def identify_sectors(text: str) -> List[str]:
    """识别新闻涉及的板块"""
    sectors = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                sectors.append(sector)
                break
    return sectors

def analyze_news_batch(news_list: List[Dict], stock_dict: Dict[str, str] = None) -> Dict:
    """批量分析新闻"""
    if stock_dict is None:
        stock_dict = {}
    
    overall_sentiment = 0
    sector_sentiment = {sector: {"count": 0, "score": 0} for sector in SECTOR_KEYWORDS}
    stock_mentions = {}
    important_news = []
    
    for news in news_list:
        full_text = news.get("title", "") + " " + news.get("content", "")
        
        # 情绪分析
        sentiment = analyze_sentiment(full_text)
        overall_sentiment += sentiment["score"]
        
        # 板块识别
        sectors = identify_sectors(full_text)
        for sector in sectors:
            sector_sentiment[sector]["count"] += 1
            sector_sentiment[sector]["score"] += sentiment["score"]
        
        # 股票提及
        if stock_dict:
            mentioned = extract_stock_mentions(full_text, stock_dict)
            for code in mentioned:
                if code not in stock_mentions:
                    stock_mentions[code] = {"count": 0, "sentiment": 0, "news": []}
                stock_mentions[code]["count"] += 1
                stock_mentions[code]["sentiment"] += sentiment["score"]
                stock_mentions[code]["news"].append(news["title"])
        
        # 重要新闻(高情绪分)
        if abs(sentiment["score"]) >= 2:
            important_news.append({
                "title": news["title"],
                "sentiment": sentiment,
                "sectors": sectors,
                "time": news.get("time", "")
            })
    
    # 计算板块情绪均值
    for sector in sector_sentiment:
        if sector_sentiment[sector]["count"] > 0:
            sector_sentiment[sector]["avg_score"] = round(
                sector_sentiment[sector]["score"] / sector_sentiment[sector]["count"], 2
            )
        else:
            sector_sentiment[sector]["avg_score"] = 0
    
    # 排序板块
    hot_sectors = sorted(
        [(s, d["count"], d["avg_score"]) for s, d in sector_sentiment.items() if d["count"] > 0],
        key=lambda x: x[1],
        reverse=True
    )
    
    return {
        "overall_sentiment": overall_sentiment,
        "overall_label": "positive" if overall_sentiment > 5 else ("negative" if overall_sentiment < -5 else "neutral"),
        "hot_sectors": hot_sectors,
        "stock_mentions": stock_mentions,
        "important_news": important_news[:10],
        "total_news": len(news_list),
        "analyzed_at": datetime.now().isoformat()
    }

def get_market_sentiment() -> Dict:
    """获取综合市场情绪"""
    # 获取新闻
    em_news = fetch_eastmoney_news(50)
    sina_news = fetch_sina_news(30)
    all_news = em_news + sina_news
    
    # 分析
    analysis = analyze_news_batch(all_news)
    
    # 保存
    save_path = NEWS_DIR / f"sentiment_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    
    return analysis

if __name__ == "__main__":
    print("获取财经新闻...")
    sentiment = get_market_sentiment()
    
    print(f"\n总体情绪: {sentiment['overall_label']} (得分: {sentiment['overall_sentiment']})")
    print(f"分析新闻数: {sentiment['total_news']}")
    
    print("\n热门板块:")
    for sector, count, score in sentiment["hot_sectors"][:5]:
        emoji = "🟢" if score > 0 else ("🔴" if score < 0 else "⚪")
        print(f"  {emoji} {sector}: 提及{count}次, 情绪{score:+.1f}")
    
    print("\n重要新闻:")
    for news in sentiment["important_news"][:5]:
        emoji = "📈" if news["sentiment"]["score"] > 0 else "📉"
        print(f"  {emoji} {news['title'][:40]}...")
