# Binance public bookTicker 冗余观察与接入

`RedundantBookTickerFeed` 组合现有同步 `BinanceWsManager`；仅支持 Binance
永续完整 BBO。两个独立 lifecycle worker 保留各自 stream ID、generation、
原始 `E/T` 和本地接收时间；ingress 单独记录 monotonic stamp，
等待队列与 wall-clock 回拨均不能使旧报价重新变新。独立有界 ingress 将网络回调与 selector 隔离。
它不支持 depth：增量深度必须先有 snapshot 对齐方案。

## 使用与运行边界

```python
from pytradekit.ws.redundant_bookticker import (
    RedundantBookTickerFeed, RedundantBookTickerOptions,
)
feed = RedundantBookTickerFeed(logger, quote_queue,
    RedundantBookTickerOptions(mode='redundant', max_age_ms=1000))
feed.update_bookticker_stream(['BTCUSDT', 'ETHUSDT'])
# 或使用阻塞至 close 的 start_bookticker_stream；update 不等待物理恢复。
# 最终必须执行 feed.close() 并检查 cleanup_pending / cleanup_failures。
```

`mode='shadow'` 只向消费队列发送 slot A 的消息；B 仅观察。shadow 的
`delivery_unavailable_ms` 代表 A 的投递不可用时间，`joint_unavailable_ms`
独立统计 A/B 合格并集为空的时长。默认模式为 `redundant`。

`get_public_feed_state()` 顶层 token 在物理切换期间保持稳定；`connected`
代表逻辑实例仍在运行，不能作为逐币交易许可。`symbols[symbol]` 中的
`epoch` 与 `available` 才表示逐币状态。覆盖丢失或未消费 transition 丢失仅增加
受影响币的 epoch；原始路仍可用时，安全 epoch 也可能已退休。
重新加入订阅不能复用先前 epoch。
所有合格观察先提升独立的 observed high-water；已投递水位独立去重。新 generation
不得回退到任何先前观察的低 `u`，即使先前那一路尚未被选为输出。shadow 的
primary 投递水位另行维护，B 的高序号不会改变 A 的交易投递。消费方需要继续执行自己的 age/skew、
source-ordering、持久确认及最终 candidate gate。

`is_current_bookticker_message` 只检查该消息的币种，保留物理身份 gate；
查询不会向消费队列发布消息：它可有界核验 raw ingress，但每个有序 fresh transition
会按原帧及原 epoch 暂存到有界 outbox，不能合并或悄然消耗中间价格。selector
随后逐帧按顺序发送并去重。过期、退休或丢失的未接受 transition 明确退休单币
的 episode epoch；同批旧 epoch 帧不能重新标新，只能由新的合格数据恢复。
fresh 健康 burst 不增 epoch，duplicate 不推进确认。物理断线且 B 仅持有更低 `u` 时会失去覆盖。
相同 `u` 只有 BBO、数量和源时间完全一致才能证明覆盖，永不重发，也不会
刷新原始接收/源时间或推进 confirmation。`u` 可以跳号，无连续整数要求。

公共 spot/perp manager 保留每个不同 `u` 的 BBO 更新，包括仅数量或源时间变化的
同价帧；不按价格相同或时间间隔采样。仅同一原生 update ID 的重复不再次投递。
热路径可调用 `get_public_symbol_state(symbol)` 精确查询一个币；返回形状与
`get_public_feed_state()` 相同，另有字符串 `symbol_scope`，`symbols` 仅含请求且
当前订阅的币。未订阅/已移除的币返回空 `symbols`；消费方必须仅合并或删除该
scope 的 epoch，不把其他未返回的币当作移除。查询在同一锁内逐帧核验 pending、
单币选择并检查当前时钟、物理代次及 ingress 缺口，不使用时间缓存或容差。
查询只暂存原帧，不向 output 投递。全标的查询仍执行即时完整选择；最终消费
freshness gate 每帧重新执行。
消费已排队消息时使用 `gate_bookticker_message(message)`，返回
`(accepted, scoped_state)`。它在同一 logical 锁下执行原 bool gate 的一次
pending 处理、单币选择及最终即时 freshness/epoch/physical 核验，随后更新
consumer 水位或退休未消费 transition，再返回同一 `symbol_scope` 快照。
消费方在自身市场锁内先应用返回的 token/epoch，再仅对 accepted 消息写缓存；
不应在该消息前另做 scoped 查询或再次调用 bool gate。后到帧尚未通过消费 gate
时，已接受当前帧的 `delivery_pending` 仍为 true。最终 candidate 检查继续使用
精确 state 查询。原 `is_current_bookticker_message` 复用同一 gate 内核保留兼容。
shadow B 的有序观察更新独立 joint 高水位及原始 coverage 时间，不逐帧重复
delivery 选择。A 的每帧选择保持不变；精确查询/全量查询仍立即检查各路资格，
原有周期 selector 维护其余观测空窗，不赋予 B 投递权限。

## 序号重置的显式恢复策略

全局 high-water 在普通 reconnect、物理 generation 更新、轮换、覆盖丢失及
移除后重新订阅期间都保留；新 socket 的低 `u` 不能自动清空它。真实交易所
序号重置持续 fail closed。仅在操作员独立核验交易所确有序号重置后，才允许
停止整个旧 logical feed、确认全部自有 worker/socket 已退出，创建新的实例，
重新获取新鲜有序 BBO，并让消费方通过新顶层 token 清空缓存和 candidate。
这是显式重同步入口；不能用单次低序号、重连次数、超时或清空字典代替核验。

## 轮换、限流与清理

默认 A 在 22h、B 在 23h 开始第一次轮换；之后每个替换实例在 22h 后轮换。
任意时刻仅一个 slot 替换。replacement 必须覆盖**最终订阅集**且 reconcile
已完成，才退出旧路；30s 超时放弃 replacement，保留旧路，60s 后可重试。
异步有界 cleanup worker 执行 close，不阻塞 selector。旧 socket cleanup
未确认完成时不创建下一 replacement，常驻/待退出/replacement 的 route manager 合计最多三个。底层 lifecycle 可能仍持有
更早 generation 的 socket thread；全部自有 generation 均须退出才确认清理成功，
不能据 manager 数量声称物理 sockets 至多三个。
close 失败或 worker 未退出会保留在 cleanup_pending/failures，不能当成成功。
Base lifecycle 仍保留原有吞掉物理 close 异常的 STOP/recovery 行为，但额外保留
仅含异常类型的内部清理结果；真实 socket close 曾失败时，线程退出也不被当作
物理清理成功。
STOP 为终态；close 中的 connect/update 不得使实例复活。

仅 public 同步控制消息每连接至少间隔 500ms，为协议 ping/pong 留余量。
这些 public 端点路由、pacing 和 jitter 修正也作用于 CEA 缺省 `single` manager，
不以启用 logical 冗余模式为前提；CEA 的配置缺省仍为 `single`。
public backoff 不清零，首档 1–1.25s，cap 档 29.75–30s；STOP 可打断等待。
private stream 的 backoff 与控制发送不变。同机双 socket 无法覆盖主机/网络
共同故障，不应宣称具有独立故障域。

## 无账户 shadow CLI

```bash
python -m pytradekit.ws.bookticker_shadow --symbols BTCUSDT,ETHUSDT --duration-s 60
```

只创建 public manager 和本地有界 sink，不读取配置、密钥或账户 client，不下单。
时长限定 1–3600s，币种最多 128 个（默认仅两个）；结束后最多额外观察清理 5s，无法确认退出则
非零退出并明确报告。输出逐币 joint/delivery unavailable 时长、原始时间年龄、
切换序号和顺序检查、有界恢复样本 P95/P99、ingress drops/depth、轮换及清理状态。
断流时长从已知原始 freshness 到期时刻开始计数；transport 中断的实际时间未知，
只能报告 selector/query 的采样检测边界。恢复样本包含初始订阅到首个合格报价；自然观察无断线不等于验证过恢复。
`delivery_outbox_depth/capacity` 与 `delivery_drops` 报告有界暂存与拒绝；逐币
`staged_highwater`、`enqueued_highwater`、`consumer_gated_highwater` 分别表示
暂存、成功进入消费 transport、通过消费者 generation/freshness gate，均不表示
业务信号发布成功。`delivery_pending` 表示暂存水位领先 consumer gate，供 CEA
最终 candidate 检查暂停落后缓存的发布，不会仅因 healthy burst 清空确认。
新暂存/gate 水位仅保留当前 targets，历史全局 ordering 水位
仍用于阻止 replay。cleanup 等待被 KeyboardInterrupt 打断也保留实际 feed 的
所有权与 pending 状态，输出 sanitized 非零结果。

独立 public probe 无 CEA candidate worker，默认明确标记 candidate counter 不可用。
如有已批准或离线 synthetic 的 `premium_work_metrics` JSON（由 `_log_work_diagnostics()` 输出），可通过
`--cea-diagnostics /path/to/diagnostics.json` 引入仅 allowlist 的 discard counts；
其他诊断字段和原文件路径不输出。CEA 的真实 handler/candidate 离线测试同时
验证 `public_feed_invalidation -> generation_changed` 计数。

本功能尚需独立 review 和 staged release 批准；此文档与 CLI 不授权生产变更。
