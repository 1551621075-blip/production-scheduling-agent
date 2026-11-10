# 排产分析 Agent（本地 Demo／测试版）

本仓库提供本项目的本地 Demo／测试版本，用于生产部署前在本地验证核心功能、业务流程与交互方式。

基于原创合成订单、工艺与资源数据进行排产分析：把订单、工艺、物料、在制品、资源与日历编译为实际约束问题，用 **OR-Tools CP-SAT** 求解，由独立校验器核对候选，再通过中文工作台进行人工审阅、发布、接收与合成执行。

**使用范围：** 本版本使用合成数据与本地测试环境，不包含真实业务或设备数据、内部资料、生产模型及敏感配置。本地验证结果不代表生产环境的可用性、效果或业务收益。

不连接生产 MES。**模型文字不是排程；送审不等于批准，批准不等于发布，接收不等于执行。合成模拟不证明现场收益。**

## 能解决什么

- 正常排产、插单、设备停机、到料延迟四类显式情景；模型不能替用户选择情景或修改目标权重。
- 整批 BOM 消耗与产出、库存和供料、剩余工序及加工中残余、交期、机器与辅助资源资格/容量/日历、序列相关换型、冻结承诺。
- 真实两轮求解：先找可行基线，再优化加权目标；只比较同一问题、来源和目标口径的有效候选。
- 独立重算工序覆盖、时长、路线、物料、资源、日历、冻结、换型和目标；另有小规模独立枚举 oracle。
- 真实事件队列模拟；供料延迟或停机可以阻断执行，而不是把计划复制成“执行成功”。
- 中文甘特图、辅助资源累计占用、库存事件阶梯、目标比较、证据链与人工操作工作台。
- 来源版本、owner/工厂隔离、调用记账、预算、未知结果对账、发布回执回读及防重复合成执行。

### 原创案例

示例车间有两台机器、操作员与夹具资源，两个有订单的批次，以及已有加工历史和冻结工序。基线数据有真实闭班间隙、BOM 投料与定时供料；插单、停机和延迟分别改变约束后重新求解。`normal`、`infeasible`、`missing` 三个快照由代码生成，时间取当前时钟，不使用真实客户、订单或设备数据。

目标是四项的非负整数加权和：优先级加权拖期、换型分钟、完工跨度、机器占用负荷差。交期是软目标，其余规定约束不能为了更好目标而放宽。`UNKNOWN` 不是无解；只有实际求解证明才报告 `INFEASIBLE`。校验通过不证明最优。

## 快速启动

要求 **Python 3.12、uv、POSIX 环境**。原生依赖及受管模型运行时还须匹配本机 CPU/OS；当前不声明 Windows 支持。

在项目根目录执行：

```bash
uv sync --locked
uv run --no-sync production-scheduling --help
uv run --no-sync production-scheduling fixture --case normal

# 必须是仓库外、父目录已存在、尚不存在的新目录。
SCHEDULING_DATA="$HOME/scheduling-demo-data"
uv run --no-sync production-scheduling init "$SCHEDULING_DATA"

# TEST 只代替模型选工具；求解、独验、模拟和存储都实际运行。
uv run --no-sync production-scheduling serve "$SCHEDULING_DATA" --test-mode
```

`init` 只显示一次随机管理员 token，请妥善保管，不要录屏、写入脚本或上传。打开 **http://127.0.0.1:8006/**，输入 token 后：

1. 选择快照和情景，创建任务，再单独运行。
2. 缺值时按精确字段路径补齐；完整时查看候选、独验、模拟和比较。
3. 人工批准后另行请求发布，再递送到本地合成接收器。
4. 多工序首次仅为 `partial`；完成接收并独立回读后，另行确认合成执行。

token 只保存在当前页面内存；刷新需重新登录。数据目录含私有数据库与接收密钥，不放进源树或版本库。`init` 拒绝已有目录，不能用来覆盖或重置运行历史。

### 使用真实本地模型

`serve` 必须显式选择一种 provider，**没有云端回退或失败后自动改用 TEST**。停止上面的 TEST 服务后，可选择：

```bash
# 操作者已经启动的数字回环 HTTP 模型服务。
uv run --no-sync production-scheduling serve "$SCHEDULING_DATA" \
  --model-url http://127.0.0.1:18080

# 或使用显式审阅的仓库外受管运行时配置。
uv run --no-sync production-scheduling serve "$SCHEDULING_DATA" \
  --runtime-config "$SCHEDULING_RUNTIME_CONFIG"
```

受管目标为 Qwen3-4B-Instruct-2507 GGUF Q4_K_M 与 llama.cpp CPU 运行时；模型、原生 binary、动态库和实际配置不随仓库分发。配置形状、文件身份校验、作用域进程清理和局限见 [本地运行说明](docs/runtime.md)。`health` 正常不代表实际模型调用已经通过。

## 技术链路

```text
显式快照与情景
  → build_scenario → compile_problem → solve_schedule
  → validate_schedule → compare_schedules + simulate_schedule
  → submit_schedule（仅待人工审阅）

图外人工动作：审阅 → 请求发布 → 部分/完整接收 → 独立回读 → 确认合成执行
```

LangGraph 使用 `agent → guard → tools` 循环，开放九个受控工具：`get_snapshot`、`build_scenario`、`compile_problem`、`solve_schedule`、`validate_schedule`、`compare_schedules`、`simulate_schedule`、`request_human_input`、`submit_schedule`。前七个工具不接受模型提供的参数；补问只能引用当前缺失路径，送审必须引用当前候选和全部七份已签发证据。

昂贵求解和模拟在数据库事务外执行，返回后重新检查来源、租约与待执行动作。证据除内容摘要外，还须有独立持久化签发收据。模型无审批、签名、递送、执行、SQL、Python 或任意 URL 工具。

本地接收器与应用同属一个可信进程/Store，HMAC 用于认证消息；它不是物理 MES，不提供秘密持有方之间的不可否认性，也不是分布式 exactly-once 证明。默认审批不强制另一自然人双人复核。

## 验证与范围

```bash
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync python scripts/check_release.py

# 输出目录必须新建且位于源码之外；其父目录须已存在。
uv run --no-sync python scripts/run_acceptance.py \
  --test-provider --data-root "$SCHEDULING_ACCEPTANCE"
```

实际全量测试、六案例 TEST 链路、真实 Qwen、真实 Chrome、wheel/sdist 与源码外离线安装结果**分开记录**，见 [验证记录](docs/validation.md)。当前冻结核心一次完整测试 **2124 项通过、2 条预期警告**；六案例 TEST 全通过；真实本地 Qwen normal 完成 **8 次调用、19592 tokens**；真实 Chrome **34 项通过**。最后一次模型请求约 172 秒，CPU 性能余量有限，一次通过不代表持续可靠性。脚本 `--help`、mock 测试或其他工程的结果都不能替代这些实际验收。

范围上限为 1440 个整数分钟、24 道剩余工序、8 台机器、12 个辅助资源池。数量为不可转换的离散整数单位；整批不拆批，不自动采购、补货或替料，不跨计划联合优化。不支持正在换型的残余作业。接收器完整计划按整厂互斥；执行消耗过的来源批次/工序不能通过创建新任务绕过重复执行限制。

## 文件导航

| 位置 | 内容 |
|---|---|
| `src/scheduling_agent/domain.py`、`fixtures.py` | 严格数据契约、原创合成案例 |
| `compiler.py`、`solver.py` | 不可变问题编译与实际 CP-SAT |
| `validator.py`、`simulation.py` | 独立可行性校验与事件执行 |
| `agent.py`、`provider.py`、`managed_provider.py` | 九工具图、工具选择、受管本地模型 |
| `storage.py`、`service.py` | 授权存储、来源血缘、证据签发 |
| `actions.py`、`receiver.py` | 人工审阅、HMAC 发布、回读与合成执行 |
| `api.py`、`cli.py`、`templates/`、`static/` | 本地接口、命令行、中文工作台 |
| `tests/test_oracle.py`、其他 `tests/` | 独立微型枚举及领域/恢复/边界测试 |
| `scripts/` | 独立验收、浏览器及发行/安装门禁 |

进一步阅读：[架构](docs/architecture.md) · [使用与 API](docs/usage.md) · [领域契约](docs/domain-contract.md) · [证据服务契约](docs/service-contract.md) · [人工动作契约](docs/actions-contract.md) · [依赖](docs/dependencies.md) · [第三方声明](THIRD_PARTY_NOTICES.md)。第三方许可不代表已为本工程原创代码授予统一开源许可。
