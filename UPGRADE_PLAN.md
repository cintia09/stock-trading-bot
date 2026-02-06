# 量化交易系统升级方案

## 当前状态
- ✅ 基础框架：数据获取、技术分析、交易引擎
- ✅ 数据源：新浪实时 + BaoStock K线（东方财富备用）
- ✅ 技术指标：MA/EMA/MACD/RSI/KDJ/BOLL
- ✅ 简单评分系统
- ❌ 缺少回测验证
- ❌ 缺少 T+0 日内策略
- ❌ 缺少多因子选股模型
- ❌ 缺少风险管理

## 升级目标

### 1. T+0 日内交易策略
利用底仓做日内差价（A股 T+1 规则下的 T+0）：
- 持仓股票可以当日卖出昨日持仓
- 卖出后当日买回，实现日内交易
- 核心：捕捉日内波动 2-5%

### 2. 多因子选股模型
```
综合得分 = Σ(因子权重 × 因子得分)

因子类别：
├── 动量因子 (25%)
│   ├── 5日涨幅
│   ├── 20日涨幅
│   └── 相对强弱
├── 技术因子 (25%)
│   ├── MACD信号
│   ├── KDJ信号
│   └── 布林带位置
├── 量价因子 (20%)
│   ├── 量比
│   ├── 换手率
│   └── 量价背离
├── 资金因子 (15%)
│   ├── 主力净流入
│   └── 北向资金
└── 情绪因子 (15%)
    ├── 市场热度
    └── 板块轮动
```

### 3. 回测系统
- 历史数据回测
- 策略参数优化
- 绩效指标：收益率、夏普比率、最大回撤

### 4. 风险管理
- 动态止损（ATR止损）
- 仓位控制（Kelly公式）
- 分散化（行业/市值）
- 黑天鹅保护

## 实施计划

### Phase 1: T+0 策略模块 (今天)
- [ ] 日内波动分析
- [ ] T+0 信号生成
- [ ] 底仓管理

### Phase 2: 多因子模型 (今天)
- [ ] 因子库构建
- [ ] 因子权重优化
- [ ] 选股筛选器

### Phase 3: 回测系统 (明天)
- [ ] 回测引擎
- [ ] 参数优化
- [ ] 绩效报告

### Phase 4: 风控升级 (明天)
- [ ] ATR 动态止损
- [ ] 仓位管理
- [ ] 风险监控

## T+0 策略设计

### 核心逻辑
```
1. 早盘（9:30-10:00）：观察开盘走势
2. 上午（10:00-11:30）：
   - 若持仓股跳涨 >2%，考虑先卖（锁定利润）
   - 若持仓股跳跌 >2%，考虑加仓摊低成本
3. 下午（13:00-14:30）：
   - 卖出后若回落，择机买回
   - 未卖出则观望
4. 尾盘（14:30-15:00）：
   - 确认日内交易完成
   - 调整隔夜仓位
```

### 信号触发条件
```python
# 日内卖出信号（针对昨日持仓）
def t0_sell_signal(current_price, open_price, high_price, pre_close):
    # 1. 冲高回落
    if high_price / open_price > 1.03 and current_price < high_price * 0.98:
        return "冲高回落卖出"
    
    # 2. 跳空高开后回落
    if open_price / pre_close > 1.02 and current_price < open_price:
        return "高开低走卖出"
    
    # 3. 达到止盈目标
    if current_price / pre_close > 1.05:
        return "止盈卖出"
    
    return None

# 日内买回信号（已卖出后）
def t0_buy_signal(current_price, sold_price, low_price, pre_close):
    # 1. 回落到卖出价以下
    if current_price < sold_price * 0.97:
        return "回落买入"
    
    # 2. 探底回升
    if low_price / pre_close < 0.97 and current_price > low_price * 1.01:
        return "探底回升买入"
    
    return None
```

## 数据需求

### 实时数据（盘中）
- 5秒级别行情更新
- 分时图数据
- 五档盘口

### 历史数据（回测）
- 日K线（BaoStock）
- 分钟K线（需要新数据源）

## 文件结构
```
stock-trading/
├── scripts/
│   ├── fetch_stock_data.py    # 数据获取
│   ├── technical_analysis.py  # 技术分析
│   ├── trading_engine.py      # 交易引擎
│   ├── t0_strategy.py         # T+0策略 [NEW]
│   ├── factor_model.py        # 多因子模型 [NEW]
│   ├── backtest.py            # 回测系统 [NEW]
│   ├── risk_manager.py        # 风控模块 [NEW]
│   └── main.py                # 主程序
├── data/
│   ├── klines/                # K线缓存
│   └── factors/               # 因子数据
├── backtest/
│   ├── results/               # 回测结果
│   └── strategies/            # 策略配置
└── logs/
    └── trades/                # 交易日志
```
