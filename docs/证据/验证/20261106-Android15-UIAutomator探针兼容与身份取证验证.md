# Android 15 / MuMu UIAutomator 探针兼容与身份取证验证

- 日期：2026-11-06
- 基线：`8494d2b`（`origin/main`）；工作区保留既有未提交改动。
- 结论：定位到 UIAutomator 在 MuMu Android 15 动态页面上的 idle 等待失败；已调整调用与取证回退，但实机身份闭环仍未验收。

## 根因证据

1. 在 MuMu 桌面执行 `uiautomator dump` 时可返回 139，但 XML 文件仍完整落盘；直接调用后独立 `cat` 能读取约 28 KB hierarchy。
2. 将命令放进 `adb shell sh -c` 会出现参数/启动异常，无法可靠产出 XML。代码已改为直接调用 `shell uiautomator dump <unique-path>`，再独立读取、校验和清理。
3. 在抖音直播动态页面，UIAutomator 经常返回 `ERROR: could not get idle state`、退出码 139，且不生成 XML。重复调用 4 次均复现；这不是 XML parser 错误，而是 UiAutomation 等待页面 idle 失败。
4. 单次偶发成功的 live dump 曾返回约 120 KB XML，包含主播昵称和 `avatar_container`；所以不能简单认定 Android 15 上 UIAutomator 完全不可用。

## 代码调整

- UI dump 改为直接启动 UIAutomator，独立读取唯一临时文件；进程非零退出时仍检查 XML 是否完整，只有缺文件/无效 XML 才失败。
- 动态直播页每轮最多请求一次 UI dump；失败后切换截图证据，不再重复调用 UIAutomator 等待 idle。
- 主播入口优先从 `avatar_container` 的 bounds 定位，避免点击昵称文字或关注按钮。
- 资料卡只在截图确认后才标记为打开；入口点击后未确认时保留页面，不发送 Back，也不在收尾阶段再次 dump。
- 资料卡截图回退只在竖屏且底部浅色 sheet 特征满足时点击头像；无法确认则保持 UNKNOWN，不盲点。

## 自动化验证

- 定向回归：61 项通过。
- 全量测试：217 项通过，0 失败，57.97 秒。
- `git diff --check` 通过。

## 实机验证结果与限制

- 候选 A「土小逗」：第一次解析为 LIVE，后续测试时已下播；截图显示“直播已结束”。
- 候选 B「锦钰原木生活精品馆」：开始测试时解析为 LIVE，随后两轮身份观察均未确认资料卡；再次解析已返回 ENDED。
- 因候选直播结束，未取得新的 UID/room_id 身份闭环；不能宣称探针修复已实机通过，也未开始录制。
- 第二轮导航性能统计：4 次 UI dump 合计 16.6 秒，均失败；8 次截图合计 20.2 秒。说明失败探针仍昂贵，后续应进一步降低 live 页面 UI dump 的优先级或采用可靠的替代读取通道。

## 当前状态

- 本轮没有产生录像，没有改动历史录像或数据库。
- 没有提交或推送。
- MuMu 实例 3 未启动。通过项目 `MuMuClient.shutdown_player(2)` 关闭实例 2，随后确认 `is_process_started=False`、`is_android_started=False`。
- 工作区保留未提交修改；不要 reset / checkout 覆盖。

## 下一步

1. 有持续开播目标时，冷启动实测：确认直播页、头像点击打开资料卡、资料卡头像点击进入 `UserProfileActivity`，从新建 Activity Intent 读取 UID/room_id，并与目标比对。
2. 若 UIAutomator 仍高频 `could not get idle state`，应将它从直播页热路径移除，评估通过 `dumpsys` 与截图/客户端无障碍节点替代；不能以昵称代替稳定身份。
3. 实机闭环成功后，再测冷启动与预热启动的导航耗时；本轮不进入录像质量验收。