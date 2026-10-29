# 架构与排产语义

## 1. 原创参考工程的范围

本项目是面向**原创合成工厂数据**的本地排产参考实现，不复原私有生产系统，不接入真实订单、设备或 MES，也不声明生产部署、营收或现场收益。语言模型选择受控工具；真实 CP-SAT、独立验证、事件模拟和人工授权分别形成证据，不能互相替代。

精确约束以四份契约和对应实现为准：[实施契约](implementation-contract.md)、[领域契约](domain-contract.md)、[服务契约](service-contract.md)、[人工动作契约](actions-contract.md)。使用入口见 [使用说明](usage.md)，provider 和进程边界见 [运行说明](runtime.md)，第三方来源见 [依赖说明](dependencies.md)。最终运行证据由负责人填写 [验收记录](validation.md)；本文不报告测试总数或真实模型、浏览器、安装验收成功。

## 2. 模块与信任边界

| 层 | 实际模块 | 责任与不可越过的边界 |
| --- | --- | --- |
| 不可变领域数据 | `domain.py`、`fixtures.py` | 严格整数、引用、规模、BOM、整批工艺；fixture 只生成合成输入 |
| 情景与编译 | `compiler.py` | 只应用人选定的结构化情景，展开剩余工序和整批耗料，冻结编译版本与内容 hash |
| 约束求解 | `solver.py` | 实际调用 OR-Tools CP-SAT，返回候选与真实求解状态，不由模型编排程 |
| 独立复核 | `validator.py` | 从领域数据重新计算约束、事件库存及目标，不导入 solver/compiler 的约束 helpers |
| 微型枚举参照 | `tests/test_oracle.py` | 独立枚举小问题的机器选择和整数开始时刻；oracle 的可行性/目标计算不调用 solver 或 validator |
| 事件执行 | `simulation.py` | 验证后实际推进合成加工、换型、资源、供料与交付事件，不把排程表直接标记为执行完成 |
| Agent 与证据 | `agent.py`、`service.py` | LangGraph 单 Agent，受控菜单、调用日志、预算、租约、工具证据与送审 |
| 人工动作和接收 | `actions.py`、`receiver.py` | 图外审阅、HMAC 发布、分阶段接收、独立回读、撤销、另行确认的合成执行 |
| 本地边界 | `storage.py`、`provider.py`、`managed_provider.py`、`api.py`、`schemas.py`、`cli.py` | SQLite、身份与 ACL、严格协议、数字回环模型请求、显式 CLI |
| 浏览器 | `templates/index.html`、`static/app.js`、`static/style.css` | 中文工作台、原生 DOM/SVG；不赋予浏览器接收器或签名权限 |

以上 Python 模块位于 [`src/scheduling_agent`](../src/scheduling_agent)。求解器、独验器、微型 oracle 是不同计算路径，不是三次调用同一个约束函数。模拟器会先调用独验器，再用自己的事件状态推进执行；它不是第四个独立最优性证明器。oracle 仅覆盖微型问题，不能证明所有受支持规模均正确。

## 3. 输入、单位与有界规模

- `observed_at_ms` 是 UTC 毫秒；排产从快照分钟 0 起算。规划期 `horizon_min` 为 1–1440，时间变量都是严格整数分钟；区间采用 `[start_min, end_min)`。布尔值、浮点值和字符串数字不能冒充整数。
- 最多 **24 道剩余工序**、8 台机器、12 类共享资源、20 个 Item、20 个 Lot、20 个 Order、64 次外部 Supply。每批路线最多 24 步；超限拒绝，不截断、抽样或隐式缩短规划期。
- 数量是 Item.unit 对应的不可转换离散整数单位；批量上限 1,000,000，期初库存上限 1,000,000,000,000，共享资源容量上限 64。BOM 无环，每个耗料步序必须存在于相应生产批路线。
- Lot 表示授权的**整批**生产，路线固定、时长明确、加工非抢占，不拆批、不自动增产、不补货、不自动换算单位。一个订单绑定一个同 Item、同数量的唯一 Lot；不同订单不能占同一批。无订单的组件批仍须完成。
- 每道工序每台机器最多一个 Alternative；不支持同一机器的多档速度/资源模式。Alternative 的时长是整批时长，不再按数量乘一次。机器、加工资源、换型资源均须满足目标 family 资质；`qualified=false` 或空 alternatives 是已知不可行信息，不是待补输入。

### 数量、库存与残余工序

BOM 耗料为 `lot.quantity × units_per_parent`，在指定工序加工开始时扣除。批末工序结束时才将整批产品入库；订单在对应批完成的同刻交付并扣库存；Supply 在给定分钟收料。同一分钟先聚合收料，再聚合扣料，逐事件检查余额非负，不能只看期末余额或让两道工序重复占用同一份料。

`completed_prefix` 是快照之前已完成的路线前缀，其物料影响已经包含在期初库存中，不再创建未来加工或产出事件。第一道未完成工序可显式带 residual：固定机器，从 0 开始，仅执行已知剩余时长；已发物料不重复扣除，完成后正常进入后续工序或产出。residual 要求 release 为 0、机器首位、无新增换型且始终冻结。**正在换型的残余状态不受支持**，不得猜测剩余换型时间。

### 主辅资源、日历与紧邻换型

机器容量为 1；Resource 是具有明确容量、资格与开放日历的同质资源池。不同资质/班次的人或工装应拆成不同资源池，不假定可互换。

本版本规定换型**紧邻该次加工之前**，换型与加工之间不得插入空闲，二者均非抢占；`[setup_start_min, start_min)` 为换型，`[start_min, end_min)` 为加工。加工和换型分别满足机器及所需辅资源日历，不能跨闭班。机器上的前一加工结束不晚于后一换型开始；路线的上一工序结束不晚于下一工序换型开始；批 release 不晚于首道换型开始，因此不支持路线未就绪时提前换型。

换型表须涵盖初始/可能前驱 family 到目标 family 的所有必要转换。首道从机器初始 family 换型，后续只按**实际相邻工序**计费，零换型也检查前驱。不将所有工序对的换型同时相加。共享资源在同刻先释放后获取，校验容量之和而非仅逐任务需求。

### 冻结、补缺与情景

凡来源 commitment 的 `setup_start_min < freeze_until_min`，机器、换型开始、加工起止、机器前驱逐字段冻结；residual 始终冻结。停机覆盖冻结加工或换型可使问题真正不可行，不能偷偷移动承诺。

仅允许 `Material.initial_stock` 和 `Order.due_min` 显式为 `None` 后补问。一次补齐所有准确缺失路径，不覆盖已有数值；其他结构缺失直接拒绝。补缺生成新 revision，但**不改变原 observed_at_ms**。来源在未来或超过 24 小时不能继续求解/送审；改变情景、刷新网页或补缺均不能刷新观察时效。

Scenario 只能是人创建任务时授权的 `baseline`、`insert_order`、`machine_outage` 或 `supply_delay`。插单须给出完整 Lot/Order；供料延迟引用已有 supply 且时间严格推迟；停机从指定机器开放日历中扣除对应半开区间。情景构建保留原始 revision/观察时间血缘及冻结承诺。模型不得自行改变订单、交期、资源、扰动或目标权重。

## 4. 求解、独验、比较与模拟

### 真实求解和软交期

同一个 compiled problem 分别运行 `feasibility` 和 `optimize` 两次真实 CP-SAT，每次单 worker、求解限时 5 秒。机器候选使用可选区间，主资源 NoOverlap、辅资源 Cumulative、机器相邻关系 circuit、库存 reservoir 约束共同参与求解；同分钟收料/扣料用有序事件阶段表达。

`Order.due_min` 是**软交期**：迟到可行但计罚。release、规划期、日历、资源、资质、物料与冻结是硬约束，不用罚分替代。目标为四项非负整数权重的加权和，而非字典序：

1. `weighted_tardiness`：各订单 `priority × max(0, 完成分钟 - due_min)` 之和；
2. `setup_minutes`：换型总分钟；
3. `makespan`：所有剩余工序最晚结束分钟；
4. `load_spread`：所有机器加工加换型占用分钟的最大值减最小值，未用机器计 0。

权重至少一项非零；编译阶段限定目标上界，独验用精确整数重算。可行性运行的首个候选只报告 `FEASIBLE`，不宣称最优。优化运行区分 `OPTIMAL`、`FEASIBLE`、`INFEASIBLE`、`UNKNOWN`；模型无效或异常是 `ERROR`。`UNKNOWN` 无候选仅表示本次未获得候选，**不是无解证明**。

### 独立证据

验证器从源数据复核覆盖、数量、WIP、BOM、选机、时长、资质、路线、release、实际前驱换型、主辅资源、日历、冻结、逐事件库存、目标与内容绑定，输出违规码、库存阶梯和资源活动。不信任 solver 自报 valid，也不接受模型“修复”的时间表。

比较只接受同一 snapshot/problem/objective 且独验通过的候选。取真实目标较低者，平局稳定选 baseline；仅有一个合法候选时可选它。优化运行不保证比另一轮首个可行解更好，更不能把不同情景的目标值直接说成同条件改善。

微型 oracle 位于测试代码中，只枚举剩余工序不超过 4、规划期不超过 12 的完整输入，核对可行性和精确最佳目标，不要求排程唯一。

`simulation.simulate(problem, plan, disruptions=())` 是实际离散事件引擎，记录换型、加工、资源、物料及订单推进。阻塞时保留部分轨迹，不伪造完整目标或自动改排程。执行期额外扰动只支持停机/供料延迟；插单要先构建情景、重新编译求解。送审前模拟分别覆盖已独验候选，按 plan_hash 索引；送审必须使用比较所选候选对应的完成结果。所有结果标记 `synthetic=true`、`causal_effect_proven=false`，模拟改善不等于现场因果收益。

## 5. 单 Agent、九工具与七项分析证据

模型每次仅从后端当前菜单选择一步，不运行任意 Python、SQL、URL，不直接生成权威排程。LangGraph 负责调度，SQLite 保存任务和证据；模型文本只作为 `untrusted_model_draft` 展示。

| 工具 | 作用与依赖 |
| --- | --- |
| `get_snapshot` | 读取当前来源，列出实际缺失字段 |
| `build_scenario` | 只应用用户已选的 Scenario |
| `compile_problem` | 冻结同源编译问题 |
| `solve_schedule` | 获得真实 baseline/optimized 结果 |
| `validate_schedule` | 独立验证所有现有候选 |
| `compare_schedules` | 选择同源已独验候选 |
| `simulate_schedule` | 对全部已独验候选运行事件模拟 |
| `request_human_input` | 只请求当前缺失路径集合，不猜值 |
| `submit_schedule` | 核验精确候选 hash、全部当前证据和说明后仅送审 |

前七个工具的 arguments 必须严格为 `{}`，各自产生分析证据。补问只含 `question`、`fields`；送审只含 `candidate_hash`、`evidence_ids`、`explanation`。不自动删去多余参数、补造 hash 或重试“修复”回复。正常依赖链是 get→build→compile→solve→validate；validate 后 compare/simulate 可交换顺序，均完成才允许 submit。缺失时转人工；无候选时停止，保留真实求解状态。

### 证据计算、签发与持久化核验

`compute_tool` 在数据库事务外返回私有 `_ComputedPayload`，除公开 dict 内容外，带不序列化的计算来源和 HMAC seal。来源绑定 tool、task、owner/plant、source_binding、Scenario、已有 evidence、worker lease 与 pending_action；seal 使用当前 service 实例的临时随机密钥，同时覆盖来源和结果内容。`issue_evidence` 重新准备当前上下文，只接受原私有计算结果，核对来源、stamp 和 seal；不能把模型数据、普通 dict 或 JSON 往返后的副本当作可信工具结果签发。引擎仍须检查当前任务/租约栅栏，seal 不替代这些检查。

签发时，在写事务内先通过 `Store.record_receipt` 独立保存完整签发记录，再将证据 envelope 写入 task。该 Store 回执独立于 task 内嵌的 evidence；送审除重新核对 task、来源版本/内容、编译问题、独验、比较和选中模拟外，还逐项要求证据与持久化签发回执完全相符。仅重算出自洽的 evidence_id/hash，不能替代真实签发记录。`plan-<task_id>` recommendation 仍仅为 `pending_review`。

这几类机制不能混称为公开签名：证据内容 hash 只绑定内容；私有计算 seal 仅保护可信后端的 compute→issue 交接，临时密钥和 seal 不持久化、不进入 provider 上下文；持久化 Store 签发回执是本地签发依据；发布/接收的 HMAC 则认证另一类消息。它们都不构成第三方身份或可移植的外部证明，不能防御已控制同一进程或 Store 的攻击者。

## 6. 持久化与图外发布

每个动作重新检查 owner、plant、active 状态、角色/ACL；admin 不自动越过 owner 隔离。请求身份不来自 JSON body，幂等 action_id 绑定完整请求；同 ID 不同请求拒绝。人工审阅是独立授权步骤，但默认不强制第二个自然人复核。

模型调用先提交 started 日志，再离开数据库事务等待。CP-SAT 和模拟计算同样在事务外，返回后以 worker lease、当前来源和任务状态重新设栅栏。已知无效回复仍计 usage；取消/撤权后迟到用量继续记账，unknown 不当作 0。模型预算为 16 次调用、14 次工具、40,000 tokens、600 秒协作耗时，worker 租约 1200 秒；不是到点硬杀保证，详见 [运行说明](runtime.md)。

人工动作依次独立进行：

- review：`pending_review → approved/rejected`，**批准不发布、不接收、不执行**；
- request-publication：冻结唯一请求并使用 HMAC-SHA256 认证，不调用接收器；
- deliver-local：先保存交付意图，再调用本地接收器，最后处理 ACK；
- advance-receiver：从已确认 partial 完成剩余接收，不是再次发布；
- readback：直接读取接收器持久化的权威签名回执，不以发布对象缓存作证；
- execute-synthetic：完整接收后另行明确确认，才进入 `running → completed/failed`。

发布对象分别保存 `delivery_status`、`receiver_status`、`execution_status`。多工序首次递送只暂存排序后的第一项，`partial` **不占料、不执行**；完成全部接收并重新独验后，原子取得整厂计划租约才为 `accepted`。`accepted` 不等于已执行或已完成。

**整厂租约不是 1200 秒 worker 租约。** 它是保守互斥：每个合成 plant 同时最多一个完整 active 计划，跨 owner 也不能叠加使用库存/资源；不是按区间做跨计划并行优化。执行准入后保存来源 epoch 和 operation IDs；完成/失败释放 active 租约但不抹掉使用事实。后续计划需更高 revision、更晚观察时间和未执行的新 operation IDs，不能换 publication ID 重跑旧事实。

交付 unknown 时只做独立回读，**绝不盲目重传**；未读到回执不能推断未发送。后端在递送、推进接收或撤销前，把 `pending_action.minimum_sequence` 设为此前 `receiver_sequence + 1`。回读必须先验证 HMAC、请求绑定，以及该 sequence/内容确实存在于接收器历史账本，再按接收器**最新权威状态**归并；有效旧 ACK 不能让状态倒退。最新 sequence 仍低于本次 minimum_sequence、且既未取消也无执行状态时，旧 partial 不能结清在途 advance/cancel 的 unknown。

取消还有独立语义栅栏：只要 pending action 是取消，`partial` / `accepted` 且执行仍为 `none` 的回执就不能结清它，**即使回执序号已达到 minimum_sequence**。这覆盖发布端丢失 ACK、序号落后于接收器的情况，防止取消前已生成的回执冒充取消结果。

权威 `cancelled` 或任一非 `none` 的执行状态可结清上述等待条件，但不会把真实 running/completed/failed 改称 cancelled。另有一个窄例外：推进接收被后端同步确认拒绝时，可用已有权威回执结清该次已知拒绝；崩溃、超时、结果不明或未完成的取消不能套用此例外。界面不直接获得或操作内部 pending_action。

递送/推进接收还持久化整厂 delivery intent；未决期间，同厂其他 publication 的创建、接收推进及执行准入被阻挡，不能靠换 action_id、publication 或 owner 绕过。该意图不同于完整接收后取得的整厂 active 租约；仅在后端能够据权威状态结清时移除，读到不足以结清的旧 partial 不会释放它。

取消在接收器持久化 tombstone，防迟到消息复活；执行开始后的撤销返回 `too_late`，不回滚历史。崩溃遗留 running 不自动重跑，也不能由 readback 伪造 completed。

### 认证能力的边界

请求、回执、取消使用域分隔 HMAC，绑定 owner/plant、发布/候选/来源、全部工序、时效与 synthetic 标识。接收器入口要求后端持有的实例 capability，不提供浏览器签名或接收回调入口。秘密文件位于仓库外私有数据根，目录 0700、密钥文件 0600；已有接收历史但密钥丢失/不安全时 fail closed，不能自动换新密钥冒充连续性。

发布者和接收器位于**同一可信进程、同一 SQLite Store 的不同表/方法边界**，不是物理隔离主机或防恶意管理员的安全域。共享 HMAC 不提供秘密持有方之间的不可否认性，内容 hash 也不提供电子签章或第三方证明。单机事务与幂等记录不等于分布式 exactly-once；模型、数据库、操作系统或人工判断失效的后果仍需独立治理。
