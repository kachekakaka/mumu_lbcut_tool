# 当前直播 Fragment 身份核验与房间切换实机验证

- 日期：2026-11-06
- 设备：LAPTOP-42P5GQFB / MuMu Android 15 / 实例 2
- 验证性质：真实客户端只读导航；未录制视频
- 基线 HEAD：`8494d2b`；本轮改动未提交

## 结论

已证明抖音当前 `LivePlayFragment` 的 `mArguments` 会随实际播放房间切换而更新，并且在本次客户端版本中可同时提供当前 `room_id` 和主播 UID（`live.intent.extra.USER_ID`）。

因此 Worker 已增加轻量取证快速路径：先读取当前 `LivePlayActivity` 的 Fragment 参数，成功则直接返回身份凭证，不打开资料卡、不执行 UIAutomator、不点击屏幕；字段缺失或冲突时仍保留原资料页取证回退。

## 实机 A→B→A 结果

| 次序 | 直播间 | 调度目标 room_id | Fragment live_backend_roomid | Fragment USER_ID |
|---|---|---|---|---|
| A1 | 若琳Pepper | `7705059138493369142` | `7705059138493369142` | `510639081009251` |
| B | 小汐柚 | `7705056066937178907` | `7705056066937178907` | `4022475779746329` |
| A2 | 若琳Pepper | `7705059138493369142` | `7705059138493369142` | `510639081009251` |

三次均处于 `com.ss.android.ugc.aweme.live.LivePlayActivity`。每次 `EXTRA_ROOM_UNIQUE_KEY` 的实例序号分别为 `_1`、`_2`、`_3`。

### 重要细节

- B 房间的 `FROM_ROOM_ID` 仍然保留 A 房间 ID；但 `live_backend_roomid`、`ROOM_ID`、`USER_ID`、`EXTRA_ROOM_UNIQUE_KEY` 均指向 B。A2 同理，`FROM_ROOM_ID` 保留 B，而当前场次字段回到 A。
- 这验证了不能使用 `FROM_ROOM_ID` 作为当前房间依据；实现只使用当前 Fragment 的 `live_backend_roomid` 和 `USER_ID`，并交叉核对可用的 `ROOM_ID` 与 `EXTRA_ROOM_UNIQUE_KEY`。
- `dumpsys activity top` 中存在多个相同 Fragment 参数副本。解析器要求候选副本一致；出现冲突或 UID 缺失则返回 UNKNOWN，转入资料页回退。

## Worker 快速路径实测

重新冷启动/导航至若琳Pepper后，调用 `WorkerNavigator.observe_live_room()`：

- 当前 Activity：`LivePlayActivity`
- room_id：`7705059138493369142`
- anchor_uid：`510639081009251`
- evidence source：`live_fragment_arguments`
- 取证耗时：约 `984 ms`
- 未调用 UIAutomator、未点击资料卡、未截图

这表明当前版本可以把原本昂贵的资料页身份取证替换为一次 `dumpsys activity top` 读取。

## 代码改动

- `src/worker/room_locator.py`：新增 `current_live_fragment_evidence()`；忽略 `FROM_ROOM_ID`，读取当前 `live_backend_roomid + USER_ID`，校验可用的 `ROOM_ID`、`EXTRA_ROOM_UNIQUE_KEY` 及重复 Fragment 副本一致性。
- `src/worker/navigator.py`：`observe_live_room()` 首先尝试当前 Fragment 快速路径；成功直接返回，不执行 UIAutomator/头像点击；失败仍进入既有资料页取证路径。
- `tests/test_room_locator.py`：覆盖当前 Fragment 取证、FROM_ROOM_ID 与当前房间不一致、可选字段缺失、房间冲突、重复 Fragment 冲突、UID 缺失及非直播 Activity。
- `tests/test_worker_navigator.py`：覆盖快速路径不截图、不调用 UIAutomator、不点击 UI。
- `tests/test_render_overlay_acceptance_edges.py`：更新旧资料页回退测试以适配新增的 `dumpsys activity top` 读取。

## 自动化验证

- 定向回归：`61 passed`。
- 全量测试：`222 passed in 71.47s`。
- `py_compile`：通过。
- `git diff --check`：通过。

## 限制与上线建议

1. 当前证据来自 MuMu Android 15 和本次安装的抖音版本；系统/客户端升级后应保留 UNKNOWN 回退路径。
2. `dumpsys activity top` 属于 Android 调试输出，不是抖音稳定公开 API；解析器必须严格校验，不能从启动 URL 或 `FROM_ROOM_ID` 推断当前房间。
3. `USER_ID` 本次在 A/B/A 三个房间均与调度平台解析的主播 UID 一致，但仍应在更多直播类型（连麦、PK、团播、付费直播、横屏等）做兼容性抽样。
4. 当前实现读取 `dumpsys activity top` 并依赖当前焦点 Activity；如果后续发现后台 Fragment 或多窗口导致字段混淆，应进一步限定当前 resumed Activity/task 的 Fragment 范围。
5. 本次验证的是身份取证快速路径，不等于完整录制质量验收；尚未验证身份读取后的录制启动、持续录制和异常恢复。

## 当前工作区与设备

- 没有提交或推送。
- 保留此前工作区已有的修改，没有 reset/checkout。
- MuMu 实例 2 已通过项目 `MuMuClient.shutdown_player(2)` 关闭，并确认 `is_process_started=False`、`is_android_started=False`。
- MuMu 实例 3 未启动。
- 本轮未产生录像、未修改录像或业务数据库。

证据目录：`mumu_lbcut_tool_tmp/20261106-room-switch-proof/`（A/B/A 三次 dumpsys、截图、结构化 summary）；真实快速路径脚本：`verify_live_fragment_fast_path.py`。