#!/usr/bin/env python3
"""A股特色因子单元测试"""
import sys
sys.path.insert(0, "/root/.openclaw/workspace/stock-trading/scripts")

from china_factors import get_consecutive_limit_up, get_margin_trading_change, score_china_factors

def test_limit_up():
    """测试连板检测 - 用真实数据"""
    print("=" * 50)
    print("测试连板因子")
    print("=" * 50)
    
    # 测试几只股票
    test_codes = [
        ("600519", "贵州茅台 - 大盘股，通常无连板"),
        ("000001", "平安银行 - 大盘股"),
    ]
    
    for code, desc in test_codes:
        result = get_consecutive_limit_up(code)
        print(f"\n{desc} ({code}):")
        print(f"  连板天数: {result['consecutive_days']}")
        print(f"  今日涨停: {result['is_limit_up_today']}")


def test_margin():
    """测试融资融券数据"""
    print("\n" + "=" * 50)
    print("测试融资融券因子")
    print("=" * 50)
    
    test_codes = ["600519", "000001"]
    for code in test_codes:
        result = get_margin_trading_change(code)
        print(f"\n{code}:")
        print(f"  融资变化率: {result['margin_change_pct']}%")
        print(f"  数据源: {result['source']}")


def test_scoring():
    """测试综合打分"""
    print("\n" + "=" * 50)
    print("测试综合打分")
    print("=" * 50)
    
    test_codes = ["600519", "000001", "300750"]
    for code in test_codes:
        result = score_china_factors(code)
        print(f"\n{code}:")
        print(f"  因子加减分: {result['score']:+d}")
        print(f"  原因: {result['reasons']}")
        print(f"  详情: {result['details']}")


def test_scoring_logic():
    """测试打分逻辑正确性（mock数据）"""
    print("\n" + "=" * 50)
    print("测试打分逻辑（mock）")
    print("=" * 50)
    
    from unittest.mock import patch
    
    # 测试首板次日 +8分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 1, 'is_limit_up_today': False}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': None, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == 8, f"首板次日应+8, got {r['score']}"
            print("✅ 首板次日: +8分")
    
    # 测试2连板 +5分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 2, 'is_limit_up_today': True}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': None, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == 5, f"2连板应+5, got {r['score']}"
            print("✅ 2连板: +5分")
    
    # 测试3连板 -15分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 3, 'is_limit_up_today': True}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': None, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == -15, f"3连板应-15, got {r['score']}"
            print("✅ 3连板: -15分")
    
    # 测试5连板 -15分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 5, 'is_limit_up_today': True}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': None, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == -15, f"5连板应-15, got {r['score']}"
            print("✅ 5连板: -15分")
    
    # 测试融资增长>5% +10分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 0, 'is_limit_up_today': False}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': 8.5, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == 10, f"融资增长应+10, got {r['score']}"
            print("✅ 融资增长>5%: +10分")
    
    # 测试融资下降>5% -10分
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 0, 'is_limit_up_today': False}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': -7.2, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == -10, f"融资下降应-10, got {r['score']}"
            print("✅ 融资下降>5%: -10分")
    
    # 测试组合：首板+融资增长
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 1, 'is_limit_up_today': False}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': 6.0, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == 18, f"首板+融资应+18, got {r['score']}"
            print("✅ 首板+融资增长: +18分")
    
    # 测试组合：3连板+融资下降（最危险）
    with patch('china_factors.get_consecutive_limit_up', return_value={'consecutive_days': 3, 'is_limit_up_today': True}):
        with patch('china_factors.get_margin_trading_change', return_value={'margin_change_pct': -8.0, 'source': 'mock'}):
            r = score_china_factors("test")
            assert r['score'] == -25, f"3连板+融资下降应-25, got {r['score']}"
            print("✅ 3连板+融资下降: -25分（追高+杠杆撤退）")
    
    print("\n✅ 所有mock测试通过！")


if __name__ == "__main__":
    # 先跑逻辑测试（无网络依赖）
    test_scoring_logic()
    
    # 再跑真实数据测试
    print("\n\n" + "=" * 60)
    print("以下为真实数据测试（需要网络）")
    print("=" * 60)
    test_limit_up()
    test_margin()
    test_scoring()
