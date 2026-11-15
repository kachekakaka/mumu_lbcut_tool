# 测试入口

## 后端测试

在仓库根目录、Python 3.11 环境中安装 `requirements-test.txt`。Windows 子进程与信号测试只在 Windows 运行；真实媒体夹具使用与产品相同的固定 Gyan FFmpeg/ffprobe，按[运行说明](../docs/最小调度运行说明.md#固定媒体工具部署)部署。缺少这套工具会明确失败，不通过 PATH 替代或跳过来宣称媒体验收通过。

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
| `test_pinned_media_tools.py` | 固定路径、旧版与不同构建拒绝、配套配置不一致拒绝、依赖失败前不启动设备；进程执行为替身 |
| `test_aac_without_keyflag.py` | 合成无关键标记 AAC 输入、旧命令丢音轨负对照、真实命名管道分段、所有编码包逐字节一致、逐流 DTS 单调、所有分段完整解码；不是 MuMu 实机 |
| `test_continuous_real_transport.py`、`test_stream_monitor_media.py` | 固定版本真实媒体传输、总时长封口与分析分支重建；循环输入可能有 AAC padding，CSV 相邻端点不替代逐包无漏失校验 |

## 前端测试

Node.js 环境、依赖安装和开发服务见[运行说明](../docs/最小调度运行说明.md#控制台网页)。在仓库根目录进入前端目录，已安装依赖后执行：

```powershell
cd web
npm test            # Vitest 单元／组件测试
npm run build       # Vue TypeScript 检查与生产构建
```

脚本定义以 `web/package.json` 为准。组件测试使用 jsdom 与接口替身，不打开真实录制机桌面、不证明浏览器布局或手机访问已通过；构建成功也不替代页面联调。按改动核对[当前页面交互](../docs/设计说明.md#控制台页面与交互)和[接口契约](../docs/控制台接口契约.md)。

## 实机验收与记录

自动化不启动真实 MuMu、不读取旧运行库。实机需另行确认允许的目标、实例与共享锁，使用独立数据和录像目录；验证命令结果后仍须核对任务、切片终态和 ffprobe。[历史后端验收表](../archive/docs/方案/20261111-调度控制台后端接口实施方案.md#9-验收标准)可作为覆盖参考，当前预期行为以需求、设计和接口契约为准，未验项目以[待办](../docs/待办.md)为准。

实机测试选取目标时，可先核对[历史候选名单](fixtures/sample_rooms.json)及当次授权范围；名单只提供入口，不代表目前在播或获准使用。重新确认目标状态及实例占用后再执行。接力长测由调度器按任务换播，外部巡检按风险和异常调整；计时、连续采集及音画同步可参考[六小时长测方法](../docs/证据/验证/20261112-六小时长稳测试验证.md#检查方法与验收边界)，其中的代码版本、脚本试跑和当次结果只代表历史批次，不自动作为新测试的通过证据。

审查及测试时按改动选择相关边界，例如启动中停止、依赖调用异常、失败重试、迟到结果和部分成功；在对应测试中核对实际状态与结果，不以正常路径通过代替异常分支验证。
