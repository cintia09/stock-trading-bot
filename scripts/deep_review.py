#!/usr/bin/env python3
"""
深度复盘引擎 - 5-Why 分析 + 个股涨跌原因 + 策略调整
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from fetch_stock_data import fetch_realtime_sina, fetch_market_overview, fetch_kline

BASE_DIR = Path(__file__).parent.parent
REVIEW_DIR = BASE_DIR / "reviews"
REVIEW_DIR.mkdir(exist_ok=True)


class DeepReviewEngine:
    """深度复盘引擎"""
    
    def __init__(self):
        self.account_file = BASE_DIR / "account.json"
        self.transactions_file = BASE_DIR / "transactions.json"
        self.params_file = BASE_DIR / "strategy_params.json"
        self.watchlist_file = BASE_DIR / "watchlist.json"
        
    def load_json(self, path: Path) -> dict:
        if path.exists():
            with open(path, 'r') as f:
                return json.load(f)
        return {}
    
    def save_json(self, path: Path, data: dict):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def get_market_context(self) -> Dict:
        """获取大盘环境"""
        market = fetch_market_overview()
        
        context = {
            "indices": {},
            "market_sentiment": "neutral",
            "trend": "unknown"
        }
        
        for code, data in market.items():
            context["indices"][data["name"]] = {
                "price": data["price"],
                "change_pct": data["change_pct"]
            }
        
        # 判断市场情绪
        sh_change = market.get("sh000001", {}).get("change_pct", 0)
        if sh_change > 1:
            context["market_sentiment"] = "bullish"
            context["trend"] = "上涨"
        elif sh_change < -1:
            context["market_sentiment"] = "bearish"
            context["trend"] = "下跌"
        elif sh_change > 0:
            context["market_sentiment"] = "slightly_bullish"
            context["trend"] = "小幅上涨"
        elif sh_change < 0:
            context["market_sentiment"] = "slightly_bearish"
            context["trend"] = "小幅下跌"
        else:
            context["market_sentiment"] = "neutral"
            context["trend"] = "横盘"
        
        return context
    
    def analyze_stock_movement(self, code: str, name: str, 
                                current_price: float, cost_price: float,
                                market_context: Dict) -> Dict:
        """
        分析个股涨跌原因 - 5-Why 分析法
        """
        pnl_pct = (current_price - cost_price) / cost_price * 100
        is_up = pnl_pct > 0
        
        analysis = {
            "code": code,
            "name": name,
            "current_price": current_price,
            "cost_price": cost_price,
            "pnl_pct": pnl_pct,
            "direction": "上涨" if is_up else "下跌",
            "five_why": [],
            "factors": {
                "market": None,      # 大盘因素
                "sector": None,      # 板块因素
                "individual": None,  # 个股因素
                "technical": None,   # 技术面
                "capital": None      # 资金面
            },
            "lessons": [],
            "action_suggestion": None
        }
        
        # 获取K线数据分析技术面
        klines = fetch_kline(code, limit=20)
        
        # === 5-Why 分析 ===
        
        # Why 1: 今天为什么涨/跌？
        market_change = market_context["indices"].get("上证指数", {}).get("change_pct", 0)
        
        if is_up:
            if market_change > 0.5:
                why1 = f"今日上涨{pnl_pct:.1f}%，大盘上涨{market_change:.1f}%带动"
                analysis["factors"]["market"] = "正向"
            else:
                why1 = f"今日上涨{pnl_pct:.1f}%，逆势走强，有独立行情"
                analysis["factors"]["market"] = "独立"
        else:
            if market_change < -0.5:
                why1 = f"今日下跌{abs(pnl_pct):.1f}%，跟随大盘下跌{abs(market_change):.1f}%"
                analysis["factors"]["market"] = "跟随"
            else:
                why1 = f"今日下跌{abs(pnl_pct):.1f}%，弱于大盘，有个股问题"
                analysis["factors"]["market"] = "弱势"
        
        analysis["five_why"].append({"level": 1, "question": "今天为什么涨/跌？", "answer": why1})
        
        # Why 2: 大盘/个股为什么这样走？
        if klines and len(klines) >= 5:
            recent_trend = sum(k["change_pct"] for k in klines[-5:])
            vol_ratio = klines[-1]["volume"] / (sum(k["volume"] for k in klines[-6:-1]) / 5) if len(klines) > 5 else 1
            
            if is_up:
                if vol_ratio > 1.5:
                    why2 = f"放量上涨，近5日累计{recent_trend:+.1f}%，资金积极进场"
                    analysis["factors"]["capital"] = "流入"
                else:
                    why2 = f"缩量上涨，近5日累计{recent_trend:+.1f}%，可能反弹乏力"
                    analysis["factors"]["capital"] = "观望"
            else:
                if vol_ratio > 1.5:
                    why2 = f"放量下跌，近5日累计{recent_trend:+.1f}%，资金出逃"
                    analysis["factors"]["capital"] = "流出"
                else:
                    why2 = f"缩量下跌，近5日累计{recent_trend:+.1f}%，恐慌情绪不强"
                    analysis["factors"]["capital"] = "缩量"
        else:
            why2 = "K线数据不足，无法判断趋势"
        
        analysis["five_why"].append({"level": 2, "question": "成交量和趋势如何？", "answer": why2})
        
        # Why 3: 技术面如何？
        if klines and len(klines) >= 10:
            ma5 = sum(k["close"] for k in klines[-5:]) / 5
            ma10 = sum(k["close"] for k in klines[-10:]) / 10
            
            if current_price > ma5 > ma10:
                why3 = f"价格站上5日均线({ma5:.2f})和10日均线({ma10:.2f})，多头排列"
                analysis["factors"]["technical"] = "多头"
            elif current_price < ma5 < ma10:
                why3 = f"价格跌破5日均线({ma5:.2f})和10日均线({ma10:.2f})，空头排列"
                analysis["factors"]["technical"] = "空头"
            else:
                why3 = f"均线交织，趋势不明朗。5日线{ma5:.2f}，10日线{ma10:.2f}"
                analysis["factors"]["technical"] = "震荡"
        else:
            why3 = "数据不足，无法分析均线"
            analysis["factors"]["technical"] = "未知"
        
        analysis["five_why"].append({"level": 3, "question": "技术面位置如何？", "answer": why3})
        
        # Why 4: 买入逻辑是否正确？
        if pnl_pct < -5:
            why4 = f"亏损{abs(pnl_pct):.1f}%，买入时机可能不对，或追高买入"
            analysis["lessons"].append("反思：是否在高位追涨？是否忽略了大盘风险？")
        elif pnl_pct < 0:
            why4 = f"小幅亏损{abs(pnl_pct):.1f}%，可能是短期波动，需观察"
            analysis["lessons"].append("观察：是否触及止损线？趋势是否恶化？")
        elif pnl_pct < 3:
            why4 = f"小幅盈利{pnl_pct:.1f}%，持仓正确但涨幅有限"
            analysis["lessons"].append("思考：是否需要加仓？还是等待更好机会？")
        else:
            why4 = f"盈利{pnl_pct:.1f}%，买入逻辑验证正确"
            analysis["lessons"].append("复制：分析这笔成功的原因，未来寻找类似机会")
        
        analysis["five_why"].append({"level": 4, "question": "买入逻辑是否正确？", "answer": why4})
        
        # Why 5: 下一步怎么操作？
        params = self.load_json(self.params_file)
        stop_loss = params.get("stop_loss_pct", -0.08) * 100
        take_profit = params.get("take_profit_pct", 0.05) * 100
        
        if pnl_pct <= stop_loss:
            why5 = f"已触及止损线({stop_loss}%)，建议卖出止损"
            analysis["action_suggestion"] = "SELL_STOP_LOSS"
        elif pnl_pct >= take_profit:
            why5 = f"已达止盈线({take_profit}%)，建议减仓锁定利润"
            analysis["action_suggestion"] = "REDUCE_TAKE_PROFIT"
        elif analysis["factors"]["technical"] == "空头" and pnl_pct < 0:
            why5 = "技术面转弱且浮亏，建议设置更紧的止损"
            analysis["action_suggestion"] = "TIGHTEN_STOP"
        elif analysis["factors"]["technical"] == "多头" and pnl_pct > 0:
            why5 = "技术面向好且盈利，可以继续持有"
            analysis["action_suggestion"] = "HOLD"
        else:
            why5 = "趋势不明朗，保持观望，严格执行交易纪律"
            analysis["action_suggestion"] = "WATCH"
        
        analysis["five_why"].append({"level": 5, "question": "下一步怎么操作？", "answer": why5})
        
        return analysis
    
    def generate_strategy_adjustments(self, analyses: List[Dict], 
                                       market_context: Dict) -> Dict:
        """根据复盘结果生成策略调整建议"""
        
        adjustments = {
            "params_changes": {},
            "rules_changes": [],
            "watchlist_changes": []
        }
        
        params = self.load_json(self.params_file)
        
        # 统计分析结果
        losers = [a for a in analyses if a["pnl_pct"] < 0]
        winners = [a for a in analyses if a["pnl_pct"] > 0]
        big_losers = [a for a in analyses if a["pnl_pct"] < -5]
        
        # 如果亏损股票多且跟随大盘
        market_followers = [a for a in losers if a["factors"]["market"] == "跟随"]
        if len(market_followers) > len(losers) * 0.5:
            adjustments["rules_changes"].append({
                "rule": "大盘风控",
                "change": "当上证跌幅 > 1% 时，暂停买入操作",
                "reason": f"今日{len(market_followers)}只股票跟随大盘下跌，系统性风险控制不足"
            })
        
        # 如果有大额亏损
        if big_losers:
            current_stop = params.get("stop_loss_pct", -0.08)
            new_stop = max(current_stop, -0.06)  # 收紧到6%
            if new_stop != current_stop:
                adjustments["params_changes"]["stop_loss_pct"] = new_stop
                adjustments["rules_changes"].append({
                    "rule": "止损线",
                    "change": f"从 {current_stop*100:.0f}% 收紧到 {new_stop*100:.0f}%",
                    "reason": f"有{len(big_losers)}只股票亏损超过5%，止损执行不及时"
                })
        
        # 如果技术面空头的股票多
        bearish_stocks = [a for a in analyses if a["factors"]["technical"] == "空头"]
        if len(bearish_stocks) > len(analyses) * 0.5:
            adjustments["rules_changes"].append({
                "rule": "选股条件",
                "change": "增加均线过滤：只买入价格在5日线上方的股票",
                "reason": f"持仓中{len(bearish_stocks)}只处于空头排列，选股时忽略了趋势"
            })
        
        # 如果资金流出的股票多
        outflow_stocks = [a for a in analyses if a["factors"]["capital"] == "流出"]
        if len(outflow_stocks) > 2:
            adjustments["rules_changes"].append({
                "rule": "资金流向",
                "change": "买入前检查近3日资金流向，连续流出不买入",
                "reason": f"有{len(outflow_stocks)}只股票资金持续流出"
            })
        
        # 更新观察名单
        for a in analyses:
            if a["action_suggestion"] == "SELL_STOP_LOSS":
                adjustments["watchlist_changes"].append({
                    "action": "remove",
                    "code": a["code"],
                    "name": a["name"],
                    "reason": "触发止损"
                })
            elif a["pnl_pct"] > 10:
                adjustments["watchlist_changes"].append({
                    "action": "watch",
                    "code": a["code"],
                    "name": a["name"],
                    "reason": "盈利丰厚，观察是否可以加仓同类股票"
                })
        
        return adjustments
    
    def apply_adjustments(self, adjustments: Dict) -> str:
        """应用策略调整"""
        results = []
        
        # 更新参数
        if adjustments["params_changes"]:
            params = self.load_json(self.params_file)
            for key, value in adjustments["params_changes"].items():
                old = params.get(key)
                params[key] = value
                results.append(f"✅ 参数调整: {key} 从 {old} 改为 {value}")
            params["version"] = params.get("version", 0) + 1
            params["last_updated"] = datetime.now().isoformat()
            self.save_json(self.params_file, params)
        
        return "\n".join(results) if results else "无参数调整"
    
    def run_deep_review(self) -> str:
        """运行深度复盘"""
        
        # 1. 获取市场环境
        market_context = self.get_market_context()
        
        # 2. 加载账户和持仓
        account = self.load_json(self.account_file)
        holdings = account.get("holdings", [])
        
        if not holdings:
            return "# 📊 深度复盘 | 无持仓\n\n当前无持仓，无需复盘。"
        
        # 3. 获取实时价格
        codes = [h["code"] for h in holdings]
        prices = fetch_realtime_sina(codes)
        
        # 4. 分析每只股票
        analyses = []
        for h in holdings:
            code = h["code"]
            current_price = prices.get(code, {}).get("price", h["cost_price"])
            analysis = self.analyze_stock_movement(
                code=code,
                name=h["name"],
                current_price=current_price,
                cost_price=h["cost_price"],
                market_context=market_context
            )
            analyses.append(analysis)
        
        # 5. 生成策略调整
        adjustments = self.generate_strategy_adjustments(analyses, market_context)
        
        # 6. 应用调整
        apply_result = self.apply_adjustments(adjustments)
        
        # 7. 生成报告
        report = self.generate_report(market_context, analyses, adjustments, apply_result)
        
        # 8. 保存报告
        today = datetime.now().strftime("%Y-%m-%d")
        report_file = REVIEW_DIR / f"deep_review_{today}.md"
        with open(report_file, 'w') as f:
            f.write(report)
        
        return report
    
    def generate_report(self, market_context: Dict, analyses: List[Dict],
                        adjustments: Dict, apply_result: str) -> str:
        """生成深度复盘报告"""
        
        lines = []
        today = datetime.now().strftime("%Y-%m-%d")
        
        lines.append(f"# 📊 深度复盘报告 | {today}")
        lines.append("")
        
        # === 大盘环境 ===
        lines.append("## 📈 大盘环境")
        lines.append("")
        for name, data in market_context["indices"].items():
            emoji = "🟢" if data["change_pct"] >= 0 else "🔴"
            lines.append(f"- {emoji} **{name}**: {data['price']:.2f} ({data['change_pct']:+.2f}%)")
        lines.append(f"- 🎯 市场情绪: **{market_context['trend']}**")
        lines.append("")
        
        # === 持仓分析 ===
        lines.append("## 🔍 持仓深度分析")
        lines.append("")
        
        for a in analyses:
            emoji = "🟢" if a["pnl_pct"] >= 0 else "🔴"
            lines.append(f"### {emoji} {a['name']} ({a['code']})")
            lines.append(f"**盈亏: {a['pnl_pct']:+.2f}%** | 成本: {a['cost_price']:.2f} → 现价: {a['current_price']:.2f}")
            lines.append("")
            
            # 5-Why 分析
            lines.append("**5-Why 分析:**")
            for why in a["five_why"]:
                lines.append(f"{why['level']}. **{why['question']}**")
                lines.append(f"   → {why['answer']}")
            lines.append("")
            
            # 因素总结
            lines.append("**影响因素:**")
            factors = a["factors"]
            lines.append(f"- 大盘: {factors['market']} | 资金: {factors['capital']} | 技术: {factors['technical']}")
            lines.append("")
            
            # 教训
            if a["lessons"]:
                lines.append("**教训/启示:**")
                for lesson in a["lessons"]:
                    lines.append(f"- {lesson}")
                lines.append("")
            
            # 操作建议
            action_map = {
                "SELL_STOP_LOSS": "⚠️ 建议止损卖出",
                "REDUCE_TAKE_PROFIT": "💰 建议减仓止盈",
                "TIGHTEN_STOP": "🔒 建议收紧止损",
                "HOLD": "✅ 继续持有",
                "WATCH": "👀 保持观望"
            }
            lines.append(f"**操作建议:** {action_map.get(a['action_suggestion'], '未知')}")
            lines.append("")
            lines.append("---")
            lines.append("")
        
        # === 策略调整 ===
        lines.append("## 🔧 策略调整")
        lines.append("")
        
        if adjustments["rules_changes"]:
            lines.append("### 规则调整")
            for rule in adjustments["rules_changes"]:
                lines.append(f"**{rule['rule']}**")
                lines.append(f"- 调整: {rule['change']}")
                lines.append(f"- 原因: {rule['reason']}")
                lines.append("")
        
        if adjustments["params_changes"]:
            lines.append("### 参数调整")
            for key, value in adjustments["params_changes"].items():
                lines.append(f"- `{key}` → `{value}`")
            lines.append("")
        
        lines.append(f"**应用结果:** {apply_result}")
        lines.append("")
        
        # === 明日计划 ===
        lines.append("## 📋 明日操作计划")
        lines.append("")
        
        for a in analyses:
            if a["action_suggestion"] == "SELL_STOP_LOSS":
                lines.append(f"- ⚠️ **{a['name']}**: 开盘检查，如继续下跌则止损")
            elif a["action_suggestion"] == "REDUCE_TAKE_PROFIT":
                lines.append(f"- 💰 **{a['name']}**: 盈利较好，考虑减仓50%锁定利润")
            elif a["action_suggestion"] == "TIGHTEN_STOP":
                lines.append(f"- 🔒 **{a['name']}**: 设置更紧止损，跌破X元则卖出")
        
        lines.append("")
        
        return "\n".join(lines)


def main():
    engine = DeepReviewEngine()
    report = engine.run_deep_review()
    print(report)


if __name__ == "__main__":
    main()
