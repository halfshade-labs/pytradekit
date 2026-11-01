# 高频 public bookTicker 接收

同进程双路高频 bookTicker 会放大 Python 解码、socket 读取及 selector 的重复工作。
connected 与空应用队列不证明输入及时处理；应分别核对 source、接收、monotonic ingress 和 consumer。

固定 websocket-client 1.3.1 上的 Binance public 接收适配器保留原 ABNF 解析、控制帧、ping/pong 和 CLOSE 校验。
每次底层读取最多16KiB，复用原 recv_strict 的剩余缓存；缓存或 TLS pending 有数据时继续解析。
每连接复用一个 selector，空闲检查最长100ms，不合并或丢弃消息。
重组后的 TEXT 仍由 WebSocketApp 严格 UTF-8 decode 后进入回调，仅省去重复的 Python 逐字节扫描。
未修改全局库对象；私有成交、其他交易所、自定义 dispatcher 和显式 continuation callback 沿用原路径。

每条输入仍按原顺序做单符号选择和水位/代次校验。稳定物理状态下，重复全标的覆盖扫描最多每20ms一次，
与原空闲轮询周期一致；连接状态或目标变更立即重扫。完整查询及 consumer gate 继续即时核验，
不缓存可交易许可，不改变源/接收/单调年龄上限、shadow 权限、重复/乱序处理或失效 epoch。

回归使用实际 WebSocketApp 和 ABNF 解析器，通过内存传输核对顺序、分片 Unicode、非法编码、ping/pong、
关闭及读取上限。selector 测试在同一时点处理100条有序更新，确认逐帧交付而不重复全组扫描，
时间与连接状态变化仍触发覆盖核验。

性能对照必须匹配标的、时段和负载，包含消费者完整查询。profiler 与并行测试会影响调度。
实验中的陈旧输入、重连及清理未完成须保留；短期改善不能代替自然候选发布与连续24小时验证。

## 有预算的 public feed 读取

`BinanceWsManager.get_public_feed_state(*, read_budget=None)` 和
`is_current_bookticker_message(message, *, read_budget=None)` 支持可选的
`PublicFeedReadBudget`。不传预算时保留原来的阻塞加锁及返回语义；该参数不适用于
`get_public_feed_health` 的全覆盖扫描。业务调用方只使用公开 accessor，不读取或操作 manager 的私有锁。

```python
from threading import Event

from pytradekit.gateway.websocket.public_feed_access import PublicFeedReadBudget
from pytradekit.utils.exceptions import PublicFeedReadCancelled, PublicFeedReadTimeout
from pytradekit.utils.time_handler import get_monotonic_timestamp_ns

cancelled = Event()
read_budget = PublicFeedReadBudget(
    deadline_ns=get_monotonic_timestamp_ns() + 50_000_000,
    cancelled=cancelled.is_set,
)
try:
    state = manager.get_public_feed_state(read_budget=read_budget)
    current = manager.is_current_bookticker_message(message, read_budget=read_budget)
except (PublicFeedReadTimeout, PublicFeedReadCancelled) as error:
    logger.debug("public feed read rejected: %s; %s", error, error.note)
```

预算不可变，`deadline_ns` 必须是 `[0, 2**63 - 1]` 内的整数，拒绝 bool、float 和越界值。
它是 PTK monotonic 纳秒时钟的绝对截止点，不能使用 UTC/Unix 时间；重用预算不会续期。
`cancelled` 可以省略，提供时必须是无阻塞、无业务副作用的 callable，且每次返回严格的 bool；
`Event.is_set` 是支持的用法。错误的输入或返回类型抛出 TypeError/ValueError，谓词自身的异常原样传播。
取消状态采用采样检查，发起取消后必须保持锁存，直到本次读取完成；只有检查实际观察到的取消状态
才能拒绝结果。若在两次检查之间（包括快照读取期间）先 set 再 clear，该短暂取消可能被漏过，
本 API 不记录取消事件历史。

有预算时先检查预算，再尝试立即取锁，不为空闲锁增加轮询等待。竞争时每次有限等待最多10ms，
并始终使用同一个绝对截止点计算剩余等待；获得锁后先再次检查 monotonic 截止点。
锁内只执行原有的常数时间、无恢复副作用的状态读取，释放锁后再次检查截止点和取消状态，
只有最终检查通过才返回快照。取消谓词从不在状态锁内执行。若读取期间到期或取消，快照会被拒绝，
不会作为可用结果返回；若快照读取本身抛错，先释放锁，再保留原异常作为主异常。

`PublicFeedReadTimeout` 和 `PublicFeedReadCancelled` 都继承 `JwjException`；
异常消息说明失败类型，`.note` 包含本次 `deadline_ns`，诊断时应一起记录。
预算只中止取锁等待和结果接受，不中断任意外部代码、阻塞的用户谓词，也不会停止或恢复 manager、
关闭 socket、创建 accessor worker 或辅助线程。10ms 是单次竞争等待上限，实际响应仍受线程调度影响；
阻塞谓词没有有限响应保证。调用方应给自己的后续 cleanup 独立预算，不能拿已耗尽的读取预算继续清理，
也不能把一次读取超时当成 manager shutdown 已完成。
