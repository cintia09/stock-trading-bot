#!/usr/bin/env python3
"""
股票数据获取模块 - 从多个数据源获取A股实时和历史数据
支持数据源: 东方财富、新浪财经、BaoStock (自动回退)
"""

import json
import requests
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

# BaoStock 导入（延迟加载避免不必要的连接）
_bs = None
_bs_logged_in = False

def _get_baostock():
    """延迟加载 baostock 并登录"""
    global _bs, _bs_logged_in
    if _bs is None:
        import baostock as bs
        _bs = bs
    if not _bs_logged_in:
        lg = _bs.login()
        if lg.error_code == '0':
            _bs_logged_in = True
    return _bs

def _logout_baostock():
    """登出 baostock"""
    global _bs_logged_in
    if _bs and _bs_logged_in:
        _bs.logout()
        _bs_logged_in = False

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 新浪财经API
SINA_REALTIME_URL = "https://hq.sinajs.cn/list="
# 东方财富API
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 腾讯财经API
TENCENT_REALTIME_URL = "https://qt.gtimg.cn/q="

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn"
}

def get_stock_code_with_market(code: str) -> tuple:
    """根据股票代码判断市场并返回带市场前缀的代码"""
    code = str(code).zfill(6)
    if code.startswith(('60', '68', '11', '51')):
        return f"sh{code}", f"1.{code}"
    else:
        return f"sz{code}", f"0.{code}"

def fetch_realtime_sina(codes: list) -> dict:
    """从新浪获取实时行情"""
    result = {}
    sina_codes = [get_stock_code_with_market(c)[0] for c in codes]
    
    try:
        url = SINA_REALTIME_URL + ",".join(sina_codes)
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = 'gbk'
        
        for line in resp.text.strip().split('\n'):
            if not line or '=' not in line:
                continue
            match = re.search(r'hq_str_(\w+)="(.+)"', line)
            if not match:
                continue
            
            code_with_market = match.group(1)
            data = match.group(2).split(',')
            
            if len(data) < 32:
                continue
            
            code = code_with_market[2:]  # 去掉sh/sz
            result[code] = {
                "name": data[0],
                "open": float(data[1]) if data[1] else 0,
                "pre_close": float(data[2]) if data[2] else 0,
                "price": float(data[3]) if data[3] else 0,
                "high": float(data[4]) if data[4] else 0,
                "low": float(data[5]) if data[5] else 0,
                "volume": int(float(data[8])) if data[8] else 0,  # 成交量(股)
                "amount": float(data[9]) if data[9] else 0,  # 成交额(元)
                "bid1_vol": int(float(data[10])) if data[10] else 0,
                "bid1": float(data[11]) if data[11] else 0,
                "ask1_vol": int(float(data[14])) if data[14] else 0,
                "ask1": float(data[15]) if data[15] else 0,
                "date": data[30],
                "time": data[31],
                "timestamp": datetime.now().isoformat()
            }
            
            # 计算涨跌幅
            if result[code]["pre_close"] > 0 and result[code]["price"] > 0:
                result[code]["change_pct"] = round(
                    (result[code]["price"] - result[code]["pre_close"]) / result[code]["pre_close"] * 100, 2
                )
            else:
                result[code]["change_pct"] = 0
                
    except Exception as e:
        print(f"新浪数据获取失败: {e}")
    
    return result

def fetch_kline_eastmoney(code: str, period: str = "101", limit: int = 120, retries: int = 3) -> list:
    """
    从东方财富获取K线数据
    period: 101=日K, 102=周K, 103=月K, 60=60分钟, 30=30分钟, 15=15分钟, 5=5分钟
    """
    _, em_code = get_stock_code_with_market(code)
    
    params = {
        "secid": em_code,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": period,
        "fqt": "1",  # 前复权
        "lmt": limit,
        "end": "20500101",
        "_": int(time.time() * 1000)
    }
    
    for attempt in range(retries):
        try:
            time.sleep(0.3 * (attempt + 1))  # 递增延时
            resp = requests.get(EASTMONEY_KLINE_URL, params=params, timeout=15, headers=HEADERS)
            data = resp.json()
            
            if data.get("data") and data["data"].get("klines"):
                klines = []
                for line in data["data"]["klines"]:
                    parts = line.split(",")
                    klines.append({
                        "date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": int(float(parts[5])),
                        "amount": float(parts[6]),
                        "amplitude": float(parts[7]),  # 振幅
                        "change_pct": float(parts[8]),  # 涨跌幅
                        "change": float(parts[9]),  # 涨跌额
                        "turnover": float(parts[10])  # 换手率
                    })
                return klines
        except Exception as e:
            if attempt == retries - 1:
                print(f"东方财富K线获取失败 {code}: {e}")
    
    return []

def fetch_kline_baostock(code: str, limit: int = 120) -> list:
    """
    从 BaoStock 获取日K线数据（备用数据源）
    免费、稳定、无需注册
    """
    try:
        bs = _get_baostock()
        
        # 转换代码格式: 601899 -> sh.601899
        code = str(code).zfill(6)
        if code.startswith(('60', '68', '11', '51')):
            bs_code = f"sh.{code}"
        else:
            bs_code = f"sz.{code}"
        
        # 计算日期范围
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=limit * 2)).strftime('%Y-%m-%d')
        
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"  # 前复权
        )
        
        if rs.error_code != '0':
            print(f"BaoStock查询失败 {code}: {rs.error_msg}")
            return []
        
        klines = []
        while rs.next():
            row = rs.get_row_data()
            if len(row) >= 9 and row[4]:  # 确保有收盘价
                try:
                    klines.append({
                        "date": row[0],
                        "open": float(row[1]) if row[1] else 0,
                        "high": float(row[2]) if row[2] else 0,
                        "low": float(row[3]) if row[3] else 0,
                        "close": float(row[4]) if row[4] else 0,
                        "volume": int(float(row[5])) if row[5] else 0,
                        "amount": float(row[6]) if row[6] else 0,
                        "turnover": float(row[7]) if row[7] else 0,
                        "change_pct": float(row[8]) if row[8] else 0,
                        "amplitude": 0,  # baostock 不提供振幅
                        "change": 0  # 需要计算
                    })
                except (ValueError, IndexError):
                    continue
        
        # 限制返回数量
        return klines[-limit:] if len(klines) > limit else klines
        
    except Exception as e:
        print(f"BaoStock K线获取失败 {code}: {e}")
        return []

def fetch_kline(code: str, period: str = "101", limit: int = 120) -> list:
    """
    获取K线数据 - 自动回退机制
    先尝试东方财富，失败后自动切换到 BaoStock
    """
    # 首先尝试东方财富
    klines = fetch_kline_eastmoney(code, period=period, limit=limit, retries=2)
    
    if klines:
        return klines
    
    # 东方财富失败，尝试 BaoStock（仅支持日K）
    if period == "101":  # 日K
        print(f"  -> 切换到 BaoStock 获取 {code}")
        klines = fetch_kline_baostock(code, limit=limit)
        if klines:
            return klines
    
    print(f"  所有数据源均失败: {code}")
    return []

def fetch_market_overview() -> dict:
    """获取大盘指数"""
    indices = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
        "sh000016": "上证50",
        "sh000300": "沪深300",
        "sh000905": "中证500"
    }
    
    result = {}
    try:
        codes = list(indices.keys())
        url = SINA_REALTIME_URL + ",".join(codes)
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = 'gbk'
        
        for line in resp.text.strip().split('\n'):
            if not line or '=' not in line:
                continue
            match = re.search(r'hq_str_(\w+)="(.+)"', line)
            if not match:
                continue
            
            code = match.group(1)
            data = match.group(2).split(',')
            
            if code in indices and len(data) >= 4:
                price = float(data[1]) if data[1] else 0
                pre_close = float(data[2]) if data[2] else 0
                # 计算涨跌幅
                if pre_close > 0:
                    change_pct = round((price - pre_close) / pre_close * 100, 2)
                else:
                    change_pct = 0
                result[code] = {
                    "name": indices[code],
                    "price": price,
                    "pre_close": pre_close,
                    "change_pct": change_pct,
                    "volume": float(data[8]) if len(data) > 8 and data[8] else 0,
                    "amount": float(data[9]) if len(data) > 9 and data[9] else 0
                }
    except Exception as e:
        print(f"大盘指数获取失败: {e}")
    
    return result

def fetch_hot_stocks() -> list:
    """获取热门股票（涨幅榜、成交额榜）"""
    hot_list = []
    
    # 东方财富涨幅榜
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": 20,
        "po": 1,  # 1=降序
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",  # 按涨跌幅排序
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # A股
        "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18"
    }
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("data") and data["data"].get("diff"):
            for item in data["data"]["diff"][:20]:
                hot_list.append({
                    "code": item.get("f12", ""),
                    "name": item.get("f14", ""),
                    "price": item.get("f2", 0),
                    "change_pct": item.get("f3", 0),
                    "volume": item.get("f5", 0),
                    "amount": item.get("f6", 0),
                    "amplitude": item.get("f7", 0),
                    "high": item.get("f15", 0),
                    "low": item.get("f16", 0),
                    "open": item.get("f17", 0),
                    "pre_close": item.get("f18", 0)
                })
    except Exception as e:
        print(f"热门股票获取失败: {e}")
    
    return hot_list

def save_data(filename: str, data: dict):
    """保存数据到JSON文件"""
    filepath = DATA_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"数据已保存: {filepath}")

def load_data(filename: str) -> dict:
    """从JSON文件加载数据"""
    filepath = DATA_DIR / filename
    if filepath.exists():
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

if __name__ == "__main__":
    # 测试代码
    print("=" * 50)
    print("获取大盘指数...")
    market = fetch_market_overview()
    for code, info in market.items():
        print(f"  {info['name']}: {info['price']} ({info['change_pct']}%)")
    
    print("\n获取实时行情...")
    codes = ["601899", "600519", "002594", "601318", "002475"]
    realtime = fetch_realtime_sina(codes)
    for code, info in realtime.items():
        print(f"  {info['name']}({code}): {info['price']} ({info['change_pct']}%)")
    
    print("\n获取K线数据 (紫金矿业)...")
    klines = fetch_kline_eastmoney("601899", limit=10)
    for k in klines[-5:]:
        print(f"  {k['date']}: 收盘{k['close']} 涨跌{k['change_pct']}%")
    
    print("\n获取热门股票...")
    hot = fetch_hot_stocks()
    for s in hot[:5]:
        print(f"  {s['name']}({s['code']}): {s['price']} ({s['change_pct']}%)")
