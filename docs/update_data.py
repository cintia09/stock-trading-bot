#!/usr/bin/env python3
"""
投资看板数据更新脚本
读取所有JSON数据源，生成合并的 data.js 文件供 index.html 使用

使用方式: python3 update_data.py
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

# 配置：数据源路径（相对于 stock-trading 目录）
BASE_DIR = Path(__file__).parent.parent / "stock-trading"
OUTPUT_FILE = Path(__file__).parent / "data.js"

# 数据源定义
DATA_SOURCES = {
    "account": {
        "path": BASE_DIR / "account.json",
        "description": "股票持仓与账户信息"
    },
    "transactions": {
        "path": BASE_DIR / "transactions.json",
        "description": "交易记录"
    },
    "strategy_params": {
        "path": BASE_DIR / "strategy_params.json",
        "description": "策略参数"
    },
    "watchlist": {
        "path": BASE_DIR / "watchlist.json",
        "description": "关注列表"
    },
    "cb_opportunities": {
        "path": BASE_DIR / "data" / "cb_opportunities.json",
        "description": "可转债套利机会"
    },
    "tomorrow_plan": {
        "path": BASE_DIR / "tomorrow_plan.json",
        "description": "明日交易计划"
    }
}

def load_json_safe(filepath: Path) -> dict | list | None:
    """安全加载JSON文件，不存在则返回None"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"⚠️  文件不存在: {filepath}")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析错误 {filepath}: {e}")
        return None

def get_file_mtime(filepath: Path) -> str | None:
    """获取文件最后修改时间"""
    try:
        mtime = os.path.getmtime(filepath)
        return datetime.fromtimestamp(mtime).isoformat()
    except:
        return None

def main():
    print("=" * 50)
    print("📊 投资看板数据更新")
    print("=" * 50)
    
    # 构建数据结构
    dashboard_data = {
        "_meta": {
            "generated_at": datetime.now().isoformat(),
            "generator": "update_data.py",
            "version": "1.0"
        },
        "sources": {}
    }
    
    # 加载每个数据源
    for source_name, config in DATA_SOURCES.items():
        filepath = config["path"]
        data = load_json_safe(filepath)
        
        dashboard_data["sources"][source_name] = {
            "data": data,
            "description": config["description"],
            "last_updated": get_file_mtime(filepath) if data else None,
            "available": data is not None
        }
        
        status = "✅" if data else "❌"
        print(f"{status} {source_name}: {config['description']}")
    
    # 生成 data.js
    js_content = f"""// 投资看板数据文件 - 自动生成，请勿手动编辑
// 生成时间: {dashboard_data['_meta']['generated_at']}
// 使用方式: 在 index.html 中引用此文件

window.DASHBOARD_DATA = {json.dumps(dashboard_data, ensure_ascii=False, indent=2)};
"""
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(js_content)
    
    print("=" * 50)
    print(f"✅ 已生成: {OUTPUT_FILE}")
    print(f"📅 时间: {dashboard_data['_meta']['generated_at']}")
    print("=" * 50)

    # 确保看板HTTP服务在运行
    try:
        script = Path(__file__).parent / "start_server.sh"
        subprocess.run(["bash", str(script)], check=False)
    except Exception as e:
        print(f"⚠️ 启动看板HTTP服务失败: {e}")

if __name__ == "__main__":
    main()
