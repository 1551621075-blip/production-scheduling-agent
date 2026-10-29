# 第三方组件声明

## 适用范围

本文件仅记录 `production-scheduling-agent` 使用或可由操作者另行提供的**第三方组件**及许可边界，**不是对本原创项目整体授予 MIT、Apache-2.0 或其他开源许可**。项目未在 `pyproject.toml` 中指定整体 license；不得把依赖的授权自动扩展到原创代码、示例数据、文档、商标或外部模型。

本项目公开候选只应包含原创源码、合成生成器、测试、静态页面、文档与锁文件，不分发引擎二进制、动态库、模型权重、私有配置、密钥、凭据或运行数据库。依赖在安装阶段从独立发行物提供，**并非全部 vendored** 到本仓库。最终候选/包内容审查仍需独立执行。

版本依据本项目 [`uv.lock`](uv.lock)；许可摘要依据本机对应发行物的元数据/许可文件及公开原始参考资料，未联网逐文件审计。摘要不替代上游完整 LICENSE、NOTICE、版权声明、专利条款或发行物附带的其他许可。如果另行再分发依赖、原生 wheel、引擎或模型，发行者须按实际选定版本保留适用声明并核对修改/再分发义务。本文件不提供法律意见、上游背书或安全保证。

## Python 运行依赖与开发工具

下表按许可声明分组，避免把传递依赖统一标成 MIT。版本是锁版本，不是上游最新版本。`OR`/`AND` 表达式保持其选择/同时满足的差异；组内组件仍分别适用各自版权与逐文件条款。

| 许可声明摘要 | 锁定组件与版本 |
| --- | --- |
| MIT | annotated-doc 0.0.5；annotated-types 0.8.0；anyio 4.15.1；charset-normalizer 3.5.2；FastAPI 0.162.0；h11 0.16.0；immutabledict 4.3.1；iniconfig 2.3.1 |
| MIT | langchain-core 1.7.1；langchain-protocol 0.0.20；LangGraph 1.2.5；langgraph-checkpoint 4.3.1；langgraph-prebuilt 1.1.1；langgraph-sdk 0.4.10；LangSmith 0.14.14 |
| MIT | markdown-it-py 4.2.0；mdurl 0.1.2；pluggy 1.7.0；Pydantic 2.14.1；pydantic-core 2.50.1；pytest 9.2.0；PyYAML 6.0.3；Rich 15.0.0；Ruff 0.17.3；six 1.17.0；truststore 0.10.5；Typer 0.27.3；typing-inspection 0.4.4；urllib3 2.8.0 |
| Apache-2.0 | **OR-Tools 9.15.6755**；absl-py 2.5.1；distro 1.9.0；opentelemetry-api 1.45.1；requests 2.34.2；requests-toolbelt 1.0.0；tenacity 9.2.1；tzdata 2026.6 |
| BSD-3-Clause | Click 8.5.0；httpcore 1.0.9；httpcore2 2.16.0；HTTPX 0.28.1；httpx2 2.16.0；idna 3.20；jsonpatch 1.38；Pandas 3.1.0；Protobuf 6.33.6；Starlette 1.10.0；uuid-utils 0.19.0；Uvicorn 0.54.0；websockets 17.3；zstandard 0.25.0 |
| Modified BSD（须核对随包全文） | jsonpointer 3.6 |
| BSD-2-Clause | Pygments 2.21.0；xxhash 4.0.2 |
| MPL-2.0 | certifi 2026.10.23 |
| PSF-2.0 | typing-extensions 4.16.0 |
| ISC | shellingham 1.5.4 |
| `Apache-2.0 OR BSD-2-Clause` | packaging 26.3 |
| `MIT OR Apache-2.0` | sniffio 1.3.1 |
| `Apache-2.0 OR MIT` | ormsgpack 1.13.0 |
| `MPL-2.0 AND (Apache-2.0 OR MIT)` | orjson 3.13.0 |
| `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | **NumPy 2.5.4**；不能只标 BSD |
| Apache-2.0 / BSD-3-Clause，按随包说明 | python-dateutil 3.0.0；全文区分贡献许可，BSD 许可也适用于其声明覆盖的代码 |
| 平台分支，未在本机核验对应发行物许可 | colorama 0.4.6（通常上游 BSD-3-Clause，分发前仍须查所选包）；httpx2-jsfetch 1.0（Emscripten 分支，不在此推定许可） |

构建后端 Hatchling 声明 `>=1.27`，上游 MIT，但未被当前应用/dev 锁精确固定；不能据此宣称构建闭包已完全锁定。Python、uv 工具链以及原生 wheel 随带的系统/数学库也有各自条款，不因应用 wheel 为纯 Python 而消失。tzdata 的包代码许可摘要不替代所含时区数据声明；原生 OR-Tools/NumPy/Pandas 等发行物的附带 NOTICE/子组件同样应按实际包核对。

主要公开上游参考：

- [OR-Tools](https://github.com/google/or-tools)、[Pandas](https://github.com/pandas-dev/pandas)、[NumPy](https://github.com/numpy/numpy)、[Protobuf](https://github.com/protocolbuffers/protobuf)
- [FastAPI](https://github.com/fastapi/fastapi)、[Pydantic](https://github.com/pydantic/pydantic)、[HTTPX](https://github.com/encode/httpx)、[Uvicorn](https://github.com/encode/uvicorn)、[Typer](https://github.com/fastapi/typer)
- [LangGraph](https://github.com/langchain-ai/langgraph)、[LangChain](https://github.com/langchain-ai/langchain)、[LangSmith SDK](https://github.com/langchain-ai/langsmith-sdk)
- [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python)、[pytest](https://github.com/pytest-dev/pytest)、[Ruff](https://github.com/astral-sh/ruff)、[Hatch](https://github.com/pypa/hatch)

这些 URL 是文档引用，不会由应用页面自动加载，也不是本次联网检查结果。SDK 源码许可不代替其云端商业服务条款。本应用的 Agent 图显式关闭 LangSmith tracing，并隔离继承 callbacks，具体边界见 [运行说明](docs/runtime.md)；应用不配置云服务，依赖安装本身不代表远端服务被启用。

## 仓库外模型与推理引擎

| 组件 | 公开来源 | 许可边界 |
| --- | --- | --- |
| llama.cpp | <https://github.com/ggml-org/llama.cpp> | 上游 MIT；CPU 参考版本 b6500。操作者的实际可执行文件、动态库及其附带声明另核对 |
| Qwen3-4B-Instruct-2507 原模型 | <https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507> | 原模型标示 Apache-2.0；原模型身份与许可不代表任意改版或量化字节已获完整再分发授权 |
| Qwen3-4B-Instruct-2507 GGUF Q4_K_M | <https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF> | 转换/量化分发者的版本、来源、模型卡和分发条款须单独审阅；不能仅凭原模型 Apache-2.0 忽略这些条款 |

上述身份是来自 R5 原创公开参考说明的运行目标，不是本轮下载、加载、模型许可全文核验或成功推理声明。R6 不硬编码本机 runtime 路径、不依赖 R5 源树、不分发任何模型字节，也不加载旧配置中的 embedding/rerank 角色。具体操作和平台要求见 [运行说明](docs/runtime.md)。

## 限制与再分发检查

- 本文件不是“全部依赖许可证已审计”的结论，未核验的条件分支必须保留未知，不能猜作 MIT。
- 锁哈希不证明来源可信、许可完整或跨 OS 位级可复现；lock 能解析 Windows 不构成应用 Windows 支持。
- 本地 HMAC、独验和合成模拟不构成生产认证、法律电子签章、不可否认性或现场收益证据。
- 实际发布前须核对候选源码与 wheel/sdist 内容，保留适用第三方声明，排除模型、二进制和秘密。运行/安装结果由负责人另记 [验收记录](docs/validation.md)，本文不宣布最终验收通过。
