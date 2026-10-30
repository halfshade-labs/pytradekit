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
