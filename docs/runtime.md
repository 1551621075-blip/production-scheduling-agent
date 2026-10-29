# 本地运行与安全边界

## 1. 三种 provider 必须显式择一

本应用只支持本地 provider，不从环境、`.env`、shell/profile、浏览器 profile 或其他工程目录发现配置，不加载云服务凭据。`serve` 没有默认 provider；失败不会改用云端，也不会自动切换成 TEST。shell 可以展开操作者显式设置的路径变量，但应用不把这些变量当作隐式配置来源。

| CLI 选择 | 实际 provider / 指纹前缀 | 能说明什么、不能说明什么 |
| --- | --- | --- |
| `--test-mode` | `SyntheticProvider` / `synthetic-test-` | 确定性 TEST 工具选择，不是 LLM；工具仍调用实际求解、独验与模拟代码，不能算 Qwen 验收 |
| `--model-url` | `LocalProvider` / `local-` | 请求操作者已启动的数字回环 HTTP 服务；不证明其模型字节身份，不拥有该进程生命周期 |
| `--runtime-config` | `ManagedLocalProvider` / `managed-local-cpu-` | 显式配置、文件身份核验和 CPU 子进程作用域；构造/health 成功不表示推理成功 |

下列命令只作操作示例，不是本次执行记录；先按 [使用说明](usage.md) 安装并初始化。变量应指向操作者审阅的仓库外目录/绝对配置路径，不应写入版本库。

```bash
# 三选一，不要同时启动。
production-scheduling serve "$SCHEDULING_DATA" --test-mode
production-scheduling serve "$SCHEDULING_DATA" --model-url http://127.0.0.1:18080
production-scheduling serve "$SCHEDULING_DATA" --runtime-config "$SCHEDULING_RUNTIME_CONFIG"
```

业务范围固定为原创合成数据，即使选择真实模型也返回 `synthetic_only: true`。该标记与 TEST provider 不是一回事。服务默认 `http://127.0.0.1:8006/`，`--port` 可更改端口，但 CLI 不允许配置公网 host。`GET /health` 不调用推理；启动输出的 `inference_checked: false` 不能解释为模型加载或建议闭环成功。

## 2. 外部回环 HTTP 模型协议

`--model-url` 接受无凭据、无 query/fragment、无额外路径的数字回环 **HTTP 根 URL**，包括 IPv4 或方括号 IPv6；拒绝 DNS 名称（包括 `localhost`）、远端地址、HTTPS 和间接 URL。默认模型别名是 `qwen3-4b-instruct-2507`，调用 `POST /v1/chat/completions`。

- HTTPX 明确 `trust_env=False`、`follow_redirects=False`，不走环境代理、不跟随重定向；不自动重试或追加修复调用。
- 温度 0，`max_tokens=768`，`cache_prompt=true`，严格 JSON schema。每轮恰好返回一个 `{tool, arguments}`，必须符合当前后端发放的工具、字段、候选 hash 与 evidence IDs。
- 响应须是一个完整 `stop` choice；额外 function/tool call、refusal、截断、重复字段或无效 usage 均不作为成功动作。
- 输入只传白名单上下文与有界证据摘要，不把用户 token、接收密钥或 runtime 文件路径发给模型。HTTP user-message JSON 先放稳定 task/source 身份及按依赖顺序排列的全部证据，再放变化的候选、缺失路径和工具菜单，以改善前缀缓存复用机会；不保证缓存命中，也不擅自扣减服务返回的实际 token 用量。
- 仅对指定位置的重复 `source_binding`、快照和 Plan 摘要进行精确共享：原始冻结对象及其白名单摘要必须分别完全相等，且引用确实更短，才用 `{"summary_ref":"#/..."}` 指向本条 JSON 中更早的白名单字面对象。不按 hash 或相同数量推定相等，不做任意递归去重；目标不是引用，不形成链、环或外部 URL。展开后恢复全部原摘要事实，每份证据的 `evidence_id/tool/hash`、候选 `plan_hash` 和完整送审 ID 集合不省略。引用不是权限证明，动作校验仍使用完整原始冻结上下文。
- 内容哈希仍使用原 canonical JSON，与传输排序和摘要共享分开。外部服务的模型别名不是文件哈希，缓存和进程是否与其他调用共享由外部操作者负责。
- 外部模式 `session()` 不管理资源，`close()` 不停止别人启动的服务。需要受控文件身份和进程作用域时应显式选择托管模式。

LangGraph 编译/调用前，后端以 `var_child_runnable_config.set({"callbacks": []})` 隔离当前上下文继承的 RunnableConfig，再在 `tracing_context(enabled=False)` 内运行图，并显式向 invoke 传入 `callbacks: []`；`finally` 使用保存的 token 恢复原 contextvar。仅在 invoke 传空列表不足以阻止继承 callbacks 合并，因此这几步不能省略为“传空 callbacks 即关闭”。该隔离不修改进程级 tracing 设置。

应用不配置 LangSmith 云端 tracing、远端 checkpoint 或 OpenTelemetry exporter。依赖闭包含相关 SDK 不代表启用其云服务；上下文隔离也不是网络沙箱，不能据此宣称任意宿主插件、手动注入的埋点或恶意依赖已被隔离。

## 3. 继承的 CPU 运行时目标与配置形状

CPU 目标参考沿用 R5 原创文档所记载的 **Qwen3-4B-Instruct-2507 GGUF Q4_K_M + llama.cpp b6500**。这只是公开组件身份参考，不继承 R5 的任何成功调用、测试或安装结论。R6 自身安装包不导入 R5 Python 包，不需要 R5 源码路径，也不硬编码任何人的绝对目录。操作者可在审阅后显式提供已有外部配置；应用不会搜索或自动读取 R5 runtime。

模型、binary、动态库和真实配置均位于仓库外，不随本项目源码或 wheel 分发。原始模型、量化分发物与引擎的许可边界见 [依赖说明](dependencies.md) 和 [第三方声明](../THIRD_PARTY_NOTICES.md)。本说明没有读取实际模型文件或私有配置，也不确认某个现场文件与该目标一致。

下面是**不可直接运行的占位结构**；路径、hash 和字节数必须由操作者替换为审阅后的真实值。`bytes` 的占位字符串必须换成正整数，不可保留为字符串。

```json
{
  "binary": {"path": "<ABSOLUTE_BINARY_PATH>", "sha256": "<BINARY_SHA256>"},
  "libraries": [
    {"path": "<ABSOLUTE_LIBRARY_PATH_BESIDE_BINARY>", "sha256": "<LIBRARY_SHA256>"}
  ],
  "models": {
    "generation": {
      "path": "<ABSOLUTE_GENERATION_GGUF_PATH>",
      "sha256": "<GENERATION_SHA256>",
      "bytes": "<POSITIVE_INTEGER_BYTE_COUNT>"
    }
  },
  "lock_path": "<ABSOLUTE_LOCK_PATH_WITH_EXISTING_PARENT>",
  "port": 18080
}
```

配置上限 64,000 字节，严格字段/类型，拒绝重复字段、额外字段及非有限值；不支持递归配置、任意 shell 指令或下载配置。SHA-256 必须是 64 位小写十六进制；`libraries` 为 1–30 项，均须与 binary 同目录；`port` 默认为 18080，严格整数 1024–65535。

`models` 首选仅 `generation`，也兼容恰好 `generation`、`embedding`、`rerank` 的旧三角色描述。兼容字段不代表启用 BGE：旧 embedding/rerank 只做结构和路径校验，**不打开、不哈希、不加载、不计入运行指纹**。

所有路径须绝对、彼此不同、无 traversal/控制字符；实际打开的配置、binary、library、generation 与 lock 路径链拒绝软链接。构造 provider 读取显式配置与安装代码身份，但不启动引擎，也不提前打开/哈希 generation。首次合法 `decide()` 的 transport preflight 之后才核验文件并惰性启动。

### 文件身份与原生启动

- binary 必须是可执行普通文件；实际使用的 binary、libraries、generation 核验 SHA-256，generation 另核对真实字节数。缓存绑定 device/inode/size/mtime/ctime，变化后重新验证；源码 revision 不等于本机构建的二进制 hash。
- 新建 lock 使用 0600，父目录须已经存在，lock 须为单硬链接普通文件。线程作用域锁、整次 decision 锁和非阻塞 `fcntl.flock` 排除重叠运行；lock fd 传给子进程。占用端口时拒绝，不借用未知服务。
- 托管模式固定 CPU 参数 `--n-gpu-layers 0 --device none --no-op-offload --flash-attn off`，并非 GPU 失败后的静默 fallback。并行度 1，CPU/批处理线程各 4，HTTP 线程 2，context 8192，batch 512，ubatch 128，seed 42，温度 0。
- 引擎只绑定 `127.0.0.1`，关闭 Web UI、slots 和引擎日志；使用参数数组而非 shell。子进程最小环境、临时 HOME/TMPDIR，stdin/stdout/stderr 接空设备，不留下原始回复日志。

需要原生 POSIX 文件/进程能力及匹配宿主 CPU/OS 的引擎与动态库。`fcntl`、`O_NOFOLLOW`、权限位和 `pass_fds` 都是实际约束；**当前不声明 Windows 原生支持**。锁文件能解析 Windows 分支或应用 wheel 是 `py3-none-any`，均不能推导托管运行时跨 OS 可用。

## 4. 作用域、取消、用量与时间预算

`provider.session()` 包围一次 Agent `run`，不是整个持久化任务生命周期，更不跨人工补缺暂停。首个 decision 惰性启动，当前作用域可复用进程与 prompt/context cache；退出、失败或取消后的清理完成后，下一次 run 才可建立新作用域。直接一次 `decide()` 会创建自己的短作用域。失败作用域不能继续复用为成功。

`close()` 永久关闭 provider 实例，由应用退出负责；不要在每次人工暂停后关闭仍需使用的 adapter。在途 decision 时拒绝 close，不用异步杀进程冒充已取消。清理先 terminate、等待，必要时 kill、再等待，各阶段名义上限 10 秒。不能确认回收时设置 cleanup-unknown 并拒绝新推理；业务 `/reconcile` **不能**解除这个进程安全状态。操作者应独立核实拥有的进程和锁，再显式重启应用；不能盲删锁文件或只重试请求。

| 项目 | 当前上限/语义 |
| --- | --- |
| 单任务模型调用 | 16 次，跨暂停/恢复累计 |
| 单任务工具调用 | 14 次 |
| 单任务累计 tokens | 40,000；不是仅输出 tokens |
| 单任务协作耗时 | 600 秒，在步骤与返回边界核验 |
| worker 租约 | 1200 秒，用于并发与迟到回复设栅栏，不是执行器整厂计划租约 |
| 托管启动 | 名义上限 90 秒 |
| 模型 HTTP 请求 | 180 秒，另有连接/响应读取检查 |
| 单次输出上限 | 768 tokens，不是本次输入输出总量 |
| transport 大小 | 上下文 48,000、请求 64,000、动作 8,000、响应 128,000 字节 |

这是**协作式预算，不是硬实时取消或墙钟硬上限**。在途模型调用、加载、阻塞 I/O 与清理可能使实际耗时超出 600 秒；返回时的可靠 usage 也可能使累计 tokens 超过门槛，后端不得截断实际账单。CP-SAT 的 5 秒限时同样不是覆盖所有模型构建和退出的硬杀 watchdog。

调用先持久化 started 日志，模型等待不持事务；已知 usage 先记账，再验证动作。无效工具参数也可能产生实际消耗；cache 命中不能擅自扣减服务返回的 total_tokens。仅确认未发送的失败可记 0；可能已发送但无可信 usage 时保留 unknown，不能当作免费或自动重试。

任务取消阻止后续工具/送审，但不保证瞬间停止在途 HTTP。取消、撤权后可靠的迟到 usage 仍单调记账。unknown 须由本 owner 范围内 admin 在无活跃 worker 时对账，明确 `no_result=true`；tokens 不得低于已知用量，`null` 表示未知并保守耗尽剩余预算，不代表 0。对账不恢复丢失的模型结果、不重发原调用、不授权执行设备。

## 5. 本地服务、浏览器与接收秘密

首页、health 和两个静态资源无需身份；业务 `/api` 使用 Bearer，身份/工厂权限由后端核验。HTTP 防护包括数字回环 Host、拒绝 query、同源写请求、严格有限 JSON、1 MiB 请求体上限、no-store/CSP、禁用代理头信任和访问日志。该边界不是反向代理部署教程，不应把监听改为公网或添加外部资源。

浏览器 token 仅保存在当前页内存，退出/离页清除，不写 localStorage、sessionStorage、Cookie、URL 或日志。登录不是换取生产权限；界面隐藏/禁用按钮不能替代后端鉴权。令牌和原始业务对象都不应录屏、贴到公开问题或导出到公开日志。

人工审阅只改审阅状态，`approved` 不会发布、接收或执行；每一步另行触发。多工序 partial 仅暂存，不占料、不执行，accepted 仍未加工。交付 unknown 只做独立 readback，未读到回执也不自动重传；旧 partial 低于在途动作的 `minimum_sequence` 时不能结清 unknown。持久化整厂 delivery intent 阻止其他 publication 绕过未决交付，权威取消/执行状态及同步已知拒绝的有限例外由后端判定。接收器执行已开始时撤销返回 `too_late`；遗留 running 不自动重跑。详细状态见 [架构说明](architecture.md) 和 [人工动作契约](actions-contract.md)。

HMAC 密钥在仓库外数据根的独立秘密文件，私有目录 0700、文件 0600，拒绝链接、宽权限或异常身份。已有接收器历史但 key 丢失/改变时 fail closed，不自动重建。密钥不进入 API、公共 artifacts 或模型上下文。发布和接收在同一可信进程/Store，HMAC 只验证持有秘密的一侧所认证的消息，不提供秘密持有方之间的不可否认性，更不是物理 MES 或分布式 exactly-once。

## 6. 验收不能互相替代

TEST 编排、真实 CP-SAT、独验/oracle、真实 Qwen 推理、Chrome 交互、源码外安装和合成执行必须分别记录。R5 的推理数字不迁移为 R6 成绩；health 或 provider 构造成功不算模型验收；模拟 completed 不算真实设备加工。最终运行结果以负责人补齐的 [验收记录](validation.md) 为准，未运行或未通过项不能记成功。
