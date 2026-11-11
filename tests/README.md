# 测试入口

在仓库根目录、Python 3.11 环境中安装 `requirements-test.txt`。Windows 子进程与信号测试只在 Windows 运行；真实媒体夹具需要 PATH 中的 ffmpeg、ffprobe，缺失时对应测试会跳过，不能视为媒体验收通过。

每次使用新的仓外临时目录，避免 pytest 的 `--basetemp` 清理既有产物：

```powershell
python -m pip install -r requirements-test.txt
$Dev = Join-Path (Split-Path (Get-Location).Path -Parent) 'mumu_lbcut_tool_dev'
$Batch = 'console-tests-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$TestRoot = Join-Path $Dev ('mumu_lbcut_tool_tmp/' + $Batch)
python -m pytest tests -q -p no:cacheprovider --basetemp "$TestRoot/work" --junitxml "$TestRoot/results.xml"
```

控制台定向回归可把 `tests` 换为下列测试文件：

| 文件 | 验证边界 |
| --- | --- |
| `test_console_controls.py`、`test_scheduler_integration.py` | 真实 SQLite、本场意图、失败恢复、按主播停止、命令原子提交与幂等、跨来源迟到观测；设备与解析结果为注入边界 |
| `test_console_api.py` | FastAPI 请求/响应、严格参数、分页与筛选、历史关联、北京时间统计、告警、日志尾部和静态托管；测试日志与静态夹具均在临时目录 |
| `test_console_query_process.py` | 真实受控子进程；分阶段阻塞、IPC 截断、重复超时、容量与解析器进程复用、回收失败。网络阻塞为合成注入，不调用外网 |
| `test_console_lifecycle.py` | 独立 Windows 宿主；端口占用、双主控、初始化/调度异常、Ctrl+C/Ctrl+Break、强杀时仅回收查询 Job。模拟录制子进程不等于真实 MuMu |
| `test_real_process_delivery.py` | 真实 Worker 控制句柄、HTTP/CLI、合成媒体和事件收尾；模拟器与导航边界被替换 |
| `test_room_scanner.py`、`test_minimal_loop_regression.py` | 逐目标收取、容量不足不分配序号、UNKNOWN 保留可信状态及任务去重 |

自动化不启动真实 MuMu、不读取旧运行库。实机需另行确认允许的目标、实例与共享锁，使用独立数据和录像目录；验证命令结果后仍须核对任务、切片终态和 ffprobe，具体边界见[后端方案](../docs/方案/20261111-调度控制台后端接口实施方案.md#9-验收标准)。
