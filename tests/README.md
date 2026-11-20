# 测试入口

| 检查 | 运行目录 | 命令和条件 |
| --- | --- | --- |
| 文档链接 | 仓库根目录 | `python scripts/check_docs.py`，只需 Python 标准库和 Git。[覆盖范围](../docs/README.md#文档维护与检查) |
| Python 测试 | 仓库根目录 | 选择已安装 `requirements-test.txt` 的 Python 3.11，再按下节运行 pytest。可用 `python -c "import sys; print(sys.executable)"` 确认当前解释器。 |
| 前端测试 | `web/` | 首次或依赖变化后 `npm ci`，再用 `npm test`；项目已用 Node 24 和 npm 11 验证。 |
| 前端类型检查 | `web/` | 同一套前端依赖，`npm run typecheck` 单独检查类型，`npm run build` 在类型通过后生成生产包。 |

## 后端测试

在仓库根目录、Python 3.11 环境中安装 `requirements-test.txt`。Windows 子进程与信号测试只在 Windows 运行；真实媒体夹具使用与产品相同的固定 Gyan FFmpeg/ffprobe，按[运行说明](../docs/最小调度运行说明.md#固定媒体工具部署)部署。缺少这套工具会明确失败，不通过 PATH 替代或跳过来宣称媒体验收通过。

每次使用新的仓外临时目录，避免 pytest 的 `--basetemp` 清理既有产物：

```powershell
python -m pip install -r requirements-test.txt
$Dev = Join-Path (Split-Path (Get-Location).Path -Parent) 'mumu_lbcut_tool_dev'
$Batch = 'console-tests-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$TestRoot = Join-Path $Dev ('mumu_lbcut_tool_tmp/' + $Batch)
New-Item -ItemType Directory -Path $TestRoot | Out-Null
python -m pytest tests -q -p no:cacheprovider --basetemp "$TestRoot/work" --junitxml "$TestRoot/results.xml"
```

控制台定向回归可把 `tests` 换为下列测试文件：

| 文件 | 验证边界 |
| --- | --- |
| `test_bilibili_bridge.py`、`test_bilibili_host.py`、`web/tests/bilibili.test.ts` | 合成 Core 回包、SQLite 占用与恢复、本机访问限制、并发续领；Windows 专属 venv 验证双主控启动同一无窗口子进程。真实手机扫码单独验收。 |
| `test_segment_uploads.py` | 独立 SQLite、切换部署、并发确认、步骤和代次失效、提交后移走录像并查完分页、拒绝后的修订和旧弹窗失效。真实 ASGI 路由覆盖争议片隔离、登记与提交间的冲突、领取取消和等锁时的响应。Core 回包由测试控制。 |
| `test_script_worker_process.py` | 真 Windows 子进程、合成媒体和实际 Ctrl+Break；脚本正常停止/最终片判定及忽略信号后的有界强制回收，不操作设备 |
| `test_script_safety.py` | 脚本导入/帮助/预览无设备副作用，独立输出、离线解析、严格实例映射、有界客户端、取消及验收反例；设备/网络为替身，专属睡眠进程测试超时不涉及 MuMu；不能替代实机验收 |
| `test_ui_dump_runtime.py`、`test_room_locator.py`、`test_navigation_budget.py` | 32 位 UI 导出命令、路径注入拒绝、精确 serial 和指标归类；非零/缺入口/超时不误读或点击，唯一文件及预算约束；设备为替身 |
| `test_worker_state_machine.py`、`test_worker_lifecycle_state.py` | ADR 0008 全部状态对、非法迁移不改变原状态、重复状态幂等；真实 Controller 的早退、正常结束、恢复失败与异常收尾状态顺序，设备和采集边界为替身 |
| `test_console_controls.py`、`test_scheduler_integration.py` | 真实 SQLite、本场意图、失败恢复、按主播停止、命令原子提交与幂等、跨来源迟到观测；设备与解析结果为注入边界 |
| `test_console_api.py` | FastAPI 请求/响应、严格参数、分页与筛选、历史关联、北京时间统计、告警、日志尾部和静态托管；测试日志与静态夹具均在临时目录 |
| `test_console_query_process.py` | 真实受控子进程；分阶段阻塞、IPC 截断、重复超时、容量与解析器进程复用、回收失败。网络阻塞为合成注入，不调用外网 |
| `test_console_lifecycle.py` | 独立 Windows 宿主；端口占用、双主控、初始化/调度异常、Ctrl+C/Ctrl+Break、强杀时仅回收查询 Job；浏览器替身发起真实 HTTP 请求，覆盖慢初始化、健康旧服务冲突、缺前端、打开失败及阻塞时仍可退出。模拟录制子进程不等于真实 MuMu |
| `test_console_browser.py` | CLI 默认值及传参、IPv4/IPv6 访问地址、HTTP 启动顺序、关闭/失败守卫、单次派发、线程和系统打开失败；浏览器调用为替身，不弹出真实浏览器 |
| `test_real_process_delivery.py` | 真实 Worker 控制句柄、HTTP/CLI、合成媒体和事件收尾。覆盖 Popen 后未写 PID 的主控退出、旧实例占用、两段回放及 SQLite 等锁、超时和提交故障后的服务恢复。模拟器与导航为隔离边界。 |
| `test_room_scanner.py`、`test_minimal_loop_regression.py` | 逐目标收取、容量不足不分配序号、UNKNOWN 保留可信状态及任务去重 |
| `test_pinned_media_tools.py` | 固定路径、旧版与不同构建拒绝、配套配置不一致拒绝、依赖失败前不启动设备；进程执行为替身 |
| `test_aac_without_keyflag.py` | 独立源 MKV、真实命名管道和分析器重启，逐流比较全部编码包、检查 DTS 和完整解码。负对照包括移除 AAC 兼容参数、传输时丢失或重复媒体块。 |
| `test_continuous_real_transport.py`、`test_stream_monitor_media.py` | 固定版本真实媒体传输、总时长封口与分析分支重建；循环输入可能有 AAC padding，CSV 相邻端点不替代逐包无漏失校验 |
| `test_runtime_identity.py` | 运行期双样本、冲突宽限与失证期限、开录证据过期、同 Activity 错房恢复、迟到观察、风险回标与受控边界重建；固定时钟及设备注入不能替代实机时延验收 |
| `test_quality_delivery.py`、`test_worker_events.py` | 质量事件编号／无编号重投、日志回放同编号、正常迟到回标、未知／未完成切片完整性异常、原任务与新任务隔离、事务回滚；调度队列不中断由 `test_scheduler_integration.py` 补充 |
| `test_worker_executor.py`、`test_mumu_client.py`、`test_dep_manager.py` | Windows 独占命令回收树、共享客户端继承管道、无效或失效 HWND、跨进程安装锁、残缺工具与中途退出。 |
| `test_continuous_and_overlay.py`、`test_live_surface_probe.py` | 实际采集命令参数、空页面探针与尾片、清单增量读取和替换。逐轮统计 32 至 1,024 条历史下的访问量和处理时延。 |

### 旧测试承接入口

| 精简的旧断言 | 当前入口及会检出的退化 |
| --- | --- |
| `StreamEndDetector`、`HealthMonitor` 及旧命名格式 | `test_scheduler_integration.py` 交错未知与确认下播，误停会失败。真实分析与看门狗见 `test_stream_monitor_media.py`、`test_worker_controller.py`，冻结或停涨没触发应有动作会失败。成品隔离以 Controller 两次运行后实际文件可读为准。 |
| 通过 `shutil.which` 模拟缺 ffprobe、重复短尾与归档保护 | `test_pinned_media_tools.py` 从真实依赖入口移除固定 ffprobe，并核对未启动设备。`test_segment_integrity.py` 用真实 ffprobe 检出无效媒体、接受短尾并保护已存在的成品。 |
| 源码文本中的采集参数和清理位置 | `test_continuous_and_overlay.py` 截获生产入口的两条命令，缺少 scrcpy 参数或强制帧率会失败。`test_identity_stability.py` 记录实际 ADB 输入，额外点击会失败。 |
| 多个同义查询阻塞名、顺序拿锁和混合故障集合 | `test_console_query_process.py` 保留无响应与半行两种超时，随后须能查询成功。真正错房由 Controller 入口验证，损坏媒体由固定 ffprobe 验证。两个同时存活的进程争用实例锁见 `test_naming_lock_observability.py`。 |
| 旧切片上传状态列 | `web/tests/upload.test.ts` 挂载现行切片页，验证正常片可安排、冲突片保留原单查询及刷新失败保留状态。`test_segment_uploads.py` 覆盖旧弹窗确认和派发竞争。旧模拟状态配色在同文件保留一组参数。 |

质量标记的 HTTP 非法时间投影、固定时钟跨 24 小时窗口和任务摘要见 `test_console_api.py` 的 `test_quality_*`。原始风险经存储写入，实际列表请求必须成功；单片是内部查询入口，使用同一响应模型验证，不假定存在单片 GET 接口。历史实施和页面联验结果见 [T20 验证记录](../docs/证据/验证/20261117-T20质量标记去重与汇总实施验证.md)。

浏览器启动定向回归运行 `test_console_browser.py`、`test_console_api.py`、`test_console_lifecycle.py`。夹具 `console_host.py` 默认替换浏览器并使用临时静态目录；仅人工桌面验收显式指定 `--open-browser --browser real --static-root <已构建前端目录>`，设备仍为空闲替身。候选部署入口、真实浏览器及远端验证边界见 [T19 验证记录](../docs/证据/验证/20261117-T19控制台启动实施验证.md)。

T16 的隔离浏览器验证使用生产前端、实际 Core API、独立数据与上传 Worker。故障注入和证据位置见 [T16 验证记录](../docs/证据/验证/20261118-T16切片页手动安排上传验证.md)。

## 前端测试

Node.js 环境、依赖安装和开发服务见[运行说明](../docs/最小调度运行说明.md#控制台网页)。在仓库根目录进入前端目录，已安装依赖后执行：

```powershell
cd web
npm test            # Vitest 单元／组件测试
npm run build       # Vue TypeScript 检查与生产构建
```

脚本定义以 `web/package.json` 为准。组件测试使用 jsdom 与接口替身，不打开真实录制机桌面、不证明浏览器布局或手机访问已通过；构建成功也不替代页面联调。按改动核对[当前页面交互](../docs/设计说明.md#控制台页面与交互)和[接口契约](../docs/控制台接口契约.md)。

`quality_flags.test.ts` 核对真实提示组件接收的完整条数、疑似时间与未知条数文案，包括同内容多条、零值、非有限值和名称分组；`job_identity.test.ts` 核对轮询刷新后的风险保留、告警完成和完整性失败原因。

## 实机验收与记录

自动化不启动真实 MuMu、不读取旧运行库。实机需另行确认允许的目标、实例与共享锁，使用独立数据和录像目录；验证命令结果后仍须核对任务、切片终态和 ffprobe。[历史后端验收表](../archive/docs/方案/20261111-调度控制台后端接口实施方案.md#9-验收标准)可作为覆盖参考，当前预期行为以需求、设计和接口契约为准，未验项目以[待办](../docs/待办.md)为准。

实机测试选取目标时，可先核对[历史候选名单](fixtures/sample_rooms.json)及当次授权范围；名单只提供入口，不代表目前在播或获准使用。重新确认目标状态及实例占用后再执行。接力长测由调度器按任务换播，外部巡检按风险和异常调整；计时、连续采集及音画同步可参考[六小时长测方法](../docs/证据/验证/20261112-六小时长稳测试验证.md#检查方法与验收边界)，其中的代码版本、脚本试跑和当次结果只代表历史批次，不自动作为新测试的通过证据。

审查及测试时按改动选择相关边界，例如启动中停止、依赖调用异常、失败重试、迟到结果和部分成功；在对应测试中核对实际状态与结果，不以正常路径通过代替异常分支验证。
