#!/usr/bin/env python3
"""
可转债套利扫描器 v2
- 从东方财富获取可转债基础数据+正股价格
- 计算转股价值、溢价率、套利空间
- 输出到 data/cb_opportunities.json 供看板展示
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
OUTPUT_FILE = BASE_DIR / "data" / "cb_opportunities.json"
DASHBOARD_DIR = Path(__file__).parent.parent.parent / "dashboard"


def fetch_cb_list():
    """从东方财富获取已上市可转债列表（精简字段，快速）"""
    all_items = []
    url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'

    for page in range(1, 8):
        params = {
            'reportName': 'RPT_BOND_CB_LIST',
            'columns': 'SECURITY_CODE,SECUCODE,TRADE_MARKET,SECURITY_NAME_ABBR,'
                       'LISTING_DATE,DELIST_DATE,CONVERT_STOCK_CODE,RATING,'
                       'ACTUAL_ISSUE_SCALE,INITIAL_TRANSFER_PRICE,TRANSFER_PRICE,'
                       'TRANSFER_START_DATE,CEASE_DATE,SECURITY_SHORT_NAME,'
                       'CONVERT_STOCK_PRICE,TRANSFER_VALUE,TRANSFER_PREMIUM_RATIO,'
                       'CURRENT_BOND_PRICENEW',
            'pageSize': '200',
            'pageNumber': str(page),
            'sortColumns': 'PUBLIC_START_DATE',
            'sortTypes': '-1',
            'source': 'WEB',
            'client': 'WEB',
        }
        try:
            r = requests.get(url, params=params, timeout=12)
            data = r.json()
            if not data.get('result') or not data['result'].get('data'):
                break
            items = data['result']['data']
            all_items.extend(items)
            print(f"  第{page}页: {len(items)}条 (累计{len(all_items)})")
            if len(items) < 200:
                break
        except Exception as e:
            print(f"  ⚠️ 第{page}页失败: {e}")
            break
        time.sleep(0.5)

    # 过滤：已上市 + 有转股价 + 未退市
    listed = []
    for item in all_items:
        if not item.get('LISTING_DATE'):
            continue
        tp = item.get('TRANSFER_PRICE') or item.get('INITIAL_TRANSFER_PRICE')
        if not tp:
            continue
        if item.get('DELIST_DATE'):
            continue
        listed.append(item)

    print(f"📊 共 {len(all_items)} 只，{len(listed)} 只已上市有效")
    return listed


def get_sina_bond_code(security_code, trade_market):
    """转换可转债代码为新浪格式"""
    if trade_market == 'CNSESH':
        return f'sh{security_code}'
    return f'sz{security_code}'


def get_sina_stock_code(stock_code):
    """转换股票代码为新浪格式"""
    if stock_code.startswith(('6', '9')):
        return f'sh{stock_code}'
    elif stock_code.startswith('68'):
        return f'sh{stock_code}'
    return f'sz{stock_code}'


def fetch_sina_batch(codes, max_retries=2):
    """新浪行情批量查询，带重试"""
    results = {}
    batch_size = 40
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        code_str = ','.join(batch)
        for attempt in range(max_retries):
            try:
                r = requests.get(
                    f'https://hq.sinajs.cn/list={code_str}',
                    headers={'Referer': 'https://finance.sina.com.cn'},
                    timeout=8
                )
                r.encoding = 'gbk'
                for line in r.text.strip().split('\n'):
                    if '="' not in line:
                        continue
                    eq = line.index('="')
                    code = line[len('var hq_str_'):eq]
                    val = line[eq + 2:].rstrip('";')
                    if val:
                        fields = val.split(',')
                        results[code] = fields
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"  ⚠️ 新浪行情失败({len(batch)}条): {e}")
                time.sleep(1)
        time.sleep(0.3)
    return results


def scan(cb_list):
    """扫描套利机会"""
    # 先尝试用新浪拿实时价格
    bond_map = {}
    stock_set = set()
    for item in cb_list:
        sc = item['SECURITY_CODE']
        mkt = item.get('TRADE_MARKET', '')
        sina_bond = get_sina_bond_code(sc, mkt)
        bond_map[sina_bond] = item
        stk = item.get('CONVERT_STOCK_CODE', '')
        if stk:
            stock_set.add(get_sina_stock_code(stk))

    all_codes = list(bond_map.keys()) + list(stock_set)
    print(f"🔍 查询 {len(bond_map)} 转债 + {len(stock_set)} 正股 行情...")
    quotes = fetch_sina_batch(all_codes)
    print(f"  获取到 {len(quotes)} 条行情")

    opportunities = []
    for sina_bond, item in bond_map.items():
        sc = item['SECURITY_CODE']
        stk_code = item.get('CONVERT_STOCK_CODE', '')
        transfer_price = item.get('TRANSFER_PRICE') or item.get('INITIAL_TRANSFER_PRICE')
        if not transfer_price:
            continue
        transfer_price = float(transfer_price)
        if transfer_price <= 0:
            continue

        # 可转债价格：优先新浪实时，其次东财
        bond_price = None
        bq = quotes.get(sina_bond)
        if bq and len(bq) > 3:
            try:
                p = float(bq[3])
                if p > 0:
                    bond_price = p
                elif float(bq[2]) > 0:
                    bond_price = float(bq[2])
            except:
                pass
        if not bond_price:
            bp = item.get('CURRENT_BOND_PRICENEW')
            if bp:
                try:
                    bond_price = float(bp)
                except:
                    pass
        if not bond_price or bond_price <= 0:
            continue

        # 正股价格：优先新浪，其次东财
        stock_price = None
        sina_stk = get_sina_stock_code(stk_code) if stk_code else None
        if sina_stk:
            sq = quotes.get(sina_stk)
            if sq and len(sq) > 3:
                try:
                    p = float(sq[3])
                    if p > 0:
                        stock_price = p
                    elif float(sq[2]) > 0:
                        stock_price = float(sq[2])
                except:
                    pass
        if not stock_price:
            sp = item.get('CONVERT_STOCK_PRICE')
            if sp:
                try:
                    stock_price = float(sp)
                except:
                    pass
        if not stock_price or stock_price <= 0:
            continue

        # 计算指标
        convert_value = (100.0 / transfer_price) * stock_price
        premium_rate = ((bond_price - convert_value) / convert_value) * 100.0

        rating = (item.get('RATING') or '').replace('sti', '').strip()
        remaining = float(item.get('ACTUAL_ISSUE_SCALE', 0) or 0)

        years_left = 0
        expire = item.get('CEASE_DATE', '')
        if expire:
            try:
                years_left = round((datetime.strptime(expire[:10], '%Y-%m-%d') - datetime.now()).days / 365, 1)
            except:
                pass

        can_convert = False
        cs = item.get('TRANSFER_START_DATE', '')
        if cs:
            try:
                can_convert = datetime.now() >= datetime.strptime(cs[:10], '%Y-%m-%d')
            except:
                pass

        # 正股涨跌幅
        stock_chg = None
        if sina_stk:
            sq = quotes.get(sina_stk)
            if sq and len(sq) > 3:
                try:
                    prev = float(sq[2])
                    curr = float(sq[3])
                    if prev > 0 and curr > 0:
                        stock_chg = round((curr - prev) / prev * 100, 2)
                except:
                    pass

        # 套利策略分类
        strategy = ''
        score = 0
        if premium_rate < -2 and can_convert:
            strategy = '负溢价转股套利'
            score = min(100, abs(premium_rate) * 10)
        elif premium_rate < 0 and can_convert:
            strategy = '低溢价套利'
            score = min(80, abs(premium_rate) * 8)
        elif bond_price < 100 and premium_rate < 15:
            strategy = '低价低溢价'
            score = max(0, 60 - premium_rate * 2)
        elif bond_price < 105 and premium_rate < 5 and can_convert:
            strategy = '面值附近低溢价'
            score = max(0, 50 - premium_rate * 3)
        elif premium_rate < 3 and can_convert:
            strategy = '极低溢价'
            score = max(0, 40 - premium_rate * 5)
        elif bond_price < 95:
            strategy = '深度折价'
            score = max(0, 45 - bond_price / 5)
        else:
            continue

        opportunities.append({
            'bond_name': item.get('SECURITY_NAME_ABBR', ''),
            'bond_code': sc,
            'stock_name': item.get('SECURITY_SHORT_NAME', ''),
            'stock_code': stk_code,
            'bond_price': round(bond_price, 3),
            'stock_price': round(stock_price, 2),
            'stock_chg': stock_chg,
            'transfer_price': round(transfer_price, 2),
            'convert_value': round(convert_value, 3),
            'premium_rate': round(premium_rate, 2),
            'rating': rating,
            'remaining_scale': round(remaining, 2),
            'years_left': years_left,
            'can_convert': can_convert,
            'strategy': strategy,
            'score': round(score, 1),
        })

    opportunities.sort(key=lambda x: x['score'], reverse=True)
    return opportunities


def main():
    print("=" * 50)
    print("🔄 可转债套利扫描器 v2")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    cb_list = fetch_cb_list()
    if not cb_list:
        print("❌ 获取失败")
        sys.exit(1)

    opportunities = scan(cb_list)

    # 保存
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    result = {
        'scan_time': datetime.now().isoformat(),
        'total_listed': len(cb_list),
        'opportunities_found': len(opportunities),
        'opportunities': opportunities[:30],
    }
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 找到 {len(opportunities)} 个机会，TOP30 已保存")
    for i, op in enumerate(opportunities[:10], 1):
        print(f"  {i}. {op['bond_name']} 价格{op['bond_price']:.2f} "
              f"溢价{op['premium_rate']:.1f}% [{op['strategy']}] "
              f"评分{op['score']}")


if __name__ == '__main__':
    main()
