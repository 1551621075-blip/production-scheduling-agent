# 使用说明

## 1. 适用范围与安装

本项目只处理原创合成订单、资源、工艺与库存。排产目标、模拟 completed 和图表均不是现场生产事实或收益。模型只能选择受控工具，不能直接写排程、放宽约束、批准、发布或执行。数据语义见 [架构说明](architecture.md)，真实模型配置见 [运行说明](runtime.md)。

要求 Python 3.12、uv，以及满足本机原生依赖的 POSIX 环境；当前不声明 Windows 原生支持。以下是**由操作者按需执行的示例，不是本文的执行记录**。先进入本项目源码根目录，再准备锁定环境：

```bash
uv sync --locked
uv run --no-sync production-scheduling --help
uv run --no-sync production-scheduling fixture --case normal
```

`uv sync` 在无缓存时可能从锁中的公开依赖源下载包，不下载模型；本次文档编写未执行网络安装。离线场景需要事先准备完整匹配的依赖，不能假定初次同步也完全离线。

### 源码外安装后使用 CLI

如果已有独立构建、审阅的应用 wheel 和完整本机依赖 wheelhouse，可在源码目录之外使用下列命令。`SCHEDULING_VENV` 为新的仓库外 venv 路径；`SCHEDULING_WHEEL`、`SCHEDULING_WHEELHOUSE` 均为操作者显式提供的绝对路径。Python 3.12 必须已经可用；这些变量不含凭据。

```bash
uv venv --python 3.12 --no-python-downloads "$SCHEDULING_VENV"
uv pip install --python "$SCHEDULING_VENV/bin/python" \
  --no-index --only-binary :all: \
  --find-links "$SCHEDULING_WHEELHOUSE" "$SCHEDULING_WHEEL"
"$SCHEDULING_VENV/bin/production-scheduling" --help
"$SCHEDULING_VENV/bin/production-scheduling" fixture --case normal
```

这条安装命令不会自动读取源码中的 uv.lock 约束版本；wheelhouse 必须由负责人按锁版本和 SHA-256 核对。独立源码外安装探针与局限见 [依赖说明](dependencies.md)。上面的操作指南不代表实际包验收已经完成；最终证据以 [验收记录](validation.md) 为准。

## 2. 初始化与显式启动

将 `SCHEDULING_DATA` 设为**仓库外、父目录已存在、当前尚不存在的新目录**。它只是 shell 展开的路径占位变量；应用不从环境自动发现数据目录、provider、token 或配置。源码环境可在下列命令前使用 `uv run --no-sync`；源码外安装则使用对应 venv 的 console script。

```bash
# init 只显示一次随机管理员 token；禁止录屏、tee 或输出到公共日志。
production-scheduling init "$SCHEDULING_DATA"

# 三种 provider 恰好选择一种，不要同时运行。
production-scheduling serve "$SCHEDULING_DATA" --test-mode
production-scheduling serve "$SCHEDULING_DATA" --model-url http://127.0.0.1:18080
production-scheduling serve "$SCHEDULING_DATA" --runtime-config "$SCHEDULING_RUNTIME_CONFIG"
```

- `init DATA` 建立私有数据目录、本地管理员和 `normal`、`infeasible`、`missing` 三个合成快照，只存 token hash 并将原 token 显示一次。不推理、不求解、不生成建议、不批准、不发布、不执行；拒绝已有目录，不能当作重置命令。
- `fixture --case normal|infeasible|missing` 只输出一个严格 Snapshot JSON，默认为 normal，不持久化。时间来自当前时钟。当前没有训练、自动发布或模拟专用 CLI 子命令。
- `serve DATA` 要求已初始化；`--runtime-config` 必须是操作者审阅的仓库外绝对路径；`--model-url` 只接受数字回环 HTTP 根 URL。没有 provider 默认值或云端/TEST fallback。
- 默认服务为 **`http://127.0.0.1:8006/`**，可用 `--port` 改端口，CLI 不提供公网 host 选项。`GET /health` 只返回健康/指纹和 `synthetic_only`，不加载或调用模型。
- 数据目录包含私有数据库和接收密钥，不应放进源树、备份到公共位置或上传。已有接收历史但密钥缺失/不安全时 fail closed，不要重新 init 或删除数据库来绕过。

TEST provider 不是语言模型，但调用的排产工具使用实际 CP-SAT、独验和模拟。真实本地模型须另行显式选择，不能把 TEST 流程或 health 成功叫作 Qwen 成功。

## 3. 浏览器工作流

打开同源本地首页，将本实例 token 输入密码框。token **仅存在当前页内存**，不写 localStorage、sessionStorage、Cookie、URL 或日志；退出/离页清除。登录身份、owner、工厂范围和角色由后端决定，不能从页面修改角色获得权限。

1. **检查快照。** 确认观察时间、revision、整数数量、工艺、物料、主辅资源和冻结承诺。来源超过 24 小时或在未来会被拒绝；刷新页面不能延长时效。
2. **显式选择情景。** `baseline` 使用原始输入；`insert_order` 需完整新 Lot/Order；`machine_outage` 指定机器及时间区间；`supply_delay` 指定已有 supply 和更晚时间。可从服务端读取示例 JSON，但模型不能更改你的选择。创建任务不自动运行。
3. **单独运行任务。** 查看九工具、七项分析证据、调用/tokens/工具预算和 solver 真实状态。`UNKNOWN` 不等于无解；有合法 baseline 不保证 optimized 更好。取消任务仍是独立操作，不抹除在途或未知用量。
4. **有缺失才补问。** 仅补 `initial_stock`、`due_min` 的实际缺失路径，一次填齐整数值与依据。补缺生成新 revision，不覆盖已有值、不刷新 observed_at_ms；保存补缺后须另行运行，不自动调用模型。
5. **审阅证据。** 查看同源候选目标、独验、换型/加工甘特图、辅资源占用、逐事件库存和合成模拟。模型说明按非可信纯文本展示，不能覆盖候选事实。送审前模拟不等于发布或执行。
6. **人工审阅。** 只有当前 owner 的 reviewer/admin 可批准/拒绝。请求绑定当前 recommendation `content_hash`；**approved 不会发布、不接收、不执行**。此默认机制不强制另一自然人双人复核。
7. **另行请求发布。** 批准后重新读取最新 content_hash，明确确认创建唯一 HMAC 认证的发布请求；此步仍未递送。
8. **首次递送与完成接收。** 多工序首次本地递送只 stage 第一项，显示 partial，不占料、不执行。确认 partial 后再完成剩余接收；完整校验并取得整厂计划租约后才为 accepted。单工序首次可直接完整接收。
9. **另行确认合成执行。** 只有权威 accepted、来源有效且整厂租约存在，才能显式执行一次原创事件模拟。running、completed、failed 是执行状态，不能用 accepted 代替 completed。

刷新/重新读取只读对象，不自动模型调用、递送、回读接收器或执行。接收器独立 readback 是另一个有审计的明确动作。跨 owner 的整厂计划冲突仅报 busy，不展示他人对象；整厂互斥不支持跨计划并行优化。

## 4. 当前 HTTP API

API 前缀 `/api`；除首页、`/health`、`/static/app.js`、`/static/style.css` 外均要求 Bearer。列表响应是 `{"items": ...}`；业务对象包含 `kind/object_id/owner/plant_id/status/version/content_hash/payload`。API 中引用 `snapshot_id` 时应使用快照**存储 object_id**，不是 payload 内自然 snapshot_id；对象 ID 必须原样保留，不自己拼接。

发布对象 API 使用白名单投影，只返回状态、来源/候选绑定、累计已接收工序和执行结果，不返回原始 signed_request、receiver_receipt 或适配器内部 pending_action。其顶层 `content_hash` 仍是完整持久化对象的 opaque 版本绑定，**不是这份删减后 payload 的摘要**，客户端不应据投影重算并宣称签名验证。独立回执验证由后端 readback 完成。

所有 mutation 使用 `Content-Type: application/json` 和严格 JSON 对象，拒绝重复键、非有限值、多余字段、错误类型、query 和跨源写入；请求上限 1 MiB。不在请求体传角色、本机文件路径、原始签名或 receiver 状态。`action_id` 绑定完整请求，相同 ID 不同内容拒绝。一次独立动作使用一个新 ID；网络结果未知时先读取权威状态，不盲目换 ID 重做。

### 读取和任务

| 方法与路径 | 请求体/结果要点 |
| --- | --- |
| `GET /api/me` | 当前 user_id、role、plants、acl_version |
| `GET /api/snapshots` | 本 owner 可访问快照列表 |
| `GET /api/snapshots/{snapshot_id}` | 当前快照对象 |
| `GET /api/snapshots/{snapshot_id}/scenarios` | 可用示例情景；自定义来源可能仅 baseline |
| `POST /api/snapshots` | `{action_id, snapshot}`；reviewer/admin 导入严格 Snapshot |
| `GET /api/tasks`、`GET /api/tasks/{task_id}` | 任务与累计用量/状态 |
| `POST /api/tasks` | `{action_id, snapshot_id, scenario}`；只创建 |
| `POST /api/tasks/{task_id}/run` | 严格 `{}`；不是后台自动触发，可能等待本地推理 |
| `POST /api/tasks/{task_id}/cancel` | `{action_id}`；取消后仍记在途 usage |
| `POST /api/tasks/{task_id}/resume` | `{action_id, answers: [{field, value}], note}`；只保存补缺，之后另行 run |
| `POST /api/tasks/{task_id}/reconcile` | `{action_id, call_id, no_result: true, tokens: 整数或null, note}`；admin，且无活跃 worker |
| `GET /api/recommendations`、`GET /api/recommendations/{recommendation_id}` | 待审/已审建议及证据 |
| `GET /api/publications`、`GET /api/publications/{publication_id}` | 发布状态，不等价于独立读取 receiver |
| `GET /api/audit` | 当前 owner/工厂范围的审计事件 |

补问路径来自当前任务 `awaiting_fields`，例如 `materials[0].initial_stock`、`orders[0].due_min`；示例索引并非任何任务的默认答案。必须原样提交全部实际路径；due 为 0–1440 整数，库存为受领域上限约束的非负整数，不猜 0。只有 source 中原本 None 的值可以补。

### 人工动作

| 方法与路径 | 严格请求体 | 不代表什么 |
| --- | --- | --- |
| `POST /api/recommendations/{id}/review` | `{action_id, recommendation_hash, decision: "approved"或"rejected", note}` | 只审阅，不发送；这是 HTTP 请求值，不是发布或执行指令 |
| `POST /api/recommendations/{id}/request-publication` | `{action_id, recommendation_hash, confirmed: true}` | 只创建唯一发布请求，不递送 |
| `POST /api/publications/{id}/deliver-local` | `{action_id}` | 首次递送；不接受 fault、签名或操作 ID 列表 |
| `POST /api/publications/{id}/advance-receiver` | `{action_id}` | 完成已确认 partial 的剩余接收，不是重传 |
| `POST /api/publications/{id}/readback` | `{action_id}` | 独立读取并验证 receiver 回执，不执行 |
| `POST /api/publications/{id}/cancel` | `{action_id, note}` | 权威撤销未执行计划，不回滚已执行历史 |
| `POST /api/publications/{id}/execute-synthetic` | `{action_id, confirmed: true}` | 另行启动合成事件引擎，不操作现场设备 |

审阅 `decision` 在 HTTP schema、API 和 `ActionService.review` 中统一直接使用 `approved` / `rejected`，没有中间词汇转换；`approve` / `reject` 不是合法请求值。

`recommendation_hash` 是最近读取对象顶层 `content_hash`，不是 candidate plan_hash。审阅改变对象内容，因此发布前应再次读取最新 hash。`confirmed`、`no_result` 必须是真正 JSON `true`，不能用字符串或数字。`note` 要求非空且最多 2000 字符。没有浏览器签名、接收回调或调用者指定权威成功状态的 API；额外执行扰动仅通过后端 Python 模拟接口/测试，不是公开 HTTP 或独立 CLI 命令。

### 安全的 curl 示例

下列示例**未执行**。先自行启动本地实例，仅在私有终端使用。示例为 Bash；关闭 xtrace，通过隐藏输入将 token 放在临时 shell 变量中，不写字面凭据、不 export、不放命令行参数或日志。`curl --header @-` 从标准输入读头；`-q` 不读 `.curlrc`，`--noproxy '*'` 不经代理。不要启用 `-v`、`--trace`、tee 或录屏。

```bash
set +x
IFS= read -r -s -p '本地访问令牌: ' SCHEDULING_TOKEN
printf '\n'

# 只读取当前 owner 的快照。
printf 'Authorization: Bearer %s\n' "$SCHEDULING_TOKEN" |
  curl -q --silent --show-error --fail --noproxy '*' \
    --header @- http://127.0.0.1:8006/api/snapshots

# 将占位 ID 替换为上一步当前对象的 object_id；仅创建，不运行。
printf 'Authorization: Bearer %s\n' "$SCHEDULING_TOKEN" |
  curl -q --silent --show-error --fail --noproxy '*' \
    --header @- --header 'Content-Type: application/json' \
    --request POST http://127.0.0.1:8006/api/tasks \
    --data '{"action_id":"example-create-001","snapshot_id":"<SNAPSHOT_OBJECT_ID>","scenario":{"kind":"baseline"}}'

unset SCHEDULING_TOKEN
```

再次尝试时不能随意复用例子 action_id 对应不同对象。API JSON 响应也可能含敏感业务状态，不应公开粘贴。shell 变量只是显式给 curl 的临时输入，应用不读取环境 token，不从 dotenv 或 profile 自动获取凭据。

## 5. 状态、失败与恢复

| 状态/错误 | 正确处理 |
| --- | --- |
| `awaiting_input` | 检查当前缺失路径，只补真实库存/交期；不把“不具资质”“库存为0”当缺失 |
| `no_schedule` | 读取真实求解状态；INFEASIBLE 才是本模型的无解结论，UNKNOWN 不作无解证明 |
| `blocked` / 预算耗尽 | 检查错误、证据和累计预算；不自动改 provider 或刷新预算 |
| 来源 stale/changed | 获取新的合法来源并重新审查；不能刷新 observed_at_ms 冒充新观测 |
| 模型 unknown | 保留账本，由本 owner admin 核实 call_id 和无结果事实后对账；`tokens=null` 保守耗尽剩余预算，不记0、不重发 |
| 发布 delivery unknown | **只独立 readback，绝不再次递送**；无回执也不能认定没发送或创建新 publication 绕过 |
| `partial` | 只是 staged，未占料未执行；已确认后可显式完成剩余接收 |
| `accepted` | 完整接收并持有整厂租约，尚未执行；若要执行须另行确认 |
| 执行 `running` | 不自动重跑；崩溃或最终结果不明时保留事实并人工核查 |
| 发布取消 `too_late` | 接收器执行已开始，不回滚；任务取消与发布撤销不是同一操作 |
| 接收 key 缺失/变化 | fail closed，核查原密钥和安全备份，不自动生成新密钥假装历史连续 |

回读到“曾经 partial”不等于最近一次推进接收或撤销已结束。后端为在途动作记录 `minimum_sequence = 此前 receiver_sequence + 1`，验证回执确实由接收器历史账本签发，并按最新权威状态归并。若最新回执仍低于门槛且无权威取消/执行事实，unknown 保留；不要把旧 partial 当作再次推进的许可。权威取消或执行事实，以及后端同步确认的特定推进拒绝，按 [架构说明](architecture.md) 处理；用户不能把超时/崩溃自行判为已拒绝。

未决递送/推进的整厂 delivery intent 持久保留，阻止同厂其他 publication 的创建、接收或执行准入；改 action_id、换 publication 或 owner 都不能绕过。回读无回执或仅有不足以结清的旧回执，不会自动释放阻挡；这些校验由后端完成，不要求浏览器访问 pending_action 或原始签名。

每任务最多 16 次模型调用、14 次工具、40,000 tokens、600 秒协作耗时；worker 租约 1200 秒。不是硬取消承诺，在途调用/清理可能超过门槛，已知迟到用量仍须计入。已执行的 source epoch 和 operation IDs 保留，后续计划必须使用更高 revision、更晚观察时间及新的未执行工序，不能用新 ID 重跑旧快照。

## 6. 如何看待结果

甘特图和库存轨迹来自已独验候选或实际合成事件，不是模型画出的假数据；但系统不支持拆批、抢占、同机多模式、多计划并行优化或任意生产规模。紧邻换型、整数分钟、软交期和 24 剩余工序/1440 分钟都是显式模型限制。共享 HMAC 的同进程接收器不提供不可否认性、物理隔离、分布式 exactly-once 或生产认证。

真实 Qwen、TEST 编排、CP-SAT、独验/oracle、Chrome、包安装及合成执行应分开验收。本文不报告任何最终测试总数、浏览器通过、模型通过或安装通过；最终由负责人在 [验收记录](validation.md) 记录实际运行结果。合成模拟不证明现场因果收益。
