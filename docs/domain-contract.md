# 排产数据与算法契约 v1

此文件是本原创工程的精确实现契约；无生产事实或现场收益声明。领域接口由 `domain.py` 定义，核心计算模块不得相互复制约束实现。

## 单位、数量与工艺

- 绝对 `observed_at_ms` 为 UTC 毫秒，快照规划时间从 0 开始；所有排程变量为严格整数分钟，规划期 1..1440。拒绝 bool、float、字符串数字。半开区间 `[start_min,end_min)`，结束释放先于同时刻开始。
- Item 的 `unit` 是不可转换的离散量纲，每个数量是该单位的整数个。BOM 每行表示每一父件在指定 `step_index` 消耗 `units_per_parent` 子件；整批消耗为 lot.quantity × 系数，不四舍五入，不拆批，不自动补货或增产。
- Item BOM 无环，引用有效，指定步序必须存在于生产该 Item 的每个 Lot。每 Lot 的全部路线固定、正时长、非抢占；最后未完成工序结束产出该 Lot.quantity 的 Item。
- Order 显式绑定唯一 Lot，同 Item、同数量，不能两个订单占用同一批；订单交付发生在该批最后工序完成时，同刻从库存扣除。组件 Lot 可没有订单，但仍是必须完成的已授权生产批。
- `completed_prefix` 表示快照前已完成的路线前缀，不产生排程或未来物料事件，其影响已计入期初库存。允许第一未完工序为 residual：固定机器、从 0 开始、明确剩余时长、已发料不再扣除，完成时后续/成品正常记账。不支持正在换型的残余状态，必须明确拒绝，不能猜测时长。
- 除 residual 外，每工序输入在加工 start 消耗；每 Lot 的产品在终端 end 入库；外部 Supply 在指定分钟入库。每分钟先聚合全部收料，再聚合全部扣料。检查每个事件后库存非负，包含中途和同刻重复占料，不能只检查期末。

## 主辅资源、资质、日历与换型

Machine 为容量 1，Resource 为明确容量的同质共享资源池；不同资格/班次的人或工装应分别建资源，不假定可互换。Machine 和 Resource 都有非重叠开放 Window 及 `qualified_families`。

工序 Alternative 指定一台机器、整批正整数时长、共享资源需求和显式 `qualified`。只有 alternative.qualified 且机器及全部需求资源具备目标 family 的资质才可选。明确 false 或空 alternatives 是已知不可行，而不是缺失。

Machine 初始 family 明确。每台机器对初始/可能工序 family 到可能目标 family 的换型表必须完整。SetupRule 指定 duration_min≥0 与共享资源需求；换型及加工使用同一机器，换型需资源资格支持目标 family。

**本 v1 的确定约定：换型紧邻加工前，不可抢占；setup interval 为 `[setup_start_min,start_min)`，不允许空闲插在换型与该加工之间。** 该约定属于模型限制，不宣称全行业通用。加工和换型分别满足资源日历，不跨闭班。正换型和加工在机器上无重叠；前一加工 end≤后一 setup_start。首道使用机器初始 family，后道只按真正相邻工序计算换型，禁止全对全重复计费。零换型仍校验前驱。residual 固定第一个、setup_start=start=0，不再换型。

路线先后约束 prev.end≤next.setup_start；批 release≤首道 setup_start（residual 特例 release=0）。从而本模型禁止提前为路线未准备好的工序换型。共享资源需求之和不能超过容量，等时刻先释放再获取。

## 冻结与情景

Snapshot 含 `freeze_until_min` 和 `commitments`（来源快照中的已承诺 Slot）。凡 commitment.setup_start_min < freeze_until_min 必须逐字段冻结，包括 machine、start/end、setup_start 和 predecessor_id；residual 始终冻结。新停机覆盖冻结加工/换型时是真正不可行，不能偷偷移走。

Scenario 为严格结构化、人先选定的扰动，不让 LLM 自由修改：`baseline`、`insert_order`（明确新增 Lot/Order）、`machine_outage`（指定机器半开停机区间 `[start_min,end_min)`）、`supply_delay`（指定现有 supply_id 新时间）。builder 生成新内容哈希但保留 source observed_at/revision 血缘，不刷新时效；现有冻结承诺不变。无效变更拒绝。

缺失仅允许显式 None 的 Material.initial_stock 与 Order.due_min；工具补问包含准确字段路径。补充只能一次填全部缺失项，不覆盖已知数值、不重置 observed_at；其他结构字段缺失按无效输入拒绝而非猜测。

## 优化、状态、独验

Order.due_min 是**软交期**，迟到可行但计罚；release、horizon、日历、资源、资质、物料、冻结均为硬约束，不罚分代替。

ObjectiveConfig 非负整数权重，至少一个非零，分别对应 `weighted_tardiness`（订单 priority × max(0,终端end-due)）、`setup_minutes`（全部正换型时长）、`makespan`（全部剩余工序最晚end）、`load_spread`（全部机器加工+换型占用分钟 max-min，含未用机器0）。总目标为四项加权和，不声称字典序。精确整数重算，界定上限以避免 int64/浮点溢出。

同一个 compiled problem 两次真实 CP-SAT：`feasibility` 模式无最优化目标，首个可行基线只标 FEASIBLE；`optimize` 模式使用上述目标。都返回真实 OPTIMAL/FEASIBLE/INFEASIBLE/UNKNOWN，UNKNOWN无候选不等于无解；solver异常或模型无效为显式 error，不伪造状态。

Compare 只能比较相同 snapshot_hash、problem_hash、objective 配置的独验通过候选，选择更低实际目标，平局稳定选 baseline。不承诺优化运行一定胜过独立可行基线；可以如实选择 baseline。不同情景结果不能按同目标编号直接宣称改善。

验证器只共享 domain schema 与 canonical hash，不导入 solver/compiler 的约束 helpers，也不相信 solver.valid；独立重算 exact coverage、BOM/数量/WIP、机器选择/时长/资质、路线/release、相邻换型、主辅资源/日历、冻结、事件库存、目标及哈希。输出库存阶梯点、机器资源活动、违规码；无效候选不能比较/送审。

微型 oracle 独立枚举≤4个未完工序、H≤12 的机器选择/整数开始，独立约束判断且不调用 solver/validator，核对可行性与精确最佳目标（排程本身可多解）。

## 公开接口（待 domain.py 类型定义完成后锁定，不由调用者猜字段）

- `fixtures.demo_snapshots(now_ms) -> dict[str, Snapshot]`：normal/infeasible/missing；`demo_scenarios(snapshot) -> dict[str, Scenario]`：baseline/insert_order/machine_outage/supply_delay。
- `compiler.build_scenario(snapshot: Snapshot, scenario: Scenario) -> Snapshot`
- `compiler.compile_problem(snapshot: Snapshot) -> CompiledProblem`，缺失/过期由 service/validator 显式门控，不交给模型补值。
- `solver.solve(problem: CompiledProblem, *, mode='optimize', time_limit_s=5.0, seed=41723) -> SolveResult`，单 worker，禁止搜索日志或回调外发。
- `validator.validate_plan(problem: CompiledProblem, plan: Plan) -> ValidationReport`
- `validator.compare_plans(problem, baseline: Plan, optimized: Plan) -> dict`，两者先独验，不信调用者标记。
- `simulation.simulate(problem, plan, *, disruptions=()) -> dict`，原创事件引擎，实际推进合成资源/物料/订单；先独验计划，再显式应用执行扰动，记录 blocked/failure/completion，不静默把不满足冻结/库存的计划修成成功。

时间与规模限制是可用性边界，应拒绝超限，不截断或静默采样。
