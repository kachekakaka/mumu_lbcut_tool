# 历史脚本源码

2026-11-17 按 T14 方案从 scripts/ 归档，来源基线提交为 `4a71935`。以下 19 个文件保留原源码文本，扩展名为 `.py.txt`，不作为运行或测试入口；不要将其改名或交给 Python 执行。原命令仅用于解释历史证据。现行工具见[维护脚本](../../scripts/README.md)。归档没有迁移/清理现有 JPG、XML、缓存、录像和日志。

| 原文件（归档后追加 .txt） | 用途与风险 | 现行承接 |
| --- | --- | --- |
| analyze_frames_cv.py | 固定 Gemini 截图的颜色/边缘判断，结论有限 | 仅历史方法 |
| analyze_gate_record.py | 固定准入录像抽帧、覆盖图片 | 无专用入口 |
| analyze_img.py | 固定截图统计 | 截图工具验证 PNG 有效性 |
| analyze_seg001_timeline.py | 固定历史切片时间线、覆盖 JPG | 无专用入口 |
| analyze_verify_fix.py | 固定修复样本抽帧 | 无专用入口 |
| bench_compare.py | 实例 2 滑动/静态对比、删同名录像 | 不保留滑动入口 |
| check_video_frames.py | 遍历整个临时目录，覆盖报告，备用抽帧参数错误 | 不恢复批量扫描 |
| find_live_now.py | 固定候选网页正则解析 | scan_samples.py |
| inspect_popcorn.py | 单个固定网页 ID/状态探针，无超时 | 仅历史方法 |
| inspect_soak_results.py | 固定压测目录、个人 ffprobe | inspect_recording.py 单文件检查 |
| inspect_top_nodes.py | 固定 UI 顶部节点筛选 | parse_ui.py |
| list_records.py | 固定临时目录递归录像列表 | 无全目录扫描入口 |
| read_dump.py | 固定 XML 文本提取 | parse_ui.py |
| verify_10min_results.py | 固定十分钟样本汇总；不能证明连续性 | inspect_recording.py 单文件检查 |
| verify_concurrent_scrcpy.py | 单实例双采集交叠、删旧录像 | 旧采集调查，不恢复交叠管线 |
| verify_dual_isolation.py | 旧 MVP 双实例、无锁、默认目标和 Enter | 当前 Worker；不承诺音频隔离验收 |
| verify_final_frames.py | 固定样本抽帧/复制，无遮挡结论过强 | 仅历史方法 |
| verify_room_fps_e2e.py | 固定实例、递归删 run、帧差估算 FPS | 当前 Worker 录制/封口；不承接动作 FPS |
| verify_single_stream.py | 旧 MVP 单录、默认实例、Enter | verify_worker_e2e_live.py |
