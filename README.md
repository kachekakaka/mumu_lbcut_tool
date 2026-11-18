# mumu_lbcut_tool

本项目包含 MuMu 单路录播 Worker、SQLite 持久化调度器、控制台 HTTP 后端和网页控制台。调度 CLI 与网页通过 HTTP 查询和控制；调度层负责开下播查询、去重排队和实例分配，Worker 负责本地准入、采集、有限恢复与切片封口。

切片页支持在录制机上[手动安排外部 Core 上传](docs/最小调度运行说明.md#在切片页手动安排上传)。网页控制台已完成台式机真实录制联调。操作入口与网页构建见 [调度运行说明](docs/最小调度运行说明.md)，后端接口见 [控制台接口契约](docs/控制台接口契约.md)，剩余工作与验收见 [待办](docs/待办.md)，页面行为见 [控制台页面与交互](docs/设计说明.md#控制台页面与交互)。

运行环境：Windows、Python 3.11、MuMu、scrcpy，以及固定的 Gyan FFmpeg/ffprobe 9.0.2 full build。媒体工具部署见 [运行说明](docs/最小调度运行说明.md#固定媒体工具部署)。Python 运行依赖见 `requirements.txt`，测试依赖见 `requirements-test.txt` 和 [测试入口](tests/README.md)。

## 文档入口

| 要了解的内容 | 入口 |
| --- | --- |
| 协作、授权、实施与验证边界 | [AGENTS.md](AGENTS.md) |
| 文档结构、各类文档职责、方案创建与归档 | [docs/README.md](docs/README.md) |
| 当前架构、职责及代码入口 | [设计说明](docs/设计说明.md) |
| Git 忽略规则 | [.gitignore](.gitignore) |
| 维护脚本及设备操作边界 | [scripts/README.md](scripts/README.md) |

## 项目目录与文件存放

路径相对仓库根目录解析，不依赖本机固定盘符。标有“按需”的部分为约定位置，尚未创建；不预建空文档或占位目录。

```text
项目父目录/
├─ mumu_lbcut_tool/                  Git 仓库
│  ├─ AGENTS.md                     长期协作规则
│  ├─ README.md                     项目入口与文件存放约定
│  ├─ .gitignore                    本地文件忽略规则
│  ├─ docs/
│  │  └─ README.md                  文档结构与生命周期
│  ├─ web/                         网页控制台源码；web/dist/ 构建结果与 node_modules 不进 Git
│  ├─ tests/                       测试代码、固定夹具与测试说明（按需）
│  └─ archive/docs/                归档方案与失效决定（按需）
└─ mumu_lbcut_tool_dev/             仓外工作目录
   ├─ mumu_lbcut_tool_tmp/          临时文件与测试产物
   └─ mumu_lbcut_tool_dists/        打包产物
```

| 位置 | 存放内容与边界 |
| --- | --- |
| 本仓库 | `src/worker/`、`src/orchestrator/`、`web/`（网页控制台源码及其单元测试）、源码依赖、长期文档与可重复测试；不保存运行数据库、真实录像或网页构建结果 |
| `scripts/`、`archive/scripts/` | 前者保存维护入口；后者保存已退役脚本的源码文本和用途说明，不作为可运行工具；既有现场产物的整理须另行确定 |
| `../mumu_lbcut_tool_dev/mumu_lbcut_tool_tmp/` | 临时脚本、草稿、探针、构建中间文件、测试输入副本、输出、日志、截图、报告及验包解压副本 |
| `../mumu_lbcut_tool_dev/mumu_lbcut_tool_dists/` | 打包生成的程序目录、安装包、便携包及发布压缩包；测试包同样放在这里，并在名称中注明测试用途 |
| `../mumu_lbcut_tool_dev/runtime-console/` | v2 控制台默认长期调度数据库与事件日志；仅 `run --data-dir` 可指定其他独立目录 |
| `../mumu_lbcut_tool_dev/runtime/` | 保留旧运行数据及共享实例锁；新服务不读取旧库、不迁移或覆盖已有数据库 |
| 用户指定的 `--output-dir` | 正式录像目录；按调度任务与 Worker run_id 分层，禁止覆盖已有录像 |
| `../mumu_lbcut_tool_dev/tools/` | scrcpy 与固定媒体工具；ffmpeg/ffprobe 成套放入 `ffmpeg-9.0.2-full_build/bin/`，二进制不进 Git，具体部署见运行说明 |

临时与测试材料按任务或批次分子目录，例如 `mumu_lbcut_tool_tmp/20261020-任务名称/`；同日多批次追加序号。打包产物使用独立目录或文件名，不覆盖已有版本。打包目录存放产物，验包运行和解压副本放入临时目录；打包产物不混入私人配置或测试运行数据。

需要长期维护、可重复执行的测试代码及固定夹具放在仓库 `tests/` 中；一次性测试脚本、现场样本、测试输出和大体积材料放在外部临时目录。需长期保留的验证摘要进入文档，具体规则见 [文档说明](docs/README.md)。

目录用途不代表可以自动清理；数据保护与操作授权遵循 [AGENTS.md](AGENTS.md)。

## Git 忽略范围

[.gitignore](.gitignore) 覆盖系统杂项、编辑器临时文件、常见本地环境文件，以及误放在仓库根目录的私人数据、临时文件、日志、测试输出和打包目录。这些忽略项不代表允许改用仓内目录存放产物，仍应使用上表约定的位置。

两个外部工作子目录本就在仓库之外，不需要用越过仓库根目录的忽略规则处理。忽略规则只是防误加入措施，不是凭证识别器，不替代提交前的内容检查，也不会自动取消已跟踪文件。

按实际工具维护依赖和缓存规则。不要笼统忽略所有图片、压缩包、数据库扩展名，或任意层级的 `data`、`media` 等目录，以免遮蔽源码、文档素材和经审查的测试夹具。环境示例文件仅在确认不含真实凭证后纳入版本管理。新增规则时同时核对应该忽略的产物与应该保留的源码。
