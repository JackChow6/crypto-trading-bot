# 加密货币永续合约量化交易引擎

一个运行在 **Binance USDT-M 永续合约**上的量化交易系统：**动量选币 + 确定性规则信号 + 固定止盈止损 + 实盘执行（ccxt）+ Web 控制台**。

- 📡 **动量选币**：多周期平滑动量（5m/15m/30m/1h/4h 加权打分）从全市场筛选最强势的交易对，或 24h 涨幅榜
- 🎯 **规则信号**：「趋势三共振」（Supertrend + EMA 排列 + 市场结构）确定性判断方向，不做黑箱预测
- 🛡️ **服务器端止盈止损**：开仓即挂币安 Algo 条件单（reduce-only），断线也有保护
- 📊 **Web 控制台**：K 线图 + 动量榜单 + 持仓卡片 + 权益曲线，浏览器实时查看
- 🧪 **实盘/模拟盘双模式**：`live: false` 纯模拟盘（默认安全），`live: true` 真实下单

> ⚠️ **风险提示**：加密货币合约交易存在真实亏损甚至爆仓风险。本项目默认纯模拟盘，接入真实资金前请充分回测并自行承担风险。

---

## 架构

```
全市场 ticker → 流动性过滤 → 动量打分排序 → Top N 交易对
                                              │
                                              ▼
                    WebSocket 实时行情（K线/盘口/成交，合并连接）
                                              │
                                              ▼
                    规则信号：趋势三共振（Supertrend + EMA + 结构）
                                              │
                                              ▼
                    入场确认（回踩/跌破） → 风控 → 市价开仓
                                              │
                                              ▼
                    服务器端止盈/止损 Algo 条件单（reduce-only）
                                              │
                                              ▼
                    持仓结算 + 对账 + Web 控制台
```

**核心原则**
- 只在「趋势方向明确 + 多指标共振」时给信号，震荡/分歧一律观望
- 止盈止损走交易所服务器端，引擎断线也不裸奔
- 全局 REST 限流 + IP 封禁熔断，避免打爆币安 API

---

## 安装

```bash
pip install ccxt pyyaml aiohttp
# WebSocket 实时行情需要 ccxt.pro：
pip install ccxtpro
```

> 可选：本地模型信号源（`signal_source: ai`）需要 Ollama：
> ```bash
> ollama pull qwen3:4b
> ```

---

## 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 exchange.api_key / api_secret（可选 telegram 凭据）
```

关键配置项：

| 配置 | 说明 |
|---|---|
| `exchange.api_key/api_secret` | Binance API（只开交易权限，禁提现） |
| `engine.live` | `false`=模拟盘（默认），`true`=实盘 |
| `engine.signal_source` | `rule`=规则信号（推荐），`ai`=本地模型 |
| `engine.fixed_tp_usd / fixed_sl_usd` | 固定止盈/止损金额（USDT） |
| `engine.reverse` | `true`=反向跟单（镜像信号方向） |
| `engine.gainers.*` | 动量选币参数（Top N、刷新间隔、成交额门槛） |
| `engine.risk.*` | 每笔保证金、杠杆、最大持仓、回撤熔断 |
| `engine.dashboard` | Web 控制台开关与端口 |

> 国内访问币安需走代理（Clash 等），在 `exchange.proxy` 填入。

---

## 运行

```bash
# 动量信号源（推荐）：自动筛选强势币 + 规则信号 + 自动交易
python v43/engine.py --config config.yaml --gainers

# TG 信号驱动：信号选币，实时行情定入场
python v43/engine.py --config config.yaml --tg

# WebSocket 自主行情扫描（单币）
python v43/engine.py --config config.yaml --ws

# 合成行情自测（模拟盘，不联网下单）
python v43/engine.py --config config.yaml --simulate
python v43/engine.py --config config.yaml --mock
```

启动后 Web 控制台：**http://127.0.0.1:8000**

---

## Web 控制台

- **概览**：权益/余额、今日盈亏、净盈亏、总浮盈、胜率、盈亏比、最大回撤
- **动量/涨幅榜**：各周期收益（5m~4h）、成交额、是否已持仓
- **K 线图**：蜡烛 + 成交量 + MA 均线 + 入场/止盈/止损线 + 十字光标
- **持仓卡片**：浮盈%、ROE、SL→TP 进度条、持仓时长
- **操作**：暂停/恢复、平仓、平全部、熔断、手动加币

---

## 目录结构

```
config.example.yaml   配置模板（复制为 config.yaml）
v43/
  engine.py           编排器（--gainers/--tg/--ws/--simulate/--mock）
  momentum.py         多周期平滑动量选币
  gainers.py          24h 涨幅榜选币
  rule_signal.py      规则信号（趋势三共振）
  live.py             实盘执行层（ccxt 下单 + 服务器端止盈止损）
  paper.py            模拟盘 + 持仓管理 + 复盘统计
  stream.py           WebSocket 实时行情流（多币合并连接）
  features.py         特征引擎（ATR/EMA/结构/订单流）
  events.py           事件检测
  risk.py             风控
  dashboard.py        Web 控制台
  backtest_*.py       回测脚本（动量因子、参数网格扫描等）
```

---

## 上线前检查清单

- [ ] `--simulate` / `--mock` 跑通，日志里信号→风控→开仓链路正确
- [ ] 用 `--gainers` + `live: false` 跑模拟盘一段时间，看胜率/盈亏比
- [ ] 用回测脚本验证止盈止损参数（`python v43/backtest_fixed_opt.py`）
- [ ] Binance API 只开交易权限、禁提现、IP 白名单
- [ ] 小资金实盘验证后再逐步加大仓位

---

## 免责声明

本项目仅供学习研究，不构成任何投资建议。使用者需自行承担使用本软件进行交易的全部风险与后果。
