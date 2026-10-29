# 服务与 Agent 契约 v1

## 身份与对象

`SchedulingService(store: Store, *, clock=None)` 使用毫秒时钟。Store 接口沿用本项目 storage.py，owner + plant + active/role/ACL 每动作重验。对象公共形状为 kind/object_id/owner/plant_id/status/version/content_hash/payload，输入路径不得传本机文件路径或身份角色。

- `import_snapshot(principal, snapshot: Snapshot, action_id: str) -> object`：reviewer/admin；快照自然 ID 经 owner+plant+snapshot_id 哈希得到全局对象 ID；首次创建或更高 revision 替代，较旧/同版本不同内容拒绝。快照 hash 与 object content_hash 区分：payload 顶层为 Snapshot.model_dump(mode='json')。
- `get_snapshot(principal, snapshot_id: str) -> object`：这里 snapshot_id 指存储 object_id；任何引用必须重新授权。
- `source_binding(conn, principal, snapshot_id) -> dict`：绑定 object_id/version/content_hash/source revision/plant_id；运行/送审/审批/发布前重验。
- 快照未来、超过 24h、缺失值未补充时不能求解或送审。结构缺失或错误不猜测。None 字段补充由独立操作生成新 revision，源观察时间不变。
- `submit_schedule(conn, principal, task, candidate, evidence, explanation) -> object` 仅可信引擎调用；验证全部内容确由持久化本 task 签发，来源当前有效、候选独验通过、比较及模拟证据齐全。创建唯一 `plan-<task_id>` recommendation/status=pending_review。

待审记录 payload 至少：task_id、snapshot_id、source_binding、scenario、compiled_problem、candidate（Plan）、validation、comparison、simulation、evidence、untrusted_model_draft、review=None。独验/候选权威，模型解释不覆盖事实。普通 hash 不叫密码学签名。

## 引擎接口与九工具

`AgentEngine(service, provider, *, clock=None)`；`create(principal, snapshot_id, *, scenario: Scenario, action_id)`、`get(principal, task_id)`、`run(principal, task_id)`、`cancel(principal, task_id, *, action_id)`、`resume(principal, task_id, *, action_id, answers: list[dict], note)`、`reconcile(principal, task_id, *, call_id, action_id, tokens: int|None, note, no_result=True)`。

任务状态：created/running/awaiting_input/blocked/submitted/no_schedule/cancelled。对象 ID 全局随机。任务 payload 至少含 snapshot_id/source_binding/scenario、evidence、candidate、awaiting_fields、calls/tokens/tools/unknown_calls/elapsed_s/accounted_call_elapsed_s/error、lease_id/lease_until_ms、provider_fingerprint、pending_action、handled_call_ids。

工具精确名称：

1. `get_snapshot`：取权威源快照，产出缺失字段和来源证据。
2. `build_scenario`：仅应用用户创建任务时授权的 Scenario。
3. `compile_problem`：调用真实 compiler，编译版与 source/scenario hash 冻结。
4. `solve_schedule`：同一问题分别跑 feasibility baseline 和 optimize，单 worker 各最多 5s，明确真实状态。无可行候选时无排程终态，UNKNOWN 不称无解。
5. `validate_schedule`：独立重算两候选，失败不能继续比较/送审。
6. `compare_schedules`：同源同目标已独验候选，选实际更优或唯一合法候选；平局 baseline。
7. `simulate_schedule`：在同一个已编译情景下分别模拟全部已独验候选，结果按 plan_hash 索引并标识合成；可在 compare 前或后执行。送审必须使用比较选中候选对应且完成的模拟结果，不受LLM改参数。
8. `request_human_input`：只补 source 中实际 None 字段。
9. `submit_schedule`：只送审冻结候选与全部工具证据。

前七个 arguments **必须 `{}`**。补问严格 `{"question": str, "fields": list[str]}`，fields 精确为当前缺失路径集合，无遗漏重复。提交严格 `{"candidate_hash": sha256, "evidence_ids": list[str], "explanation": str}`，候选哈希及全部当前 evidence IDs 精确绑定，不能伪造、修复或 strip 多余参数。

`available_tools(context)` 从 evidence 状态派生：先 get_snapshot；缺失则仅补问；其余按依赖 build→compile→solve→validate；validate 后 compare/simulate 可换序，均完成才 submit。模型每步只选择一个当前可用工具。不执行任意 SQL/Python/URL 或模型提供的排程。

## Provider 协议

`ProviderReply(action: dict,tokens:int)`、`ProviderError(code,message,*,tokens=None,sent=True)`、`LocalProvider(url,model=...)`、`ManagedLocalProvider(config_path)`、`SyntheticProvider()`；`decide(context)->ProviderReply`、`session()` 上下文管理、`close()`、fingerprint、metadata、is_test_provider。

context 顶层白名单：task_id、context_hash（源绑定哈希）、available_tools、evidence、candidate（至少 plan_hash）、required_input。evidence 按工具名称索引，值含 evidence_id/tool/payload/hash；transport 给模型有界摘要，不泄漏用户token/接收密钥/文件路径。取证 payload 带 source摘要及缺失路径；submit数据不是由provider自行拼排程。

精确 schema+当前issued values约束；已知usage先保存再验动作；unknown 与已知tokens分开，不自动重试。纯本地回环HTTP，无代理/重定向/云回退，显式关 tracing 与callbacks。

预算：16模型调用、14工具、40000累计tokens、600秒协作耗时；租约1200秒。不是硬实时中断。沿用经验证的 started日志先提交、模型等待不持事务、fenced pending reply、取消/撤权仍记账、迟到用量单调、过期unknown对账、同事务停止采样水位。provider.session 每run一次，close仅app结束。

## API/CLI 与 UI 约定

API前缀 `/api`：`GET /me,/snapshots,/tasks,/recommendations,/publications,/audit`、相应对象GET；`POST /tasks` body={snapshot_id,scenario,action_id}；`POST /tasks/{id}/run`；取消/补充/对账各独立POST。图外路径由 actions-contract 定义。

create_app(data_root,provider,*,clock=None) 返回FastAPI；127.0.0.1:8006；health 不推理。令牌Bearer、mutation same-origin、strictJSON上限、no-store/CSP与本地host守卫。浏览器token仅内存，图表原生DOM/SVG，不CDN或raw innerHTML。

CLI：`production-scheduling init DATA`（全新目录/随机一次token/导入demo，不求解），`fixture --case normal`（输出Snapshot JSON），`serve DATA --test-mode|--runtime-config ABS|--model-url LOOPBACK` 三选一。不需要训练命令。实例构造不启动模型，serve退出负责close。
