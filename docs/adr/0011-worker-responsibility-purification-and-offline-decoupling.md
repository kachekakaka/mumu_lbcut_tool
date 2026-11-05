# 0011 录播 Worker 职责纯化与外网解耦

为了彻底消除录播执行单元（Worker）与调度平台（Orchestrator）之间的职责越界与功能重叠，决定将直播间元数据解析（标题/昵称/封面）以及开播/下播状态检测的主权完全收归调度平台。Worker 内部彻底剥离所有外网 HTTP/HTTPS 请求代码，退化为纯本地运行、零外网依赖的执行单元；完整保留基于本地 ADB 的卡顿看门狗、像素差分、原地 Intent 刷新自愈与弹窗消解能力；通过标准操作系统中断信号响应调度器的下播封口指令。

## Status
accepted

## Context
在早期单兵作战设计中，Worker 内部集成了基于 `requests` 的短链 302 重定向与网页 HTML 爬取逻辑（`WorkerRoomResolver`），并尝试在录制期间通过网络与屏幕文字复合判断下播。这不仅导致 Worker 产生不必要的外网网络依赖、启动时容易因网络延迟或抖音反爬风控卡死，而且与调度平台基于类似 f2 机制的高并发巡检能力严重重叠。此外，下播状态的主权分散导致系统切片生命周期管理混乱。

## Decision Details
1. **外网 API 彻底剥离与离线自适应唤起**：下线 Worker 内部的公网 HTTP 请求模块，Worker 启动时直接通过 CLI 参数接收标准目标标识（`--target <target>`）与归档输出目录（`--output-dir`）。调度平台在日常调度中直接传递解析好的 19 位真实内部 `room_id`，Worker 走 `snssdk1128://live?room_id=...` 秒级直达；在手工调试传入短号或网页 URL 时，Worker 纯离线自适应 fallback 唤起带包名的网页深度链接（`am start -a android.intent.action.VIEW -d https://live.douyin.com/{web_rid} -p com.ss.android.ugc.aweme`），兼顾自动化性能与人工调试宽容度。
2. **纯本地核心能力保留与前置门禁硬卡口**：Worker 完整保留其特有的纯本地 ADB 守护机制：
   - 本地 MP4 文件增长检测；
   - 前置内容门禁（Pre-flight Content Gate）必须首先通过微秒级 Android 前台 Activity 类名硬卡口校验（确认真正进入 `LivePlayActivity`，若停留在 `MainActivity` 推荐短视频流直接阻断拒绝盲录），再结合首帧白底遮罩检测与像素帧差动态检测；
   - 本地 ADB 截屏像素差分与颜色方差看门狗（Visual Stall Watchdog）；
   - 遭遇 10 秒卡死时的本地 ADB Intent 原地重拉自愈（`snssdk1128://live?room_id=...`）；
   - 本地 UI 树弹窗识别与模拟点击跳过（Popup Dismiss Matrix），纳入“未成年人模式/青少年模式”自动点击“不再提醒”，并包含冷启动闪屏预热保护（3.5 秒）。
3. **下播主权与停机封口协同**：开播与下播的判定主权 100% 归属于调度平台。调度平台通过 API 检测到下播后，通过向 Worker 发送 Windows 标准中断信号（`CTRL_BREAK_EVENT` / `SIGINT`）通知其平滑停机；Worker 捕获信号后指挥底层录制管线完成 MP4 尾部索引（`moov atom`）的安全写入封口，随后正常退场。
4. **切片过滤职责让渡**：Worker 始终忠实记录并上报所有落盘的 MP4 切片绝对路径；对于下播时产生的微短碎片切片（如小于 15 秒），由调度平台在投递外部上传服务（如 biliup 服务）前执行过滤，Worker 自身不增加业务过滤逻辑。

## Considered Options
- **Worker 内部保留网络双重兜底**：虽然理论上能提供单兵容错，但实际上引入了双重状态不一致的隐患，且无法摆脱外网风控带来的阻塞风险。
- **由 Worker 判定下播并主动退出**：屏幕文字识别极易受直播间弹幕、PK 画面干扰而误判下播，导致正在进行的直播被误杀；调度平台通过官方 Web 接口探测状态准确率达 100%，由调度平台单点决策下播是最稳健的方案。
