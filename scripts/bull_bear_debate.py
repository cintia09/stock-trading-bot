#!/usr/bin/env python3
"""
Bull/Bear 辩论机制 — 对候选股进行多空辩论，输出买入置信度

灵感来源：TradingAgents 的多角色辩论框架
实现方式：单次LLM调用 + prompt工程模拟辩论（省token）
"""

import json
import re
import requests
import random
from typing import Dict, Optional

# LLM配置 — 优先通过OpenClaw Gateway调用，fallback到直接API
import os
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARAMS_FILE = os.path.join(os.path.dirname(_SCRIPT_DIR), "strategy_params.json")

def _load_llm_config():
    """从strategy_params.json读取LLM配置"""
    defaults = {
        "provider": "openclaw",  # openclaw / gemini / openai
        "model": "gemini-2.0-flash",
        "api_key": "",
        "base_url": "",
    }
    try:
        with open(_PARAMS_FILE) as f:
            params = json.load(f)
        llm_cfg = params.get("debate_llm", {})
        for k, v in llm_cfg.items():
            defaults[k] = v
    except Exception:
        pass
    return defaults


def _call_llm(prompt: str) -> str:
    """调用LLM — 优先走OpenClaw CLI，fallback到直接API"""
    cfg = _load_llm_config()
    
    if cfg["provider"] == "openclaw":
        return _call_via_openclaw(prompt)
    elif cfg["provider"] == "openai":
        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 2048,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    else:
        # Gemini直连
        api_key = cfg.get("api_key", "") or os.environ.get("GEMINI_API_KEY", "")
        base = cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{cfg['model']}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_via_openclaw(prompt: str) -> str:
    """通过GitHub Copilot API调用LLM（复用OpenClaw的token）"""
    # 读取OpenClaw的copilot token
    token_file = "/root/.openclaw/credentials/github-copilot.token.json"
    with open(token_file) as f:
        token_data = json.load(f)
    token = token_data["token"]
    
    # 从token解析API endpoint
    base_url = "https://proxy.business.githubcopilot.com"
    for part in token.split(";"):
        if part.startswith("proxy-ep="):
            base_url = "https://" + part.split("=", 1)[1]
            break
    
    # 读取配置的模型，默认gemini-2.0-flash
    cfg = _load_llm_config()
    model = cfg.get("model", "gemini-2.0-flash")
    # 去掉provider前缀
    if "/" in model:
        model = model.split("/", 1)[1]
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "GitHubCopilotChat/0.35.0",
        "Editor-Version": "vscode/1.107.0",
        "Editor-Plugin-Version": "copilot-chat/0.35.0",
        "Copilot-Integration-Id": "vscode-chat",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    resp = requests.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _build_debate_prompt(code: str, info: Dict) -> str:
    """构建辩论prompt"""
    # 组装股票信息
    info_text = f"""
股票代码: {code}
股票名称: {info.get('name', '未知')}
当前价格: {info.get('price', '未知')}
今日涨跌幅: {info.get('change_pct', '未知')}%
PE(市盈率): {info.get('pe', '未知')}
PB(市净率): {info.get('pb', '未知')}
行业: {info.get('industry', '未知')}
市值: {info.get('market_cap', '未知')}
近5日涨跌: {info.get('recent_5d_change', '未知')}%
近20日涨跌: {info.get('recent_20d_change', '未知')}%
成交量比(量比): {info.get('volume_ratio', '未知')}
换手率: {info.get('turnover_rate', '未知')}%
近期新闻/事件: {info.get('news', '无')}
技术信号: {info.get('technical_signals', '无')}
评分: {info.get('score', '未知')}
""".strip()

    return f"""你是一个专业的A股投资分析系统。现在要对以下股票进行多空辩论分析。

## 股票信息
{info_text}

## 辩论规则
请严格按照以下格式，依次扮演三个角色进行分析：

### 第一轮：Bull（多头分析师）
站在看多的角度，找出3-5个买入理由。必须基于上述具体数据论证，不要空泛。
重点关注：估值是否合理、技术面是否向好、行业趋势、资金面、催化剂事件。

### 第二轮：Bear（空头分析师）
站在看空的角度，找出3-5个不该买入的理由。必须基于上述具体数据论证。
重点关注：估值泡沫、技术面风险、行业逆风、资金流出、潜在利空。

### 第三轮：裁判（综合评判）
综合多空双方观点，给出最终裁决。

## 输出格式（严格JSON）
请直接输出以下JSON，不要包含其他内容：
```json
{{
  "bull_points": ["看多理由1", "看多理由2", "看多理由3"],
  "bear_points": ["看空理由1", "看空理由2", "看空理由3"],
  "bull_summary": "多头总结（1-2句话）",
  "bear_summary": "空头总结（1-2句话）",
  "confidence": 55,
  "key_risk": "最大风险点（1句话）",
  "key_opportunity": "最大机会点（1句话）",
  "verdict": "买入/观望/回避"
}}
```

注意：
- confidence 范围 0-100，50为中性，>60偏多，<40偏空
- 基于A股市场特点分析（T+1、涨跌停、散户结构等）
- 如果数据不足，适当降低confidence
- 请直接输出JSON，不要用markdown代码块包裹"""


def _parse_response(text: str) -> Dict:
    """解析LLM返回的JSON"""
    # 尝试直接解析
    text = text.strip()
    
    # 去掉markdown代码块
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试用正则提取JSON
        match = re.search(r'\{[^{}]*"confidence"[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    
    # 解析失败，返回默认
    return {
        "bull_points": [],
        "bear_points": [],
        "bull_summary": "解析失败",
        "bear_summary": "解析失败",
        "confidence": 50,
        "key_risk": "LLM返回格式异常",
        "key_opportunity": "未知",
        "verdict": "观望",
        "_parse_error": True,
        "_raw": text[:500]
    }


def debate_stock(code: str, info: Dict) -> Dict:
    """
    对单只股票进行Bull/Bear辩论
    
    Args:
        code: 股票代码，如 "sh600519"
        info: 股票信息字典，包含 price, change_pct, pe, industry, news 等
        
    Returns:
        dict: {confidence, key_risk, key_opportunity, bull_summary, bear_summary, ...}
    """
    try:
        prompt = _build_debate_prompt(code, info)
        response = _call_llm(prompt)
        result = _parse_response(response)
        
        # 确保必要字段
        result.setdefault("confidence", 50)
        result.setdefault("key_risk", "未知")
        result.setdefault("key_opportunity", "未知")
        result.setdefault("bull_summary", "")
        result.setdefault("bear_summary", "")
        result.setdefault("verdict", "观望")
        result["code"] = code
        result["name"] = info.get("name", code)
        
        # 强制confidence在范围内
        result["confidence"] = max(0, min(100, int(result["confidence"])))
        
        return result
        
    except Exception as e:
        return {
            "code": code,
            "name": info.get("name", code),
            "confidence": 50,
            "key_risk": f"辩论失败: {str(e)}",
            "key_opportunity": "未知",
            "bull_summary": "辩论失败",
            "bear_summary": "辩论失败",
            "verdict": "观望",
            "error": str(e)
        }


def apply_debate_to_decision(debate_result: Dict, original_quantity: int) -> tuple:
    """
    根据辩论结果调整买入决策
    
    Returns:
        (adjusted_quantity, reason)
    """
    confidence = debate_result.get("confidence", 50)
    
    if confidence < 40:
        return 0, f"辩论置信度过低({confidence})，放弃买入。风险: {debate_result.get('key_risk', '未知')}"
    elif confidence <= 60:
        adj_qty = (original_quantity // 200) * 100  # 减半，取整到100
        if adj_qty < 100:
            return 0, f"辩论置信度中等({confidence})，减半后不足1手，放弃"
        return adj_qty, f"辩论置信度中等({confidence})，买入量减半。风险: {debate_result.get('key_risk', '未知')}"
    else:
        return original_quantity, f"辩论置信度高({confidence})，正常买入。机会: {debate_result.get('key_opportunity', '未知')}"


# === 测试 ===
if __name__ == "__main__":
    test_stocks = [
        {
            "code": "sh600519",
            "info": {
                "name": "贵州茅台",
                "price": 1520.0,
                "change_pct": 1.2,
                "pe": 28.5,
                "pb": 8.2,
                "industry": "白酒",
                "market_cap": "1.9万亿",
                "recent_5d_change": 3.5,
                "recent_20d_change": -2.1,
                "volume_ratio": 1.3,
                "turnover_rate": 0.15,
                "news": "春节消费数据超预期，高端白酒动销良好",
                "technical_signals": "MACD金叉，KDJ超买区",
                "score": 72
            }
        },
        {
            "code": "sz000725",
            "info": {
                "name": "京东方A",
                "price": 4.85,
                "change_pct": -0.8,
                "pe": 35.2,
                "pb": 1.1,
                "industry": "面板/显示",
                "market_cap": "1700亿",
                "recent_5d_change": -3.2,
                "recent_20d_change": 8.5,
                "volume_ratio": 0.8,
                "turnover_rate": 1.2,
                "news": "OLED产线良率提升，但面板价格承压",
                "technical_signals": "均线多头排列，RSI中性",
                "score": 58
            }
        },
        {
            "code": "sz300750",
            "info": {
                "name": "宁德时代",
                "price": 210.0,
                "change_pct": 2.5,
                "pe": 22.0,
                "pb": 4.5,
                "industry": "锂电池/新能源",
                "market_cap": "9200亿",
                "recent_5d_change": 5.8,
                "recent_20d_change": 12.3,
                "volume_ratio": 1.8,
                "turnover_rate": 0.9,
                "news": "固态电池技术突破，欧洲工厂投产进度加速",
                "technical_signals": "放量突破前高，MACD强势",
                "score": 78
            }
        }
    ]
    
    print("=" * 60)
    print("🐂 vs 🐻  Bull/Bear 辩论测试")
    print("=" * 60)
    
    for stock in test_stocks:
        print(f"\n{'─' * 50}")
        print(f"📌 辩论: {stock['info']['name']}({stock['code']})")
        print(f"{'─' * 50}")
        
        result = debate_stock(stock["code"], stock["info"])
        
        print(f"\n🐂 多头: {result.get('bull_summary', 'N/A')}")
        if result.get("bull_points"):
            for p in result["bull_points"]:
                print(f"   + {p}")
        
        print(f"\n🐻 空头: {result.get('bear_summary', 'N/A')}")
        if result.get("bear_points"):
            for p in result["bear_points"]:
                print(f"   - {p}")
        
        print(f"\n⚖️ 裁决: {result.get('verdict', 'N/A')}")
        print(f"   置信度: {result['confidence']}/100")
        print(f"   最大风险: {result['key_risk']}")
        print(f"   最大机会: {result['key_opportunity']}")
        
        # 模拟买入决策
        adj_qty, reason = apply_debate_to_decision(result, 500)
        print(f"\n📊 决策: 原始500股 → 调整后{adj_qty}股")
        print(f"   理由: {reason}")
        
        if result.get("_parse_error"):
            print(f"   ⚠️ 解析异常，原始返回: {result.get('_raw', '')[:200]}")
        
        print()
