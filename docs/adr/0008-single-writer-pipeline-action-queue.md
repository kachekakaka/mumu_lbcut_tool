# ADR 0008: 采用单写者动作队列解耦录制管线与后台看门狗

- **状态**：Accepted
- **日期**：2026-11-02（修订于 2026-11-02）
- **决策者**：项目架构设计团队
- **关联上下文**：
  - [CONTEXT.md](../../CONTEXT.md)
  - [ADR 0005: 录播 Worker 采用 NDJSON 标准事件流作为外部控制契约](0005-ndjson-event-stream-contract.md)
  - [ADR 0006: 卡顿看门狗采用原地 Intent 刷新与周期切片保障音画同步](0006-stall-watchdog-and-av-sync.md)
  - [20261102-Worker稳定性加固与异常治理综合方案.md](../方案/20261102-Worker稳定性加固与异常治理综合方案.md)

---

## 1. 背景与问题陈述 (Context)

在原有实现中，后台 `StallWatchdog` 线程直接跨线程调用 `WorkerPipeline.stop_segment()` 和 `start_segment()` 进行切片自愈，而主控制线程同时保留自己的切片计时与切片索引变量。这造成了严重的并发数据竞争（F-03）：切片文件大小基线重置错乱、切片索引不一致、NDJSON 开始/结束事件乱序以及新旧看门狗线程重叠运行。
此外，若主控制循环因执行同步慢操作（如网络查询、截图、文件探测）发生阻塞，或者旧代际看门狗动作在新切片启动后迟滞到达，将破坏故障感知的时效性并造成新切片被误杀。

## 2. 决策内容 (Decision)

实行**录制管线单写者原则（Single-writer Pipeline Pattern）与有界动作队列机制**：
1. **控制权独占与非阻塞主循环**：录制管线（`WorkerPipeline`）的所有生命周期操作（创建、切片切换、强制终止）以及外部 NDJSON 切片事件的发射，严格由 Worker 主线程单线程串行执行。所有耗时外部操作（HTTP 查询、截帧、身份读取、ffprobe 校验等）均不得在主线程中执行长等待同步阻塞，必须通过受控后台只读任务或带剩余预算的可轮询调用完成，确保主循环以秒级 tick 可靠感知子进程生命周期；
2. **纯观察者看门狗与动作队列**：`StallWatchdog` 退化为纯观察者，仅负责周期采样与卡顿研判；当判定需要自愈切片或致命退出时，仅向线程安全的 `queue.Queue` 投递结构化动作请求（如 `ACTION_REFRESH_INTENT`、`ACTION_RECYCLE_SEGMENT`、`ACTION_FATAL_STALL`）；
3. **代际 ID 强校验与动作去重**：主线程维护单调递增的整数 `generation_id` 并向看门狗发布只读快照。看门狗动作请求必须携带采样时的代际 ID；主线程出队时，旧代际请求（`req.generation_id < self.generation_id`）一律丢弃，同代际内的重复自愈动作进行合并去重；
4. **严格状态流转与 STOPPING 优先级**：系统状态严格遵循主方案完整迁移矩阵（`PREFLIGHT → RECORDING | STOPPING`、`RECORDING → RECOVERING | STOPPING`、`RECOVERING → RECORDING | STOPPING`、`STOPPING → STOPPED`）。一旦进入 `STOPPING`，所有恢复与新切片启动逻辑被强行阻断；退出事件 reason 严格按主方案契约精确区分，不再混淆未确诊与明确错房、也不再将多种失败笼统归为同一原因；
5. **幂等清理收尾**：`cleanup()` 过程具备重入幂等保护，确保在任何退出路径下切片终态事件（`segment_finished` 或 `segment_aborted`）恰好发射一次，并对 OS 级别强杀（如 SIGKILL）明确保证边界。

## 3. 权衡与后果 (Consequences)

- **正面影响**：根除了多线程直接操作管线导致的状态撕裂、事件错位与死锁风险；切片编号与事件时序绝对单调递增；保证了新切片基线重置与进程句柄更新的原子性；代际过滤彻底解决了跨切片误杀问题；
- **负面影响与取舍**：看门狗请求的执行延迟取决于主循环 tick 粒度（通常在 1 秒以内），但在录制运维场景下秒级响应延迟完全可接受且极大提升了确定性。
