# 维护脚本

本目录维护 8 个录制相关命令和 1 个只读文档检查入口；历史源码见[归档说明](../archive/scripts/README.md)。联网和设备入口须显式传入 `--execute`；省略时仅作本地预览，不报告设备空闲或运行依赖已就绪。

## 入口与参数

| 入口 | 必需参数 | 用途和执行副作用 |
| --- | --- | --- |
| `check_docs.py` | 无 | 检查工作区 Markdown 相对链接与标题锚点；范围和返回结果见[文档维护与检查](../docs/README.md#文档维护与检查) |
| `launch_vm.py` | `--vmindex` | 启动明确指定实例、连接确认的 ADB 地址；不自动关闭实例 |
| `dump_screen.py` | `--vmindex --output-dir` | 对已启动实例导出新的 UI XML 和焦点；本次设备临时文件读取成功后才清理 |
| `save_screenshot.py` | `--vmindex --output-dir` | 对已启动实例截图，验证 PNG 后保存；不自动开机 |
| `parse_ui.py` | `--input` | 离线 XML；`--filter all|top|controls`，顶部阈值 `--top-y 300`；不拉取、不点击 |
| `inspect_recording.py` | `--input` | 单个媒体文件的轨道、时长信息；时长差不是音画同步测量 |
| `inspect_events.py` | `--input` | NDJSON 统计和生命周期序列，报告坏行；不推导未记录的业务事实 |
| `scan_samples.py` | `--input` | 当前 RoomResolver 查询；`--workers` 默认 2、上限 10；不操作设备 |
| `verify_worker_e2e_live.py` | `--vmindex --target --output-dir` | 指定目标的 Worker 短录制和主动中断；取消自动选房/旧房间兜底 |

可从其他目录用脚本绝对路径调用；用户相对路径按调用目录解析。以下示例默认只预览，变量由调用者明确指定：

```powershell
py -3.11 scripts/launch_vm.py --vmindex $Instance
py -3.11 scripts/save_screenshot.py --vmindex $Instance --output-dir $EvidenceRoot
py -3.11 scripts/verify_worker_e2e_live.py --vmindex $Instance --target $WebRid --output-dir $EvidenceRoot
```

## 依赖、输出和占用

Python 3.11 与根目录 requirements；没有 OpenCV 依赖。设备工具按现有安装发现规则寻找 MuMu/ADB。媒体检查使用[固定配套 FFmpeg/ffprobe](../docs/最小调度运行说明.md#固定媒体工具部署)，不从 PATH 回退。Worker 验证要求本地 scrcpy.exe 和 scrcpy-server，使用 `--require-local-tools`，不下载或替换工具。

UI、截图和 Worker 输出必须位于仓库之外，每次创建独立批次，不覆盖旧报告、不删除旧录像。失败材料保留。纯离线分析写标准输出。截图验证需要已有 Pillow，事件分析支持 UTF-8 BOM 和 UTF-16 BOM，损坏输入明确报告。

UI dump 与 Worker 共用 `src/mvp/ui_dump.py`，通过系统 `/system/bin/app_process32` 和明确的 CLASSPATH 启动 UIAutomator，避开实测的默认 64 位退出崩溃。需要镜像内已有该 32 位入口和配套系统 JAR；缺失或失败时不下载、不回退到默认入口。命令只设置本次进程环境，不修改系统文件或模拟器全局设置。

UI dump 非零时，即使本次唯一 XML 可解析，独立脚本也只留存为失败材料并返回错误；Worker 则将本次界面读取判为不可用，不读取 XML 或据此点击。两者都不将“已写出 XML”的提示当作命令成功。实机覆盖及剩余项见[T14 验证记录](../docs/证据/验证/20261117-T14脚本整理与安全边界验证.md)。

设备入口使用与 Worker 相同的实例锁；同一共享锁目录内的占用会拒绝执行。其他安装副本、手工 ADB 和录制进程不受此锁保证，实机操作前必须核对实际部署及活跃任务，验证窗口内不能从其他程序操作该实例。管理映射不足或冲突时拒绝，不按默认端口猜设备。截图/UI 不支持绕锁取证。

MuMu 管理命令、ADB 连接/读取和媒体命令均有超时。超时回收仅针对本次客户端，不终止共享 ADB server；冷启动取消不撤销已经提交给 MuMu 的开机请求。

## Worker 验证的边界

默认 `--target-kind web_rid`，只独立查询指定网页入口，确认在播和 room_id/UID 后才启动 Worker。内部场次可使用 `--target-kind room_id --expected-anchor-uid`，可另提供 `--web-rid`。默认要求确认身份；显式 `--allow-unconfirmed-identity` 不放行可靠冲突，也不计作正常确认身份实机验收。

切片默认 20 秒，观察默认 50 秒；`--segment-duration >= 5`，且 `2 * segment_duration + 10 <= record_seconds <= 600`。`--navigation-timeout` 默认 300 秒，范围 60～900。从首次切片启动开始计时，观察到期有正常非最终片且后续片仍活动时发送停止。未满足条件也有界收尾，不无限续录凑片数。正常通过须有配对的切片事件、非最终片和停止后的唯一最终片、有效音视频轨、正常停止原因、无异常/恢复/质量告警；退出码为 0 不是充分条件。

本入口会启动指定实例、设置静音/帧率/渲染策略、置底窗口并导航；进入应用操作阶段后，收尾会关闭该实例抖音。不会自动还原原直播页/设置或关机。父进程不重复取得 Worker 的锁。超时先平滑停止，再只处理已确认归属的 Worker 和专属采集/分析进程，强制停止算失败。不能确认的残留进程只报告，不扩大清理范围。

切片按同一 run_id 和 segment_index 唯一配对：开始事件指向 `capture_*/part_*.mp4`，完成事件指向任务目录内封口后的正式文件；两者名称按当前协议核对，不要求已经改名的临时文件仍存在。

批次中保留原始 events.ndjson、worker.log、fallback.ndjson（仅需要时产生）、切片、result.json 或 failure.json。报告不代表完整解码、音频隔离、画面无遮挡、音画同步或长稳验收。

## 输入及退出结果

扫描 JSON 为非空数组，至少每项含整数 `id` 和字符串 `entry`；接受 web_rid 或完整直播网页链接，拒绝短链、重复 id 和规范化后重复入口。整份输入校验通过后才联网，单项失败不丢其他结果；UNKNOWN 不当作 ENDED。

| 退出码 | 意义 |
| --- | --- |
| 0 | 本次检查成功，或明确标为未执行的预览；Worker 验证仅正常通过使用 0 |
| 2 | 命令行参数缺失或组合无效 |
| 3 | 输入/依赖/占用/设备/外部命令等执行错误，错误原因写 stderr；失败材料可用时保留 |
| 4 | 分析不完整、缺轨、坏事件行、扫描 UNKNOWN，或 Worker 验收有告警/失败/条件未满足；查看具体报告 |
| 130 | 被 KeyboardInterrupt 取消 |

实机窗口内的信号取消可能以操作取消错误结束，不能将任何非零结果当作测试成功。隔离测试入口及覆盖见[测试说明](../tests/README.md)。
