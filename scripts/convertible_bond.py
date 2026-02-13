#!/usr/bin/env python3
"""
可转债套利监控模块

数据来源：
- 东方财富API: 可转债基本信息（代码、名称、转股价、评级、到期日等）
- 新浪财经API: 可转债实时行情（现价、涨跌幅）

套利策略：
1. 转股套利: 溢价率 < -1%（负溢价），可即时转股获利
2. 双低策略: 价格+溢价率×100 < 130，低价低溢价的安全品种
3. 折价套利: 转债价格 < 100 且到期收益率 > 2%，有债底保护

输出：
- convertible_bonds.json: 全市场可转债数据
- cb_opportunities.json: 套利机会列表（按得分排序）
"""

import json
import requests
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 路径配置
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 输出文件
BONDS_FILE = DATA_DIR / "convertible_bonds.json"
OPPORTUNITIES_FILE = DATA_DIR / "cb_opportunities.json"

# API配置
REQUEST_TIMEOUT = 30
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


class ConvertibleBondFetcher:
    """可转债数据采集器"""
    
    def __init__(self):
        self.bonds: List[Dict] = []
        self.last_update: Optional[str] = None
    
    def fetch_bond_list(self) -> List[Dict]:
        """从东方财富获取可转债列表基本信息"""
        url = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
        all_bonds = []
        
        for page in range(1, 10):  # 最多10页
            params = {
                'reportName': 'RPT_BOND_CB_LIST',
                'columns': 'ALL',
                'quoteColumns': 'f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE',
                'pageSize': '500',
                'pageNumber': str(page),
                'sortColumns': 'PUBLIC_START_DATE',
                'sortTypes': '-1',
                'source': 'WEB',
                'client': 'WEB',
            }
            headers = {
                **REQUEST_HEADERS,
                'Referer': 'https://data.eastmoney.com/kzz/default.html',
            }
            
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
                data = resp.json()
                
                if not data.get('result') or not data['result'].get('data'):
                    break
                
                bonds = data['result']['data']
                all_bonds.extend(bonds)
                
                if len(bonds) < 500:
                    break
                    
            except Exception as e:
                print(f"[ERROR] 获取第{page}页可转债列表失败: {e}")
                break
        
        return all_bonds
    
    def _get_bond_code_for_sina(self, security_code: str, market: str) -> str:
        """转换为新浪接口使用的代码格式"""
        # 沪市可转债代码以11开头，深市以12开头
        if security_code.startswith('11'):
            return f'sh{security_code}'
        elif security_code.startswith('12'):
            return f'sz{security_code}'
        else:
            # 根据市场判断
            if 'SH' in market:
                return f'sh{security_code}'
            else:
                return f'sz{security_code}'
    
    def fetch_realtime_prices(self, bond_codes: List[str]) -> Dict[str, Dict]:
        """从新浪获取可转债实时行情"""
        prices = {}
        
        # 分批获取，每批最多80个
        batch_size = 80
        for i in range(0, len(bond_codes), batch_size):
            batch = bond_codes[i:i+batch_size]
            codes_str = ','.join(batch)
            
            url = f'http://hq.sinajs.cn/list={codes_str}'
            headers = {
                **REQUEST_HEADERS,
                'Referer': 'https://finance.sina.com.cn',
            }
            
            try:
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                resp.encoding = 'gbk'
                
                for line in resp.text.strip().split('\n'):
                    if not line or '=' not in line:
                        continue
                    
                    # 解析: var hq_str_sh110100="名称,今开,昨收,现价,最高,最低,..."
                    parts = line.split('=')
                    code = parts[0].split('_')[-1]  # sh110100
                    data_str = parts[1].strip('"')
                    
                    if not data_str:
                        continue
                    
                    fields = data_str.split(',')
                    if len(fields) < 10:
                        continue
                    
                    # 字段顺序: 名称, 今开, 昨收, 现价, 最高, 最低, 买一价, 卖一价, 成交量, 成交额...
                    try:
                        prices[code] = {
                            'name': fields[0],
                            'open': float(fields[1]) if fields[1] else None,
                            'pre_close': float(fields[2]) if fields[2] else None,
                            'price': float(fields[3]) if fields[3] else None,
                            'high': float(fields[4]) if fields[4] else None,
                            'low': float(fields[5]) if fields[5] else None,
                            'volume': int(fields[8]) if fields[8] else 0,
                            'amount': float(fields[9]) if fields[9] else 0,
                        }
                    except (ValueError, IndexError):
                        continue
                
                time.sleep(0.1)  # 避免请求过快
                
            except Exception as e:
                print(f"[ERROR] 获取实时行情失败 (批次{i//batch_size + 1}): {e}")
        
        return prices
    
    def fetch_stock_prices(self, stock_codes: List[str]) -> Dict[str, float]:
        """从新浪获取正股实时价格"""
        prices = {}
        
        # 转换代码格式
        sina_codes = []
        code_map = {}  # sina_code -> original_code
        for code in stock_codes:
            if code.startswith('6'):
                sina_code = f'sh{code}'
            elif code.startswith(('0', '3')):
                sina_code = f'sz{code}'
            else:
                continue
            sina_codes.append(sina_code)
            code_map[sina_code] = code
        
        # 分批获取
        batch_size = 80
        for i in range(0, len(sina_codes), batch_size):
            batch = sina_codes[i:i+batch_size]
            codes_str = ','.join(batch)
            
            url = f'http://hq.sinajs.cn/list={codes_str}'
            headers = {
                **REQUEST_HEADERS,
                'Referer': 'https://finance.sina.com.cn',
            }
            
            try:
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                resp.encoding = 'gbk'
                
                for line in resp.text.strip().split('\n'):
                    if not line or '=' not in line:
                        continue
                    
                    parts = line.split('=')
                    sina_code = parts[0].split('_')[-1]
                    data_str = parts[1].strip('"')
                    
                    if not data_str:
                        continue
                    
                    fields = data_str.split(',')
                    if len(fields) < 4:
                        continue
                    
                    try:
                        price = float(fields[3]) if fields[3] else None
                        if price and sina_code in code_map:
                            prices[code_map[sina_code]] = price
                    except (ValueError, IndexError):
                        continue
                
                time.sleep(0.1)
                
            except Exception as e:
                print(f"[ERROR] 获取正股价格失败 (批次{i//batch_size + 1}): {e}")
        
        return prices
    
    def calculate_ytm(self, price: float, face_value: float, years_to_maturity: float, 
                      annual_coupon: float = 2.0, redemption_price: float = 110.0) -> float:
        """
        计算到期收益率 (简化版YTM)
        
        Args:
            price: 当前价格
            face_value: 面值 (通常100)
            years_to_maturity: 剩余年限
            annual_coupon: 年票息 (平均约2%)
            redemption_price: 到期赎回价 (通常110)
        
        Returns:
            年化到期收益率 (%)
        """
        if years_to_maturity <= 0 or price <= 0:
            return 0.0
        
        # 简化计算: (到期收益 + 总票息) / 当前投资 / 年限
        total_coupon = annual_coupon * years_to_maturity
        total_return = (redemption_price - price) + total_coupon
        ytm = (total_return / price) / years_to_maturity * 100
        
        return round(ytm, 2)
    
    def fetch_all_data(self) -> List[Dict]:
        """获取并整合所有可转债数据"""
        print("[INFO] 开始获取可转债数据...")
        
        # 1. 获取基本信息
        print("[INFO] 获取可转债基本信息...")
        raw_bonds = self.fetch_bond_list()
        print(f"[INFO] 获取到 {len(raw_bonds)} 只可转债")
        
        # 2. 筛选已上市的可转债
        listed_bonds = [b for b in raw_bonds if b.get('LISTING_DATE')]
        print(f"[INFO] 其中已上市 {len(listed_bonds)} 只")
        
        # 3. 准备代码列表
        bond_code_list = []
        stock_code_set = set()
        
        for bond in listed_bonds:
            code = bond.get('SECURITY_CODE', '')
            market = bond.get('TRADE_MARKET', '')
            sina_code = self._get_bond_code_for_sina(code, market)
            bond['_sina_code'] = sina_code
            bond_code_list.append(sina_code)
            
            stock_code = bond.get('CONVERT_STOCK_CODE')
            if stock_code:
                stock_code_set.add(stock_code)
        
        # 4. 获取可转债实时行情
        print("[INFO] 获取可转债实时行情...")
        bond_prices = self.fetch_realtime_prices(bond_code_list)
        print(f"[INFO] 获取到 {len(bond_prices)} 只转债行情")
        
        # 5. 获取正股实时价格
        print("[INFO] 获取正股实时价格...")
        stock_prices = self.fetch_stock_prices(list(stock_code_set))
        print(f"[INFO] 获取到 {len(stock_prices)} 只正股价格")
        
        # 6. 整合数据
        print("[INFO] 整合数据...")
        processed_bonds = []
        today = date.today()
        
        for bond in listed_bonds:
            sina_code = bond.get('_sina_code', '')
            security_code = bond.get('SECURITY_CODE', '')
            stock_code = bond.get('CONVERT_STOCK_CODE', '')
            
            # 获取实时价格
            price_info = bond_prices.get(sina_code, {})
            current_price = price_info.get('price')
            
            # 获取正股价格
            stock_price = stock_prices.get(stock_code) or bond.get('CONVERT_STOCK_PRICE')
            # 确保是数值类型
            if stock_price:
                try:
                    stock_price = float(stock_price)
                except (ValueError, TypeError):
                    stock_price = None
            
            # 计算转股价值和溢价率
            initial_convert_price = bond.get('INITIAL_TRANSFER_PRICE')
            if initial_convert_price:
                try:
                    initial_convert_price = float(initial_convert_price)
                except (ValueError, TypeError):
                    initial_convert_price = None
            
            transfer_value = None
            premium_ratio = None
            
            if stock_price and initial_convert_price and initial_convert_price > 0:
                # 转股价值 = 正股价格 / 转股价 * 100
                transfer_value = round(stock_price / initial_convert_price * 100, 2)
                
                if current_price and transfer_value > 0:
                    # 溢价率 = (转债价格 - 转股价值) / 转股价值 * 100%
                    premium_ratio = round((current_price - transfer_value) / transfer_value * 100, 2)
            
            # 计算剩余年限
            years_to_maturity = None
            cease_date_str = bond.get('CEASE_DATE')
            if cease_date_str:
                try:
                    cease_date = datetime.strptime(cease_date_str.split()[0], '%Y-%m-%d').date()
                    years_to_maturity = round((cease_date - today).days / 365, 2)
                except:
                    pass
            
            # 计算到期收益率
            ytm = None
            if current_price and years_to_maturity and years_to_maturity > 0:
                ytm = self.calculate_ytm(current_price, 100, years_to_maturity)
            
            # 计算双低值 (价格 + 溢价率*100 的变体，这里用 价格 + 溢价率)
            double_low = None
            if current_price and premium_ratio is not None:
                double_low = round(current_price + premium_ratio, 2)
            
            # 判断是否在转股期
            in_convert_period = False
            transfer_start = bond.get('TRANSFER_START_DATE')
            transfer_end = bond.get('TRANSFER_END_DATE')
            if transfer_start and transfer_end:
                try:
                    start = datetime.strptime(transfer_start.split()[0], '%Y-%m-%d').date()
                    end = datetime.strptime(transfer_end.split()[0], '%Y-%m-%d').date()
                    in_convert_period = start <= today <= end
                except:
                    pass
            
            processed = {
                'code': security_code,
                'name': bond.get('SECURITY_NAME_ABBR', ''),
                'price': current_price,
                'pre_close': price_info.get('pre_close'),
                'change_pct': round((current_price - price_info.get('pre_close', current_price)) / price_info.get('pre_close', 1) * 100, 2) if current_price and price_info.get('pre_close') else None,
                'transfer_value': transfer_value,
                'premium_ratio': premium_ratio,
                'double_low': double_low,
                'ytm': ytm,
                'years_to_maturity': years_to_maturity,
                'rating': bond.get('RATING'),
                'stock_code': stock_code,
                'stock_name': bond.get('CONVERT_STOCK_NAME') or bond.get('SECURITY_SHORT_NAME'),
                'stock_price': stock_price,
                'convert_price': initial_convert_price,
                'listing_date': bond.get('LISTING_DATE', '').split()[0] if bond.get('LISTING_DATE') else None,
                'cease_date': bond.get('CEASE_DATE', '').split()[0] if bond.get('CEASE_DATE') else None,
                'in_convert_period': in_convert_period,
                'issue_scale': bond.get('ACTUAL_ISSUE_SCALE'),  # 发行规模（亿元）
                'volume': price_info.get('volume', 0),
                'amount': price_info.get('amount', 0),
            }
            
            processed_bonds.append(processed)
        
        # 过滤无效数据（没有价格的）
        valid_bonds = [b for b in processed_bonds if b['price'] and b['price'] > 0]
        print(f"[INFO] 有效数据 {len(valid_bonds)} 只")
        
        self.bonds = valid_bonds
        self.last_update = datetime.now().isoformat()
        
        return valid_bonds
    
    def save_data(self):
        """保存数据到JSON文件"""
        data = {
            'update_time': self.last_update,
            'count': len(self.bonds),
            'bonds': self.bonds
        }
        
        with open(BONDS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"[INFO] 数据已保存到 {BONDS_FILE}")


class OpportunityScanner:
    """套利机会扫描器"""
    
    def __init__(self, bonds: List[Dict]):
        self.bonds = bonds
        self.opportunities: List[Dict] = []
    
    def scan_conversion_arbitrage(self) -> List[Dict]:
        """
        扫描转股套利机会
        条件: 溢价率 < -1% (负溢价) 且在转股期内
        """
        opportunities = []
        
        for bond in self.bonds:
            premium = bond.get('premium_ratio')
            if premium is None:
                continue
            
            # 负溢价且在转股期
            if premium < -1.0 and bond.get('in_convert_period'):
                # 计算理论收益
                potential_profit = -premium  # 负溢价率的绝对值就是理论收益
                
                # 评分: 负溢价越大越好，最高30分基础分
                base_score = min(30, potential_profit * 5)
                
                # 评级加分
                rating_bonus = {'AAA': 15, 'AA+': 12, 'AA': 10, 'AA-': 8, 'A+': 5}.get(bond.get('rating'), 0)
                
                # 流动性加分 (成交额>1000万加分)
                liquidity_bonus = min(10, (bond.get('amount', 0) / 10000000) * 5)
                
                score = min(100, base_score + rating_bonus + liquidity_bonus)
                
                opportunities.append({
                    'type': '转股套利',
                    'code': bond['code'],
                    'name': bond['name'],
                    'price': bond['price'],
                    'transfer_value': bond['transfer_value'],
                    'premium_ratio': premium,
                    'potential_profit': round(potential_profit, 2),
                    'rating': bond.get('rating'),
                    'stock_code': bond.get('stock_code'),
                    'stock_name': bond.get('stock_name'),
                    'score': round(score, 1),
                    'suggestion': f"负溢价{abs(premium):.1f}%，可转股套利。风险：转股后T+1才能卖出正股，需承担股价波动风险。",
                })
        
        return opportunities
    
    def scan_double_low(self) -> List[Dict]:
        """
        扫描双低策略机会
        条件: 价格 + 溢价率 < 130 (双低值越低越好)
        """
        opportunities = []
        
        for bond in self.bonds:
            price = bond.get('price')
            premium = bond.get('premium_ratio')
            double_low = bond.get('double_low')
            
            if price is None or premium is None or double_low is None:
                continue
            
            # 双低值 < 130 且价格 < 115 (避免高价转债)
            if double_low < 130 and price < 115:
                # 评分: 双低值越低越好
                base_score = max(0, (130 - double_low) * 2)
                
                # 低价加分
                price_bonus = max(0, (110 - price) * 0.5) if price < 110 else 0
                
                # 评级加分
                rating_bonus = {'AAA': 10, 'AA+': 8, 'AA': 6, 'AA-': 4, 'A+': 2}.get(bond.get('rating'), 0)
                
                # 剩余年限适中加分 (2-4年最佳)
                years = bond.get('years_to_maturity', 0)
                years_bonus = 5 if 2 <= years <= 4 else (3 if 1 <= years <= 5 else 0)
                
                score = min(100, base_score + price_bonus + rating_bonus + years_bonus)
                
                if score >= 30:  # 只保留评分较高的
                    opportunities.append({
                        'type': '双低策略',
                        'code': bond['code'],
                        'name': bond['name'],
                        'price': price,
                        'premium_ratio': premium,
                        'double_low': double_low,
                        'ytm': bond.get('ytm'),
                        'years_to_maturity': bond.get('years_to_maturity'),
                        'rating': bond.get('rating'),
                        'score': round(score, 1),
                        'suggestion': f"双低值{double_low:.1f}，低价低溢价组合。适合中长期持有，等待正股上涨或下修转股价。",
                    })
        
        return opportunities
    
    def scan_discount_arbitrage(self) -> List[Dict]:
        """
        扫描折价套利机会
        条件: 转债价格 < 100 且到期收益率 > 2%
        """
        opportunities = []
        
        for bond in self.bonds:
            price = bond.get('price')
            ytm = bond.get('ytm')
            years = bond.get('years_to_maturity')
            
            if price is None or ytm is None:
                continue
            
            # 价格 < 100 (折价) 且 YTM > 2%
            if price < 100 and ytm > 2.0:
                # 评分: YTM越高越好，折价越大越好
                ytm_score = min(40, ytm * 8)
                discount_score = min(30, (100 - price) * 3)
                
                # 评级加分
                rating_bonus = {'AAA': 15, 'AA+': 12, 'AA': 10, 'AA-': 7, 'A+': 4}.get(bond.get('rating'), 0)
                
                # 年限适中加分
                years_bonus = 5 if years and 1 <= years <= 3 else 0
                
                score = min(100, ytm_score + discount_score + rating_bonus + years_bonus)
                
                opportunities.append({
                    'type': '折价套利',
                    'code': bond['code'],
                    'name': bond['name'],
                    'price': price,
                    'ytm': ytm,
                    'years_to_maturity': years,
                    'rating': bond.get('rating'),
                    'premium_ratio': bond.get('premium_ratio'),
                    'score': round(score, 1),
                    'suggestion': f"价格{price:.2f}，到期收益率{ytm:.1f}%。有债底保护，可持有到期获取稳定收益。",
                })
        
        return opportunities
    
    def scan_all(self) -> List[Dict]:
        """扫描所有套利机会并按得分排序"""
        print("[INFO] 开始扫描套利机会...")
        
        # 分别扫描三种策略
        conversion = self.scan_conversion_arbitrage()
        print(f"[INFO] 转股套利机会: {len(conversion)} 只")
        
        double_low = self.scan_double_low()
        print(f"[INFO] 双低策略机会: {len(double_low)} 只")
        
        discount = self.scan_discount_arbitrage()
        print(f"[INFO] 折价套利机会: {len(discount)} 只")
        
        # 合并并排序
        all_opportunities = conversion + double_low + discount
        all_opportunities.sort(key=lambda x: x['score'], reverse=True)
        
        self.opportunities = all_opportunities
        print(f"[INFO] 总计套利机会: {len(all_opportunities)} 只")
        
        return all_opportunities
    
    def save_opportunities(self):
        """保存套利机会到JSON文件"""
        data = {
            'update_time': datetime.now().isoformat(),
            'count': len(self.opportunities),
            'by_type': {
                '转股套利': len([o for o in self.opportunities if o['type'] == '转股套利']),
                '双低策略': len([o for o in self.opportunities if o['type'] == '双低策略']),
                '折价套利': len([o for o in self.opportunities if o['type'] == '折价套利']),
            },
            'opportunities': self.opportunities
        }
        
        with open(OPPORTUNITIES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"[INFO] 套利机会已保存到 {OPPORTUNITIES_FILE}")


def scan_opportunities() -> List[Dict]:
    """
    扫描可转债套利机会（供外部调用）
    
    Returns:
        套利机会列表，按得分降序排列
    """
    # 获取数据
    fetcher = ConvertibleBondFetcher()
    bonds = fetcher.fetch_all_data()
    fetcher.save_data()
    
    # 扫描机会
    scanner = OpportunityScanner(bonds)
    opportunities = scanner.scan_all()
    scanner.save_opportunities()
    
    return opportunities


def get_bond_summary() -> Dict:
    """
    获取可转债市场概要统计（供外部调用）
    
    Returns:
        {
            'total_count': 总数,
            'opportunity_count': 有机会的数量,
            'avg_price': 平均价格,
            'avg_premium': 平均溢价率,
            'best_opportunities': [前3个最佳机会],
            'update_time': 更新时间
        }
    """
    # 读取已有数据
    bonds_data = {}
    opps_data = {}
    
    if BONDS_FILE.exists():
        with open(BONDS_FILE, 'r', encoding='utf-8') as f:
            bonds_data = json.load(f)
    
    if OPPORTUNITIES_FILE.exists():
        with open(OPPORTUNITIES_FILE, 'r', encoding='utf-8') as f:
            opps_data = json.load(f)
    
    bonds = bonds_data.get('bonds', [])
    opportunities = opps_data.get('opportunities', [])
    
    if not bonds:
        return {
            'total_count': 0,
            'opportunity_count': 0,
            'avg_price': None,
            'avg_premium': None,
            'best_opportunities': [],
            'update_time': None,
            'message': '暂无数据，请先运行 scan_opportunities() 获取数据'
        }
    
    # 计算统计
    prices = [b['price'] for b in bonds if b.get('price')]
    premiums = [b['premium_ratio'] for b in bonds if b.get('premium_ratio') is not None]
    
    return {
        'total_count': len(bonds),
        'opportunity_count': len(opportunities),
        'avg_price': round(sum(prices) / len(prices), 2) if prices else None,
        'avg_premium': round(sum(premiums) / len(premiums), 2) if premiums else None,
        'price_range': {
            'min': round(min(prices), 2) if prices else None,
            'max': round(max(prices), 2) if prices else None,
        },
        'premium_range': {
            'min': round(min(premiums), 2) if premiums else None,
            'max': round(max(premiums), 2) if premiums else None,
        },
        'opportunities_by_type': opps_data.get('by_type', {}),
        'best_opportunities': opportunities[:3] if opportunities else [],
        'update_time': bonds_data.get('update_time'),
    }


def print_opportunities(opportunities: List[Dict], limit: int = 10):
    """打印套利机会"""
    print(f"\n{'='*80}")
    print(f"可转债套利机会 TOP {min(limit, len(opportunities))}")
    print(f"{'='*80}")
    
    for i, opp in enumerate(opportunities[:limit], 1):
        print(f"\n{i}. [{opp['type']}] {opp['name']} ({opp['code']}) - 得分: {opp['score']}")
        print(f"   价格: {opp['price']:.2f}", end='')
        if opp.get('transfer_value'):
            print(f" | 转股价值: {opp['transfer_value']:.2f}", end='')
        if opp.get('premium_ratio') is not None:
            print(f" | 溢价率: {opp['premium_ratio']:.1f}%", end='')
        if opp.get('double_low'):
            print(f" | 双低值: {opp['double_low']:.1f}", end='')
        if opp.get('ytm'):
            print(f" | YTM: {opp['ytm']:.1f}%", end='')
        print()
        if opp.get('rating'):
            print(f"   评级: {opp['rating']}", end='')
        if opp.get('years_to_maturity'):
            print(f" | 剩余年限: {opp['years_to_maturity']:.1f}年", end='')
        print()
        print(f"   建议: {opp['suggestion']}")


if __name__ == "__main__":
    print("=" * 60)
    print("可转债套利监控模块测试")
    print("=" * 60)
    
    # 运行扫描
    opportunities = scan_opportunities()
    
    # 打印结果
    print_opportunities(opportunities, limit=5)
    
    # 打印概要
    print("\n" + "=" * 60)
    print("市场概要")
    print("=" * 60)
    summary = get_bond_summary()
    print(f"可转债总数: {summary['total_count']}")
    print(f"套利机会数: {summary['opportunity_count']}")
    print(f"平均价格: {summary['avg_price']}")
    print(f"平均溢价率: {summary['avg_premium']}%")
    print(f"价格区间: {summary['price_range']}")
    print(f"溢价率区间: {summary['premium_range']}")
    print(f"机会分布: {summary['opportunities_by_type']}")
    print(f"更新时间: {summary['update_time']}")
