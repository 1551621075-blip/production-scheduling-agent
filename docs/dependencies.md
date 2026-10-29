# 依赖、许可与发行边界

## 1. 工程身份和证据口径

发行名 `production-scheduling-agent`，版本 `0.1.0`，Python 包 `scheduling_agent`，console entry 为 `production-scheduling = scheduling_agent.cli:main`。业务输入、排产服务及合成事件引擎为本项目原创参考实现；不含生产接入或现场收益声明。

本文核对 [`pyproject.toml`](../pyproject.toml)、[`uv.lock`](../uv.lock) 和本机对应已安装发行物的许可元数据，不联网查询“最新版本”。第三方声明**不会给原创项目整体添加 MIT、Apache 或其他 blanket license**，也不等于完整 SBOM、逐文件许可审计、再分发授权或供应链安全证明。具体义务见实际发行物的 LICENSE/NOTICE，摘要见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

## 2. Python、锁文件和原生平台

- Python 声明范围 `>=3.12,<3.13`，锁解析范围 `==3.12.*`。构建后端声明 `hatchling>=1.27`，Hatchling **不在当前应用/dev 锁条目内**，不能宣称构建后端已精确锁定。
- 当前锁有 66 个 package 条目，包含工程自身、开发工具和平台条件分支；这不是某台主机实际安装的包数，也不是源码外安装成功证据。
- 锁中的 registry 为公开阿里云 PyPI 镜像；各候选 sdist/wheel 的 URL 与 SHA-256 留在锁文件。哈希约束特定字节，不验证许可权利、镜像可信性或所有 OS 上的构建可复现性。
- 应用 wheel 可为 `py3-none-any`，但依赖闭包并非全为纯 Python。OR-Tools、NumPy、Pandas、Protobuf、pydantic-core、orjson、ormsgpack、xxhash、uuid-utils 等包含或可能使用平台原生代码；须有对应 Python/OS/架构的发行物。
- 本项目没有把全部依赖源码 vendoring 到仓库。wheel/sdist 中不打包模型、llama.cpp、动态库、私有 runtime 配置、凭据或运行数据库；这些资源应在仓库外独立管理。最终候选内容审查仍需负责人执行，不能由本段代替。
- 托管模型和本地存储权限依赖 POSIX 能力，包括 `fcntl`、`O_NOFOLLOW`、`pass_fds` 与 Unix 权限位。**当前不声明 Windows 原生支持**；lock 含 Windows/WebAssembly 分支并不代表本应用在这些平台可运行或已验收。
- 固定依赖版本、单 worker 与 seed 不构成跨 OS、编译器、CPU 或模型引擎的 bit-reproducible 保证。源码外 fresh venv 仍共享当前 OS/基础 Python/内核，不是新宿主验收。

## 3. 直接依赖、开发工具及关键传递依赖

下表版本来自当前锁；许可为上游声明摘要，而非对下游全部捆绑文件统一授权。

| 依赖 | 锁定版本 | 本项目用途 | 许可声明 |
| --- | --- | --- | --- |
| FastAPI | 0.162.0 | 本地 API | MIT |
| HTTPX | 0.28.1 | 数字回环模型 HTTP 与测试客户端 | BSD-3-Clause |
| LangGraph | 1.2.5 | 实际单 Agent 图编排；直接声明固定 `==1.2.5` | MIT |
| LangSmith | 0.14.14 | 显式禁用 tracing 的上下文及图依赖接口 | MIT |
| OR-Tools | **9.15.6755** | **实际 CP-SAT 排产求解** | **Apache-2.0** |
| Pydantic | 2.14.1 | 严格 schema、范围和引用约束 | MIT |
| Typer | 0.27.3 | CLI | MIT |
| Uvicorn | 0.54.0 | 本地 ASGI | BSD-3-Clause |
| pytest | 9.2.0 | 开发测试 | MIT |
| Ruff | 0.17.3 | 静态检查 | MIT |
| Hatchling | 未锁定；声明 `>=1.27` | 构建后端 | MIT |
| langchain-core | 1.7.1 | 图依赖运行接口 | MIT |
| langchain-protocol | 0.0.20 | 协议类型 | MIT |
| langgraph-checkpoint | 4.3.1 | 图传递依赖 | MIT |
| langgraph-prebuilt | 1.1.1 | 图传递依赖 | MIT |
| langgraph-sdk | 0.4.10 | 图传递依赖，不代表启用远程托管 | MIT |
| absl-py | 2.5.1 | OR-Tools 传递依赖 | Apache-2.0 |
| immutabledict | 4.3.1 | OR-Tools 传递依赖 | MIT |
| **NumPy** | **2.5.4** | OR-Tools/Pandas 数值依赖 | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| **Pandas** | **3.1.0** | OR-Tools 传递依赖；不表示应用依赖生产数据表 | BSD-3-Clause |
| **Protobuf** | **6.33.6** | OR-Tools 传递依赖 | BSD-3-Clause |
| python-dateutil | 3.0.0 | Pandas 日期依赖 | Apache-2.0 / BSD-3-Clause，按文件许可说明 |
| tzdata | 2026.6 | 条件分支/日期依赖 | Apache-2.0，另核对随包时区数据声明 |
| pydantic-core | 2.50.1 | 原生校验核心 | MIT |
| orjson | 3.13.0 | 图/SDK JSON 传递依赖 | `MPL-2.0 AND (Apache-2.0 OR MIT)` |
| ormsgpack | 1.13.0 | 图 checkpoint 传递依赖 | `Apache-2.0 OR MIT` |
| opentelemetry-api | 1.45.1 | FastAPI 传递依赖 | Apache-2.0 |
| httpx2 / httpcore2 | 2.16.0 / 2.16.0 | LangSmith 传递依赖 | BSD-3-Clause |
| certifi | 2026.10.23 | HTTP 依赖证书集 | MPL-2.0 |
| typing-extensions | 4.16.0 | 类型兼容接口 | PSF-2.0 |

尤其不能把 NumPy、orjson 或全部传递依赖简写为 MIT/BSD 单一许可。锁中还有 requests、requests-toolbelt、truststore、websockets、zstandard、UUID/hash、CLI 渲染和测试依赖，具体版本以锁为准；[第三方声明](../THIRD_PARTY_NOTICES.md) 记录其许可分组与未核验的平台项。

OR-Tools 的直接声明为 `>=9.12,<10`，当前真正锁到 **9.15.6755**，不得写成实际运行的是 9.12。负责人已确认本地 9.15 CP-SAT smoke；这不等于整个项目、微型 oracle、真实 Qwen 或浏览器验收通过。最终全量结果待 [验收记录](validation.md) 记录。

### 安装 SDK 不等于使用云服务

实际 Agent 图先以 `var_child_runnable_config` set/reset 隔离继承的 callback 配置，同时使用 `tracing_context(enabled=False)` 和显式 invoke `callbacks: []`；仅向 invoke 传空 callbacks 不能阻止继承配置合并。应用不配置远端 client/checkpoint/exporter。模型传输由本项目 HTTPX adapter 处理，只允许显式数字回环 HTTP，不读取 dotenv/profile、不信任代理环境、不跟随重定向、不自动重试、不云端 fallback，也不失败转 TEST。详见 [运行说明](runtime.md)。这不是对任意修改后的代码或宿主自动埋点的绝对无网络保证。

## 4. 仓库外 CPU 模型与引擎

以下目标身份参考来自 R5 原创依赖/运行说明，不来自私有实现复制，不表示本轮读取或重新验证了权重、配置或二进制。R6 不依赖 R5 源码安装路径。

| 外部组件 | 公开上游 | 许可和身份边界 |
| --- | --- | --- |
| llama.cpp | <https://github.com/ggml-org/llama.cpp> | 参考 `b6500` CPU 构建；上游 MIT。各宿主 binary/library 的 hash 必须单独核验，不能用源码 revision 代替 |
| Qwen3-4B-Instruct-2507 原模型 | <https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507> | 原模型标示 Apache-2.0，须保留对应模型卡和许可义务 |
| Qwen GGUF 量化分发物 | <https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF> | 目标 Q4_K_M；转换/量化分发者的条款、来源和具体文件需另行核对，不能只引用原模型 Apache-2.0 |

托管 provider 核验操作者显式声明的 generation 文件字节数/SHA-256 及引擎/库 hash；不自动下载、不判定所选文件许可，也不自动证明其与上表参考 revision 一致。这里只使用 generation，不因兼容旧三角色配置而读取、运行 embedding/rerank 模型。模型、引擎许可证不构成本项目整体许可证。

## 5. 安装与源码外探针

源码环境准备命令见 [使用说明](usage.md)。下面是负责人冻结当前源码并独立准备、审阅 wheel 和完整本机依赖 wheelhouse 后的**待执行示例**，不是本次安装结果。所有路径变量由操作者明确设置，不能指向私有生产资料、模型或含秘密的目录。

```bash
.venv/bin/python scripts/check_install.py \
  --wheel "$SCHEDULING_WHEEL" \
  --wheelhouse "$SCHEDULING_WHEELHOUSE" \
  --case normal
```

`SCHEDULING_WHEEL` 应指向当前源码构建的应用 wheel；`SCHEDULING_WHEELHOUSE` 为源码目录外已审阅的平坦依赖 wheel 目录。脚本不会联网补依赖、build 或上传。离线安装器使用 `--no-index --only-binary=:all:`，**不会自动按 uv.lock 再次约束解析版本**；维护锁一致性需负责人只提供相应版本并核对锁内 SHA-256。

安装探针在源码外新 venv 中检查 installed distribution、模块、实际 console script、合成 fixture、TestClient health 和页面资源；不读取 runtime 配置、不请求模型推理。源码单元测试、mocked install、health 或应用 wheel 构建成功均不能代替该真实探针，更不能代替新 OS、Qwen、浏览器或现场验收。

本文没有运行网络安装、真实 endpoint、模型或浏览器，也不报告最终通过数。冻结后的包/源码/逐文件内容核验及实际安装结果由负责人在 [验收记录](validation.md) 中单独记录。
