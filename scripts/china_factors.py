#!/usr/bin/env python3
"""A股特色因子模块 - 连板因子 & 融资融券因子

提供:
- get_consecutive_limit_up(): 计算连续涨停天数
- get_margin_trading_change(): 获取融资余额变化率
- score_china_factors(): 综合打分入口
"""

import baostock as bs
from datetime import datetime, timedelta
from typing import Dict, Optional
import functools

# 缓存baostock登录状态
_bs_logged_in = False

def _ensure_bs_login():
    global _bs_logged_in
    if not _bs_logged_in:
        bs.login()
        _bs_logged_in = True


def _to_bs_code(code: str) -> str:
    """转换股票代码为baostock格式: 000001 -> sh.000001 / sz.000001"""
    code = code.replace("sh.", "").replace("sz.", "").replace("SH.", "").replace("SZ.", "").zfill(6)
    if code.startswith(("6", "9")):
        return f"sh.{code}"
    else:
        return f"sz.{code}"


def get_consecutive_limit_up(code: str, end_date: str = None, lookback: int = 10) -> Dict:
    """计算连续涨停天数
    
    Args:
        code: 股票代码 (如 '000001' 或 'sh.000001')
        end_date: 截止日期 YYYY-MM-DD，默认今天
        lookback: 回看天数
    
    Returns:
        {
            'consecutive_days': int,  # 连续涨停天数（0表示非涨停状态）
            'is_limit_up_today': bool,  # 今天是否涨停
        }
    """
    _ensure_bs_login()
    
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")
    bs_code = _to_bs_code(code)
    
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,close,preclose,pctChg,isST",
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3"  # 不复权
    )
    
    rows = []
    while (rs.error_code == '0') and rs.next():
        rows.append(rs.get_row_data())
    
    if not rows:
        return {'consecutive_days': 0, 'is_limit_up_today': False}
    
    # 判断涨停：涨幅>=9.8%（考虑ST为5%，但isST字段可能不准，用9.8%通用阈值）
    consecutive = 0
    is_limit_up_today = False
    
    for i in range(len(rows) - 1, -1, -1):
        try:
            pct = float(rows[i][3]) if rows[i][3] else 0
            close = float(rows[i][1]) if rows[i][1] else 0
            preclose = float(rows[i][2]) if rows[i][2] else 0
            
            # ST股涨停阈值5%，普通股10%（用9.8%/4.8%容差）
            is_st = rows[i][4] == '1' if len(rows[i]) > 4 and rows[i][4] else False
            limit_threshold = 4.8 if is_st else 9.8
            
            if preclose > 0:
                actual_pct = (close - preclose) / preclose * 100
            else:
                actual_pct = pct
            
            if actual_pct >= limit_threshold:
                consecutive += 1
                if i == len(rows) - 1:
                    is_limit_up_today = True
            else:
                break
        except (ValueError, IndexError):
            break
    
    return {
        'consecutive_days': consecutive,
        'is_limit_up_today': is_limit_up_today,
    }


def get_margin_trading_change(code: str, end_date: str = None, period: int = 5) -> Dict:
    """获取融资余额变化率
    
    Args:
        code: 股票代码
        end_date: 截止日期
        period: 计算周期（天）
    
    Returns:
        {
            'margin_change_pct': float or None,  # 融资余额变化率(%)
            'source': str,  # 数据源
        }
    """
    # 先尝试AKShare（BaoStock融资融券接口不稳定）
    result = _get_margin_akshare(code, end_date, period)
    if result['margin_change_pct'] is not None:
        return result
    
    # 再尝试BaoStock
    result = _get_margin_baostock(code, end_date, period)
    return result


def _get_margin_akshare(code: str, end_date: str = None, period: int = 5) -> Dict:
    """通过AKShare获取融资融券数据
    
    策略：只查最近一天的明细，对比期初。因为逐日查询太慢，
    这里做简化：取最近交易日和5天前交易日两天的数据对比。
    """
    try:
        import akshare as ak
        from datetime import datetime, timedelta
        
        pure_code = code.replace("sh.", "").replace("sz.", "").replace("SH.", "").replace("SZ.", "").zfill(6)
        is_sh = pure_code.startswith(("6", "9"))
        
        if end_date is None:
            end_dt = datetime.now()
        else:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        def _query_day(dt, is_sh, pure_code):
            """查询某天的融资余额，尝试最多3天（跳过非交易日）"""
            for offset in range(4):
                d = dt - timedelta(days=offset)
                ds = d.strftime("%Y%m%d")
                try:
                    if is_sh:
                        df = ak.stock_margin_detail_sse(date=ds)
                        code_col = '标的证券代码'
                    else:
                        df = ak.stock_margin_detail_szse(date=ds)
                        code_col = '证券代码' if '证券代码' in df.columns else '标的证券代码'
                    
                    if df is not None and not df.empty:
                        row = df[df[code_col] == pure_code]
                        if not row.empty:
                            return float(row['融资余额'].iloc[0])
                except Exception:
                    continue
            return None
        
        recent = _query_day(end_dt, is_sh, pure_code)
        past = _query_day(end_dt - timedelta(days=period + 2), is_sh, pure_code)  # +2 for weekends
        
        if recent is None or past is None or past == 0:
            return {'margin_change_pct': None, 'source': 'akshare_insufficient'}
        
        change_pct = (recent - past) / past * 100
        return {'margin_change_pct': round(change_pct, 2), 'source': 'akshare'}
    
    except Exception:
        return {'margin_change_pct': None, 'source': 'akshare_error'}


def _get_margin_baostock(code: str, end_date: str = None, period: int = 5) -> Dict:
    """通过BaoStock获取融资融券数据"""
    try:
        _ensure_bs_login()
        
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=period * 3)).strftime("%Y-%m-%d")
        bs_code = _to_bs_code(code)
        
        # BaoStock query_margin_trade
        rs = bs.query_stock_industry(code=bs_code)  # placeholder - bs没有直接的融资融券接口
        
        return {'margin_change_pct': None, 'source': 'baostock_unavailable'}
    
    except Exception:
        return {'margin_change_pct': None, 'source': 'baostock_error'}


def score_china_factors(code: str, klines=None, end_date: str = None) -> Dict:
    """A股特色因子综合打分
    
    Returns:
        {
            'score': int,        # 总加减分
            'reasons': list,     # 原因列表
            'details': dict,     # 详细数据
        }
    """
    score = 0
    reasons = []
    details = {}
    
    # === 连板因子 ===
    try:
        limit_info = get_consecutive_limit_up(code, end_date)
        details['limit_up'] = limit_info
        days = limit_info['consecutive_days']
        
        if days == 1:
            # 首板次日效应：+8分
            score += 8
            reasons.append(f"🔥首板次日溢价效应(+8)")
        elif days == 2:
            # 2连板：+5分
            score += 5
            reasons.append(f"🔥2连板强势(+5)")
        elif days >= 3:
            # 3连板及以上：-15分
            score -= 15
            reasons.append(f"⚠️{days}连板追高风险(-15)")
    except Exception as e:
        details['limit_up_error'] = str(e)
    
    # === 融资融券因子 ===
    try:
        margin_info = get_margin_trading_change(code, end_date)
        details['margin'] = margin_info
        change = margin_info.get('margin_change_pct')
        
        if change is not None:
            if change > 5:
                score += 10
                reasons.append(f"💰融资净买入增长{change:.1f}%(+10)")
            elif change < -5:
                score -= 10
                reasons.append(f"⚠️融资净卖出{change:.1f}%(-10)")
            else:
                reasons.append(f"融资变化{change:.1f}%(中性)")
    except Exception as e:
        details['margin_error'] = str(e)
    
    return {
        'score': score,
        'reasons': reasons,
        'details': details,
    }
