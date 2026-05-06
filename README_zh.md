# Polymarket 加密货币交易机器人

*[English](README.md) | [中文](README_zh.md)*

⚠️ **注意：本项目目前主要作为一个基于 Python 的 Polymarket 开发脚手架。内置的“闪崩（Flash Crash）”策略已经失效，请谨慎使用或仅作为学习参考。**

这是一个适用于 Polymarket 预测市场的 Python 交易机器人脚手架。它支持零 Gas 费交易和实时 WebSocket 订单簿数据流。本项目包含核心的交易基础库以及一个提供参考的“闪崩（Flash Crash）”波动率交易策略。

![Live Trading Example](assets/live.png)

## 核心特性

- **闪崩（Flash Crash）策略** - 自动化的波动率交易策略，检测 5分钟/15分钟/1小时 市场上的概率骤降，利用市场无效性获利。
- **多币种并发支持** - 使用单一的 JSON 配置文件，即可同时运行针对不同币种（如 BTC、ETH、SOL、XRP 等）的交易策略。
- **零 Gas 费交易** - 内置对 Polymarket Builder Program 的支持，实现完全免 Gas 的链上交易。
- **实时 WebSocket** - 实时流式传输订单簿更新和市场数据。
- **安全存储** - 采用 PBKDF2 + Fernet 加密技术安全存储私钥。
- **模块化 API** - 提供干净、直观的 Python 接口，便于开发者编写自定义交易策略。

## 快速开始

### 前置条件

- Python 3.8 或更高版本
- Polymarket 代理钱包（Safe Address）
- 钱包私钥

### 1. 安装步骤

```bash
# 克隆仓库
git clone https://github.com/ChyranOY/polybot-crypto-py.git
cd polybot-crypto-py

# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置文件

首先配置环境变量。复制环境变量示例文件并填写你的私钥和代理钱包地址：

```bash
cp .env.example .env
```

编辑 `.env` 文件，确保包含以下必填内容：

```bash
PRIVATE_KEY=你的私钥
PROXY_WALLET=0x你的Polymarket代理钱包地址
```

> **代理钱包地址（PROXY WALLET）**：你可以在 [polymarket.com/settings](https://polymarket.com/settings) 页面找到你的 Safe 地址。

接着配置策略参数。在根目录下创建一个 `config.json` 文件，用于为每个币种定义交易参数：

```json
{
  "BTC": {
    "interval": "15m",
    "size": 11,
    "drop": 0.40,
    "lookback": 5,
    "take_profit": 0.50,
    "stop_loss": 0.50,
    "min_price": 0.11,
    "max_price": 0.89,
    "slippage": 0.025,
    "max_spread": 0.021
  },
  "ETH": {
    "interval": "15m",
    "size": 100,
    "drop": 0.30
  }
}
```

### 3. 环境自检

在正式运行策略前，强烈建议运行自检脚本以确保所有配置正确无误：

```bash
python scripts/config_self_check.py
```

### 4. 运行策略

使用你的 JSON 配置文件启动闪崩策略：

```bash
# 使用配置文件运行策略
python apps/flash_crash_runner.py --config-file config.json

# 试运行模式（Dry-run mode，不进行真实交易）
python apps/flash_crash_runner.py --config-file config.json --dry-run

# 开启 Debug 日志运行
python apps/flash_crash_runner.py --config-file config.json --debug
```

## 交易策略说明

### 闪崩策略 (`apps/flash_crash_strategy.py`)

⚠️ **警告：此策略目前已经失效，不建议在生产环境中使用，仅供学习和二次开发参考。**

该策略会监控市场出现的突然概率下降，并根据 `config.json` 中提供的参数自动执行交易。

**`config.json` 中的关键参数:**
- `interval` - 市场时间间隔（默认：15m）
- `size` - 每次交易的代币数量
- `drop` - 触发交易的绝对概率下降阈值
- `lookback` - 检测时间窗口（秒）
- `take_profit` - 止盈百分比
- `stop_loss` - 止损百分比
- `min_price` - 允许交易的最低价格
- `max_price` - 允许交易的最高价格
- `max_spread` - 可接受的最大买卖价差

## 使用示例

### 基础用法

```python
from src import create_bot_from_env
import asyncio

async def main():
    bot = create_bot_from_env()
    orders = await bot.get_open_orders()
    print(f"当前未成交订单数: {len(orders)}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 下单

```python
import asyncio
from src import TradingBot, Config

async def place_order():
    # 使用配置和私钥初始化机器人
    bot = TradingBot(config=Config(safe_address="0x..."), private_key="0x...")

    # 下限价单
    result = await bot.place_order(token_id="...", price=0.65, size=10.0, side="BUY")
    if result.success:
        print(f"下单成功，订单ID: {result.order_id}")
    else:
        print(f"下单失败: {result.message}")

if __name__ == "__main__":
    asyncio.run(place_order())
```

### WebSocket 数据流

```python
import asyncio
from src.websocket_client import MarketWebSocket

async def stream_market():
    ws = MarketWebSocket()
    ws.on_book = lambda s: print(f"中间价: {s.mid_price:.4f}")
    await ws.subscribe(["token_id"])
    await ws.run()

if __name__ == "__main__":
    asyncio.run(stream_market())
```

## 配置参考

### 环境变量列表

| 变量名 | 是否必填 | 描述 |
|----------|----------|-------------|
| `PRIVATE_KEY` | 是 | 钱包私钥 |
| `PROXY_WALLET` | 是 | Polymarket 代理钱包地址（Safe） |
| `POLY_BUILDER_CODE` | 否 | Builder 邀请码（用于 CLOB v2 订单归因） |
| `POLY_RELAYER_API_KEY` | 否 | Relayer API Key（用于免 Gas 交易） |
| `POLY_RELAYER_API_KEY_ADDRESS` | 否 | Relayer API Key 地址 |
| `POLY_RPC_URL` | 否 | Polygon RPC URL (默认: https://polygon-rpc.com) |
| `POLY_CHAIN_ID` | 否 | Chain ID (默认: 137) |
| `POLY_CLOB_HOST` | 否 | CLOB API host (默认: https://clob.polymarket.com) |
| `POLY_DATA_DIR` | 否 | 用于存储加密凭证的目录 |
| `POLY_LOG_LEVEL` | 否 | 日志级别 (DEBUG, INFO, WARNING, ERROR) |
| `POLY_DEFAULT_SIZE` | 否 | 默认订单金额 (USDC) |
| `POLY_DEFAULT_PRICE` | 否 | 默认订单价格 |

## 零 Gas 交易与订单归因 (Gasless Trading & Attribution)

通过 Polymarket Builder Program 启用零 Gas 交易并获得交易量归因：

1. 在 [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder) 申请 Builder 权限。
2. **订单归因 (Order Attribution)**：在 `.env` 中设置 `POLY_BUILDER_CODE`。Bot 会自动将此代码注入到 CLOB V2 的 `OrderArgs` 中，以确保交易量计入你的排行榜。
3. **免 Gas 链上交易 (Gasless On-chain Transactions)**：对于 Token 授权等链上操作，Polymarket 目前使用 Relayer API Key。在 `.env` 中设置 `POLY_RELAYER_API_KEY` 和 `POLY_RELAYER_API_KEY_ADDRESS`（可在 [Settings > API Keys](https://polymarket.com/settings?tab=api-keys) 创建）。

## 安全建议

私钥可以使用 PBKDF2（480,000 次迭代）+ Fernet 对称加密技术进行加密。最佳实践：
- **永远不要**将包含真实密钥的 `.env` 或配置文件提交到版本控制系统（如 GitHub）。
- 使用一个资金有限的**专用交易钱包**，不要使用主钱包。
- 确保加密密钥文件的安全（建议权限设置为 `0600`）。

## 免责声明

本软件仅供学习和教育目的使用。交易涉及重大资金损失风险。开发者不对使用本机器人造成的任何财务损失负责。
