#!/usr/bin/env python3
"""
深度复盘引擎 v2 - 真正的 5-Why 根因分析
像 PR Review 一样层层追问，找到根本原因
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_realtime_sina, fetch_market_overview, fetch_kline

BASE_DIR = Path(__file__).parent.parent
REVIEW_DIR = BASE_DIR / "reviews"
REVIEW_DIR.mkdir(exist_ok=True)

# 板块映射（简化版）
SECTOR_MAP = {
    "601318": "保险",
    "600036": "银行", 
    "300896": "医美",
    "000333": "家电",
    "300144": "旅游",
}

class DeepReviewV2:
    """真正的 5-Why 根因分析"""
    
    def __init__(self):
        self.account_file = BASE_DIR / "account.json"
        self.params_file = BASE_DIR / "strategy_params.json"
        
    def load_json(self, path: Path) -> dict:
        if path.exists():
            with open(path, 'r') as f:
                return json.load(f)
        return {}
    
    def save_json(self, path: Path, data: dict):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_market_context(self) -> Dict:
        """获取大盘和市场环境"""
        market = fetch_market_overview()
        
        sh = market.get("sh000001", {})
        sz = market.get("sz399001", {})
        cyb = market.get("sz399006", {})
        
        # 判断市场状态
        sh_pct = sh.get("change_pct", 0)
        
        if sh_pct < -2:
            mood = "恐慌下跌"
            risk = "高"
        elif sh_pct < -1:
            mood = "明显回调"
            risk = "中高"
        elif sh_pct < -0.3:
            mood = "小幅下跌"
            risk = "中"
        elif sh_pct < 0.3:
            mood = "横盘震荡"
            risk = "低"
        elif sh_pct < 1:
            mood = "小幅上涨"
            risk = "低"
        elif sh_pct < 2:
            mood = "明显上涨"
            risk = "低"
        else:
            mood = "大涨行情"
            risk = "注意追高"
        
        return {
            "上证指数": {"price": sh.get("price", 0), "pct": sh_pct},
            "深证成指": {"price": sz.get("price", 0), "pct": sz.get("change_pct", 0)},
            "创业板指": {"price": cyb.get("price", 0), "pct": cyb.get("change_pct", 0)},
            "mood": mood,
            "risk": risk,
            "is_down_day": sh_pct < -0.5,
            "is_crash": sh_pct < -2,
        }

    def analyze_why_chain(self, code: str, name: str, 
                          cost: float, current: float,
                          market: Dict) -> Dict:
        """
        真正的 5-Why 链式分析
        每个 Why 都是对上一个答案的追问
        """
        
        pnl_pct = (current - cost) / cost * 100
        is_loss = pnl_pct < 0
        direction = "跌" if is_loss else "涨"
        
        sector = SECTOR_MAP.get(code, "未知")
        market_pct = market["上证指数"]["pct"]
        
        # 获取K线做技术分析
        klines = fetch_kline(code, limit=20)
        
        # 计算技术指标
        if klines and len(klines) >= 10:
            ma5 = sum(k["close"] for k in klines[-5:]) / 5
            ma10 = sum(k["close"] for k in klines[-10:]) / 10
            recent_5d_change = sum(k["change_pct"] for k in klines[-5:])
            vol_avg = sum(k["volume"] for k in klines[-6:-1]) / 5
            vol_today = klines[-1]["volume"]
            vol_ratio = vol_today / vol_avg if vol_avg > 0 else 1
        else:
            ma5 = ma10 = current
            recent_5d_change = 0
            vol_ratio = 1
        
        # === 构建 5-Why 链 ===
        chain = []
        root_cause = ""
        lesson = ""
        action = ""
        strategy_fix = None
        
        # ----- Why 1: 表面现象 -----
        if is_loss:
            if abs(pnl_pct) > abs(market_pct) + 0.5:
                why1 = f"今日{direction}了{abs(pnl_pct):.1f}%，跌幅超过大盘({market_pct:.1f}%)，表现偏弱"
                relative = "弱于大盘"
            elif abs(pnl_pct) < abs(market_pct) - 0.3:
                why1 = f"今日{direction}了{abs(pnl_pct):.1f}%，跌幅小于大盘({market_pct:.1f}%)，相对抗跌"
                relative = "抗跌"
            else:
                why1 = f"今日{direction}了{abs(pnl_pct):.1f}%，基本跟随大盘({market_pct:.1f}%)"
                relative = "跟随"
        else:
            if pnl_pct > market_pct + 0.5:
                why1 = f"今日{direction}了{pnl_pct:.1f}%，涨幅超过大盘({market_pct:.1f}%)，表现强势"
                relative = "强于大盘"
            else:
                why1 = f"今日{direction}了{pnl_pct:.1f}%，跟随大盘走势"
                relative = "跟随"
        
        chain.append({
            "level": 1,
            "question": f"为什么{name}今天{direction}了{abs(pnl_pct):.1f}%？",
            "answer": why1
        })
        
        # ----- Why 2: 追问原因 -----
        if relative == "弱于大盘":
            if sector in ["旅游", "消费", "医美"]:
                why2 = f"因为{sector}板块整体承压，资金从进攻转向防御"
                sector_issue = True
            else:
                why2 = f"可能有个股利空或资金主动撤离，需关注是否有负面消息"
                sector_issue = False
        elif relative == "抗跌":
            if sector in ["银行", "保险"]:
                why2 = f"因为{sector}属于防御板块，大盘下跌时资金会流入避险"
                sector_issue = False
            else:
                why2 = f"说明有资金护盘或有利好支撑，相对安全"
                sector_issue = False
        elif relative == "强于大盘":
            why2 = f"说明有独立行情，可能有利好消息或资金主动买入"
            sector_issue = False
        else:  # 跟随
            why2 = f"没有独立利好/利空，随大盘波动属于正常现象"
            sector_issue = False
        
        chain.append({
            "level": 2,
            "question": f"为什么{relative}？",
            "answer": why2
        })
        
        # ----- Why 3: 继续追问 -----
        if market["is_down_day"]:
            if sector_issue:
                why3 = f"市场情绪偏弱({market['mood']})，叠加{sector}板块本身缺乏催化剂，双重压力"
            else:
                why3 = f"今日市场整体{market['mood']}，系统性风险释放中"
        else:
            if is_loss:
                why3 = f"大盘没跌但个股下跌，说明是个股问题而非系统风险"
            else:
                why3 = f"市场情绪正常，个股走势符合预期"
        
        chain.append({
            "level": 3,
            "question": "市场环境对此有什么影响？",
            "answer": why3
        })
        
        # ----- Why 4: 技术面验证 -----
        if current > ma5 > ma10:
            tech_status = "多头排列"
            tech_ok = True
        elif current < ma5 < ma10:
            tech_status = "空头排列"
            tech_ok = False
        else:
            tech_status = "趋势不明"
            tech_ok = None
        
        if vol_ratio > 1.5:
            vol_status = "放量"
        elif vol_ratio < 0.7:
            vol_status = "缩量"
        else:
            vol_status = "量能正常"
        
        if is_loss and not tech_ok:
            why4 = f"技术面{tech_status}，{vol_status}，说明下跌趋势可能延续，买入时机选择有问题"
            timing_issue = True
        elif is_loss and tech_ok:
            why4 = f"技术面仍是{tech_status}，{vol_status}，下跌可能是短期回调，趋势未破"
            timing_issue = False
        elif not is_loss and tech_ok:
            why4 = f"技术面{tech_status}，{vol_status}，上涨有技术支撑，买入逻辑正确"
            timing_issue = False
        else:
            why4 = f"技术面{tech_status}，{vol_status}，需要继续观察"
            timing_issue = None
        
        chain.append({
            "level": 4,
            "question": "技术面支持这个走势吗？",
            "answer": why4
        })
        
        # ----- Why 5: 根本原因 + 教训 -----
        if is_loss:
            if timing_issue:
                why5 = f"**根本原因**：买入时机不对。在{tech_status}或高位追涨，忽略了技术面风险"
                root_cause = "择时问题"
                lesson = f"教训：买入前必须确认技术面趋势，{sector}板块在当前市场环境下需要更谨慎"
                action = "考虑减仓或设置更紧的止损"
                strategy_fix = {"rule": "择时", "fix": "增加均线过滤，价格需站上5日线才能买入"}
            elif sector_issue:
                why5 = f"**根本原因**：板块选择问题。{sector}板块在当前市场风格下不受青睐"
                root_cause = "选股问题"
                lesson = f"教训：需要关注市场风格切换，当前资金偏好防御，应减少{sector}配置"
                action = "等待板块回暖信号，或逢高减仓换股"
                strategy_fix = {"rule": "板块配置", "fix": f"市场下跌时减少{sector}等进攻板块配置"}
            elif market["is_crash"]:
                why5 = f"**根本原因**：系统性风险。大盘大跌，覆巢之下无完卵"
                root_cause = "系统风险"
                lesson = "教训：需要加强大盘风控，跌幅>1%时暂停操作或减仓"
                action = "等待大盘企稳"
                strategy_fix = {"rule": "大盘风控", "fix": "上证跌>1%时暂停买入，跌>2%时考虑减仓"}
            else:
                why5 = f"**根本原因**：正常波动。亏损在可接受范围内，继续观察"
                root_cause = "正常波动"
                lesson = "继续持有观察，严格执行止损纪律"
                action = "持有，设好止损"
        else:
            if pnl_pct > 5:
                why5 = f"**根本原因**：买入逻辑正确，盈利丰厚"
                root_cause = "操作正确"
                lesson = f"成功经验：{sector}板块+正确的技术择时，可以复制这个模式"
                action = "考虑止盈减仓锁定利润"
            else:
                why5 = f"**根本原因**：持仓正确，小幅盈利"
                root_cause = "正常盈利"
                lesson = "继续持有，等待更大空间"
                action = "持有"
        
        chain.append({
            "level": 5,
            "question": "根本原因是什么？我学到了什么？",
            "answer": why5
        })
        
        return {
            "code": code,
            "name": name,
            "sector": sector,
            "pnl_pct": pnl_pct,
            "relative": relative,
            "tech_status": tech_status,
            "chain": chain,
            "root_cause": root_cause,
            "lesson": lesson,
            "action": action,
            "strategy_fix": strategy_fix
        }

    def run_review(self) -> str:
        """运行复盘"""
        
        # 获取数据
        market = self.get_market_context()
        account = self.load_json(self.account_file)
        holdings = account.get("holdings", [])
        
        if not holdings:
            return "# 无持仓\n\n当前无持仓，无需复盘。"
        
        codes = [h["code"] for h in holdings]
        prices = fetch_realtime_sina(codes)
        
        # 分析每只股票
        analyses = []
        for h in holdings:
            code = h["code"]
            current = prices.get(code, {}).get("price", h["cost_price"])
            analysis = self.analyze_why_chain(
                code, h["name"], h["cost_price"], current, market
            )
            analyses.append(analysis)
        
        # 生成报告
        return self.generate_report(market, analyses)
    
    def generate_report(self, market: Dict, analyses: List[Dict]) -> str:
        """生成报告"""
        
        lines = []
        today = datetime.now().strftime("%Y-%m-%d")
        
        lines.append(f"# 📊 5-Why 深度复盘 | {today}")
        lines.append("")
        
        # 大盘
        lines.append("## 📈 市场环境")
        lines.append(f"- 上证: {market['上证指数']['price']:.0f} ({market['上证指数']['pct']:+.2f}%)")
        lines.append(f"- 情绪: **{market['mood']}** | 风险: **{market['risk']}**")
        lines.append("")
        
        # 每只股票的 5-Why
        lines.append("## 🔍 持仓 5-Why 分析")
        lines.append("")
        
        for a in analyses:
            emoji = "🟢" if a["pnl_pct"] >= 0 else "🔴"
            lines.append(f"### {emoji} {a['name']} ({a['sector']})")
            lines.append(f"盈亏: **{a['pnl_pct']:+.2f}%** | {a['relative']} | {a['tech_status']}")
            lines.append("")
            
            for c in a["chain"]:
                lines.append(f"**Why {c['level']}: {c['question']}**")
                lines.append(f"> {c['answer']}")
                lines.append("")
            
            lines.append(f"📝 **教训**: {a['lesson']}")
            lines.append(f"🎯 **行动**: {a['action']}")
            lines.append("")
            lines.append("---")
            lines.append("")
        
        # 策略调整汇总
        fixes = [a["strategy_fix"] for a in analyses if a["strategy_fix"]]
        if fixes:
            lines.append("## 🔧 策略调整")
            for f in fixes:
                lines.append(f"- **{f['rule']}**: {f['fix']}")
            lines.append("")
        
        # 总结
        lines.append("## 📋 总结")
        losers = [a for a in analyses if a["pnl_pct"] < 0]
        winners = [a for a in analyses if a["pnl_pct"] >= 0]
        lines.append(f"- 盈利: {len(winners)}只 | 亏损: {len(losers)}只")
        
        root_causes = [a["root_cause"] for a in analyses if a["root_cause"]]
        if root_causes:
            from collections import Counter
            cause_counts = Counter(root_causes)
            main_cause = cause_counts.most_common(1)[0][0]
            lines.append(f"- 主要问题: **{main_cause}**")
        
        lines.append("")
        
        return "\n".join(lines)


def main():
    engine = DeepReviewV2()
    report = engine.run_review()
    print(report)
    
    # 保存
    today = datetime.now().strftime("%Y-%m-%d")
    report_file = REVIEW_DIR / f"5why_review_{today}.md"
    with open(report_file, 'w') as f:
        f.write(report)


if __name__ == "__main__":
    main()
