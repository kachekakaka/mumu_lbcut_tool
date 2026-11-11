# 第三方代码来源

## abogus.py

- 来源：[F2](https://github.com/Johnserf-Seed/f2) 0.0.1.7 的 `f2/utils/abogus.py`，PyPI 发行包原样复制，未做修改。
- 版权：Copyright (c) 2023 JohnserfSeed，Apache License 2.0，许可证全文见同目录 [LICENSE](LICENSE)。
- 用途：为直播间状态接口 `/webcast/room/web/enter/` 生成 `a_bogus` 签名参数，调用方见 [room_resolver.py](../room_resolver.py)。
- 依赖：`gmssl`（SM3），版本见仓库 `requirements.txt`。
- 上游说明该项目仅供学习研究；本项目当前仅内部使用，对外分发前需重新核对许可与上游声明。
