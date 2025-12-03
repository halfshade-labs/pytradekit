# PyTradeKit

[![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

PyTradeKit 是一个专业的数字货币交易工具包，为量化交易系统提供统一的多交易所接口、实时数据流、配置管理和基础设施支持。

## ✨ 主要特性

- **多交易所支持**: 支持 20+ 主流数字货币交易所的 RESTful API 和 WebSocket 连接
- **统一接口**: 提供标准化的交易接口，简化多交易所集成
- **实时数据流**: 基于 WebSocket 的实时行情、订单和账户更新
- **配置管理**: 灵活的分层配置系统，支持环境变量、配置文件等多种方式
- **日志系统**: 集成 Slack/Lark 通知的结构化日志系统
- **数据库支持**: MongoDB 和 Redis 操作封装
- **时间处理**: 完善的时区、时间戳转换工具
- **异常处理**: 统一的异常体系和重试机制
- **测试覆盖**: 完整的单元测试和集成测试

## 🏦 支持的交易所

### RESTful API 支持

- **Binance** (币安) - 现货、合约、期权
- **OKX** (欧易)
- **Bybit**
- **KuCoin**
- **Huobi** (火币)
- **Gate.io**
- **Bitfinex**
- **Bitget**
- **Bitmart**
- **Bullish**
- **Coinw**
- **Exmo**
- **HashKey**
- **Kraken**
- **LBank**
- **Mercado**
- **MEXC**
- **Poloniex**
- **WazirX**
- **WOOX**

### WebSocket 支持

- Binance
- OKX
- Bybit
- KuCoin
- Huobi
- Bitfinex

## 📦 安装

### 从源码安装

```bash
# 克隆仓库
git clone https://github.com/halfshade-labs/pytradekit.git
cd pytradekit

# 安装依赖
pip install -r requirements.txt
pip install -r shared_requirements.txt

# 安装包
python setup.py install

# 或使用开发模式
pip install -e .
```

### 使用 pip 安装

```bash
pip install pytradekit
```

## 🚀 快速开始

### 1. 基本配置

创建配置文件 `cfg/config.ini`:

```ini
[exchange]
api_key = your_api_key
api_secret = your_api_secret

[log]
log_name = my_trading_bot
report_webhook = https://your-webhook-url
watch_webhook = https://your-watch-webhook-url
```

或使用环境变量 `.env`:

```env
API_KEY=your_api_key
API_SECRET=your_api_secret
```

### 2. 使用 RESTful API

```python
from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.utils.log_setup import LoggerConfig, setup_logger

# 初始化日志
log_config = LoggerConfig(
    log_name="my_bot",
    report_webhook="https://your-webhook",
    watch_webhook="https://your-watch-webhook"
)
logger = setup_logger(log_config)

# 创建交易所客户端
client = BinanceClient(
    logger=logger,
    key="your_api_key",
    secret="your_api_secret"
)

# 获取账户信息
account_info = client.get_account()

# 下单
order = client.create_order(
    symbol="BTCUSDT",
    side="BUY",
    order_type="LIMIT",
    quantity=0.001,
    price=50000
)

logger.info(f"Order created: {order}")
```

### 3. 使用 WebSocket

```python
from pytradekit.ws.binance_ws import BinanceWebSocket
from pytradekit.utils.log_setup import LoggerConfig, setup_logger

# 初始化日志
log_config = LoggerConfig(
    log_name="ws_client",
    report_webhook="https://your-webhook",
    watch_webhook="https://your-watch-webhook"
)
logger = setup_logger(log_config)

# 创建 WebSocket 连接
ws = BinanceWebSocket(logger=logger)

# 订阅订单更新
def on_order_update(message):
    logger.info(f"Order update: {message}")

ws.subscribe_user_data_stream(
    api_key="your_api_key",
    callback=on_order_update
)

# 订阅市场数据
def on_ticker(message):
    logger.info(f"Ticker: {message}")

ws.subscribe_ticker("BTCUSDT", callback=on_ticker)
```

### 4. 使用配置管理

```python
from pytradekit.utils.config_agent import ConfigAgent
import os

# 初始化配置代理
project_root = os.path.dirname(os.path.abspath(__file__))
config = ConfigAgent(project_root)

# 获取配置值
api_key = config.get("exchange", "api_key")
log_level = config.get("log", "level", fallback="INFO")
```

### 5. 使用时间处理工具

```python
from pytradekit.utils.time_handler import TimeConvert, TimeFrame
import time

# 时间戳转换
current_ms = int(time.time() * 1000)
datetime_obj = TimeConvert.ms_to_datetime(current_ms)
formatted = TimeConvert.datetime_to_str(datetime_obj)

# 时间计算
one_hour_ago_ms = current_ms - TimeConvert.HOUR_TO_MS
one_day_ago_ms = current_ms - TimeConvert.DAY_TO_MS
```

### 6. 使用数据库操作

```python
from pytradekit.utils.mongodb_operations import MongoDBOperations
from pytradekit.utils.redis_operations import RedisOperations

# MongoDB 操作
mongo = MongoDBOperations(connection_string="mongodb://localhost:27017")
mongo.insert_one("trades", {"symbol": "BTCUSDT", "price": 50000})

# Redis 操作
redis = RedisOperations(host="localhost", port=6379)
redis.set("price:BTCUSDT", 50000, ttl=3600)
price = redis.get("price:BTCUSDT")
```

## 📚 核心模块

### `pytradekit.restful`
提供各交易所的 RESTful API 客户端，支持：
- 账户查询
- 订单管理（下单、撤单、查询）
- 市场数据（K线、深度、成交）
- 资金费率查询
- 持仓查询

### `pytradekit.ws`
WebSocket 实时数据流管理：
- 市场数据订阅（ticker、深度、K线、成交）
- 用户数据订阅（订单、账户、持仓）
- 自动重连机制
- 消息队列管理

### `pytradekit.utils`
核心工具模块：
- **config_agent.py**: 配置管理
- **log_setup.py**: 日志系统（支持 Slack/Lark 通知）
- **time_handler.py**: 时间处理工具
- **mongodb_operations.py**: MongoDB 操作封装
- **redis_operations.py**: Redis 操作封装
- **exceptions.py**: 自定义异常类
- **tools.py**: 通用工具函数

### `pytradekit.trading_setup`
交易相关设置：
- 账户管理
- 交易对映射
- 持仓跟踪
- WebSocket 重启配置

### `pytradekit.notifiers`
通知系统：
- Slack 集成
- Lark 集成
- 邮件通知

## ⚙️ 配置说明

### 配置文件结构

项目支持多层配置，优先级从高到低：
1. 命令行参数
2. 外部配置文件（通过命令行指定）
3. `.env` 文件
4. 内部配置文件

### 配置示例

```ini
# cfg/config.ini
[exchange]
api_key = your_api_key
api_secret = your_api_secret
passphrase = your_passphrase  # 部分交易所需要

[database]
mongodb_uri = mongodb://localhost:27017
redis_host = localhost
redis_port = 6379

[log]
log_name = trading_bot
log_level = INFO
report_webhook = https://hooks..com/services/...
watch_webhook = https://hooks.slack.com/services/...
channel = slack  # 或 lark

[websocket]
reconnect_interval = 5
max_reconnect_attempts = 10
```

## 🧪 测试

项目使用 pytest 进行测试，运行测试：

```bash
# 运行所有测试
pytest

# 运行特定测试文件
pytest tests/test_time_handler.py

# 运行测试并生成覆盖率报告
pytest --cov=pytradekit --cov-report=html

# 运行测试并显示详细输出
pytest -v
```

测试配置位于 `cfg/pytest.ini`。

## 📝 代码规范

项目遵循严格的代码规范，详见 [CODING_STANDARDS.md](CODING_STANDARDS.md)。主要原则：

- 遵循 PEP 8 规范
- 遵循 SOLID、DRY、KISS、YAGNI 原则
- 函数名使用动词开头，snake_case 格式
- 类名使用 PascalCase 格式
- 所有测试使用 pytest
- 函数长度不超过 50 行
- 完整的类型注解和文档字符串

## 🔧 开发指南

### 添加新交易所支持

1. 在 `pytradekit/restful/` 创建新的客户端类
2. 实现标准的交易接口方法
3. 在 `pytradekit/ws/` 添加 WebSocket 支持（如需要）
4. 添加相应的单元测试

### 日志使用规范

```python
# 仅使用 debug 和 info 级别
# info 级别会触发 Slack/Lark 通知
logger.debug("Detailed debug information")
logger.info("Important business information")  # 会发送通知
```

### 异常处理

```python
from pytradekit.utils.exceptions import ExchangeException, MinNotionalException

try:
    order = client.create_order(...)
except MinNotionalException as e:
    logger.info(f"Order too small: {e}")
except ExchangeException as e:
    logger.info(f"Exchange error: {e}")
```

## 🤝 贡献指南

欢迎贡献代码！请遵循以下步骤：

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

请确保：
- 代码符合项目规范
- 添加适当的测试
- 更新相关文档
- 通过所有测试

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🔗 相关项目

- [Cross-Exchange Arbitrage](https://github.com/halfshade-labs/cross-exchange-arbitrage) - 跨交易所套利系统

## 📞 联系方式

- 项目主页: https://github.com/halfshade-labs/pytradekit
- 问题反馈: https://github.com/halfshade-labs/pytradekit/issues

## 🙏 致谢

感谢所有为这个项目做出贡献的开发者！

---

**⚠️ 风险提示**: 数字货币交易存在高风险，使用本工具进行交易时请务必做好风险管理。本工具仅供学习和研究使用，使用者需自行承担交易风险。
