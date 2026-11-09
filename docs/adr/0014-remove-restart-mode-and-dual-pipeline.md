# 0014 删除 restart 诊断模式与交叠双管线

## Status
accepted（2026-11-09 决定并实现；6～8 小时音画漂移长测尚未进行，见[待办](../待办.md)）

## Context
restart 模式每片重启一次 scrcpy，最初目的有两个：限制崩溃时的损失（[ADR 0002](0002-time-segmented-mp4-recording.md)），以及每片让音视频时间轴归零以截断累积漂移（[ADR 0006](0006-stall-watchdog-and-av-sync.md) 第 3 条）。交叠双管线（[ADR 0009](../../archive/docs/adr/0009-overlapping-dual-pipeline-seamless-slicing.md)，已归档）是为了消除重启空档而加上的。[ADR 0012](0012-continuous-capture-segment-muxer.md) 的持续采集已经让每片成为独立 MP4，且切片不重启采集、没有空档；restart 防漂移从未被实测证明有效。2026-11-08 的 60 分钟持续采集中，各片音视频时长差 20～103 ms，没有随时间增大的趋势（不是严格的同步测量）。

## Decision
删除 restart 模式、交叠双管线及其调度参数，只保留 continuous 一条录制路径。若 6～8 小时长测证实存在累积音画漂移，再在持续采集中按固定间隔主动重建采集会话来处理，不恢复 restart 模式。

## Consequences
- 冻结检测只需支持持续采集一种方式（见 [ADR 0013](0013-freeze-detection-from-captured-stream.md)）。
- 代码删除后，ADR 0009 移入 `archive/docs/adr/`，ADR 0006 第 3 条随之失效。
- 6～8 小时长时音画同步仍未验证。
