(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TOOLS = ["get_snapshot", "build_scenario", "compile_problem", "solve_schedule", "validate_schedule", "compare_schedules", "simulate_schedule", "request_human_input", "submit_schedule"];
  const WORDS = {
    created: "已创建 · 尚未运行", running: "运行中 · 不得自动重跑", awaiting_input: "等待人工补缺", blocked: "已阻断 · 需要核查", submitted: "已送审 · 未自动批准", no_schedule: "未形成排程", cancelled: "已取消", pending_review: "待人工审阅", approved: "已批准 · 不代表已发布", rejected: "已拒绝", requested: "已请求发布 · 尚未递送", unknown: "结果未知 · 禁止盲目重发", partial: "部分接收 · 不占料不执行", accepted: "完整接收 · 尚不代表执行", completed: "合成执行完成 · 非现场加工", failed: "合成执行失败", none: "尚无", not_sent: "尚未递送", acknowledged: "已确认回执", ready: "可选来源", OPTIMAL: "OPTIMAL · 已证明最优", FEASIBLE: "FEASIBLE · 可行，未证明最优", INFEASIBLE: "INFEASIBLE · 已证明无解", UNKNOWN: "UNKNOWN · 未找到候选，不等于无解", ERROR: "ERROR · 求解错误", baseline: "原始 / 可行基线", optimized: "优化候选", insert_order: "授权插单", machine_outage: "机器停机", supply_delay: "供料延迟", operator: "操作员", reviewer: "审阅员", admin: "管理员（不自动跨 owner）", get_snapshot: "权威快照", build_scenario: "构建授权情景", compile_problem: "编译约束问题", solve_schedule: "两次真实求解", validate_schedule: "独立验证", compare_schedules: "同源目标比较", simulate_schedule: "合成事件模拟", request_human_input: "缺失字段补问", submit_schedule: "冻结候选送审", setup: "换型", processing: "加工"
  };
  const LABELS = {
    object_id: "对象 ID", owner: "所属 owner", plant_id: "工厂", status: "状态", version: "对象版本", content_hash: "内容哈希（非身份签名）", snapshot_id: "来源快照", observed_at_ms: "源观察时间", revision: "源修订", horizon_min: "规划期 / 分钟", freeze_until_min: "冻结边界 / 分钟", calls: "模型调用数", tools: "工具调用数", tokens: "已知 tokens（不包含未知用量）", unknown_calls: "未知 / 进行中调用数", elapsed_s: "累计协作耗时 / 秒", error: "停止 / 错误码", lease_until_ms: "租约截至 / 毫秒（仅展示）", lease_id: "服务端租约", provider_fingerprint: "提供方指纹", source_binding: "绑定来源", scenario: "人工授权情景", task_id: "任务 ID", plan_hash: "计划哈希", problem_hash: "问题哈希", snapshot_hash: "情景快照哈希", objective_hash: "目标配置哈希", weighted_tardiness: "加权迟期", setup_minutes: "换型分钟", makespan: "最大完工时间", load_spread: "机器负荷极差", total: "加权总目标", valid: "独验通过", violations: "违规事实", evidence_id: "证据 ID", hash: "证据内容哈希", tool: "工具", payload: "证据内容", delivery_status: "递送状态", receiver_status: "接收状态", execution_status: "执行状态", receiver_sequence: "权威回执序号", accepted_operation_ids: "累计已接收工序", recommendation_id: "推荐 ID", recommendation_hash: "冻结推荐哈希", review: "审阅记录", synthetic: "是否合成", note: "人工说明", action: "动作", created_at_ms: "记录时间", updated_at_ms: "更新时间", detail: "详情", call_id: "调用 ID", awaiting_fields: "准确待补路径", at_min: "相对分钟", initial_stock: "期初库存", balance: "余额", received: "收料", consumed: "扣料", quantity: "整数数量", unit: "离散单位", item_id: "物料", machine_id: "机器", resource_id: "辅助资源", capacity: "容量", operation_id: "工序", order_id: "订单", due_min: "交期 / 分钟", priority: "订单优先权重", start_min: "加工开始", setup_start_min: "换型开始", end_min: "结束", predecessor_id: "机器前驱", events: "真实合成事件", result: "结果", completed_operations: "完成工序", deliveries: "合成交付", material: "物料事件", resource_usage: "资源实际占用", metrics: "合成指标", blocked_reason: "阻断原因", decision: "审阅决定", reviewer: "审阅人", reviewed_at_ms: "审阅时间"
  };
  const SCENARIOS = ["baseline", "insert_order", "machine_outage", "supply_delay"];
  const KINDS = ["snapshots", "tasks", "recommendations", "publications", "audit"];
  const objectKinds = {snapshot: "snapshots", task: "tasks", recommendation: "recommendations", publication: "publications"};
  const emptyData = () => Object.fromEntries(KINDS.map((key) => [key, []]));
  let token = "", epoch = 0, data = emptyData(), me = null, ready = false;
  let snapshotId = "", taskId = "", recommendationId = "", publicationId = "";
  let scenarios = {}, scenarioSource = "", selectionSerial = 0, sourceSerial = 0;
  let reviewBinding = "", publicationBinding = "", supplementBinding = "";
  let needsReload = false, staleNotice = "", lastReadback = null;
  const jobs = new Set(), controllers = new Set(), bindings = [];
  const inFlight = new Set();
  const unknownWrites = new Set();
  const payload = (obj) => obj && obj.payload && typeof obj.payload === "object" ? obj.payload : {};
  const object = (kind, id) => data[kind].find((entry) => entry.object_id === id) || null;
  const word = (value) => WORDS[value] || value;
  const own = (obj) => !!(me && obj && obj.owner === me.user_id && me.plants.includes(obj.plant_id));
  const stamp = (value) => Number.isSafeInteger(value) && Number.isFinite(new Date(value).getTime()) ? `${new Date(value).toLocaleString("zh-CN", {hour12: false})} (${value} ms)` : "未记录";
  function scalar(value, key = "") {
    if (value === null || value === undefined) return key === "tokens" ? "未知（不是 0）" : "未提供";
    if (key.endsWith("_at_ms") || key === "observed_at_ms") return stamp(value);
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return "结构化记录";
    return String(word(value));
  }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function put(id, ...children) { $(id).replaceChildren(...children); }
  function empty(id, text = "尚无记录；不会补造数据。") { put(id, node("p", text, "muted")); }
  function pairs(entries) {
    const list = node("dl", undefined, "facts");
    entries.forEach(([key, value]) => list.append(node("dt", LABELS[key] || key), node("dd", scalar(value, key))));
    return list;
  }
  // All server/model strings are text nodes, never executable markup.
  function readable(value, depth = 0) {
    if (value === null || typeof value !== "object") return node("span", scalar(value));
    if (depth > 8) return node("span", "深层结构已折叠。", "muted");
    if (Array.isArray(value)) {
      const list = node("ul", undefined, "fact-list");
      value.slice(0, 300).forEach((entry) => { const li = node("li"); li.append(readable(entry, depth + 1)); list.append(li); });
      if (!value.length) list.append(node("li", "无条目"));
      if (value.length > 300) list.append(node("li", `本视图展示前 300 / ${value.length} 条；不代表全部事件。`));
      return list;
    }
    const list = node("dl", undefined, "facts nested");
    Object.entries(value).filter(([key]) => !["signed_request", "receiver_receipt", "signature", "hmac", "key"].includes(key)).forEach(([key, item]) => {
      const dd = node("dd"); dd.append(item && typeof item === "object" ? readable(item, depth + 1) : node("span", scalar(item, key)));
      list.append(node("dt", LABELS[key] || key), dd);
    });
    return list;
  }
  function table(headers, rows, caption) {
    const result = node("table");
    if (caption) result.append(node("caption", caption));
    const tr = node("tr"), thead = node("thead"), tbody = node("tbody");
    headers.forEach((value) => { const th = node("th", value); th.scope = "col"; tr.append(th); });
    thead.append(tr);
    rows.forEach((values) => { const row = node("tr"); values.forEach((value) => row.append(node("td", scalar(value)))); tbody.append(row); });
    result.append(thead, tbody); return result;
  }
  function optionList(id, rows, selected, placeholder) {
    const select = $(id), first = node("option", placeholder); first.value = "";
    select.replaceChildren(first, ...rows.map((entry) => {
      const item = node("option", `${payload(entry).snapshot_id || entry.object_id} · ${word(entry.status)} · v${entry.version}`);
      item.value = entry.object_id; return item;
    }));
    select.value = rows.some((entry) => entry.object_id === selected) ? selected : "";
  }
  const error = (code, message) => Object.assign(new Error(message), {code});
  function assertEpoch(expectedEpoch) {
    if (expectedEpoch !== epoch) throw error("session_changed", "会话已清除");
  }
  function notify(text) { $("message").textContent = text; }
  function clearError() { $("error-message").hidden = true; $("error-message").textContent = ""; }
  function showError(exc) {
    if (["session_changed", "AbortError"].includes(exc.code || exc.name)) return;
    $("error-message").textContent = `[${exc.code || "network_unknown"}] ${exc.code ? exc.message : "连接中断，动作结果可能未知。先重新读取，再使用显式恢复或独立回读；不要盲目重发。"}`;
    $("error-message").hidden = false;
  }
  async function request(path, body, expectedEpoch = epoch) {
    assertEpoch(expectedEpoch);
    const controller = new AbortController(); controllers.add(controller);
    try {
      const response = await fetch(path, {method: body === undefined ? "GET" : "POST", headers: {Authorization: `Bearer ${token}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})}, body: body === undefined ? undefined : JSON.stringify(body), credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal});
      assertEpoch(expectedEpoch);
      const result = await response.json(); assertEpoch(expectedEpoch);
      if (!response.ok) {
        if ([401, 403].includes(response.status)) { logout("身份或权限已变化，请重新连接。"); throw error("session_changed", "权限变化"); }
        throw Object.assign(error(result.error?.code || `http_${response.status}`, result.error?.message || "服务端拒绝此请求。"), {httpStatus: response.status});
      }
      return result;
    } finally { controllers.delete(controller); }
  }
  // Busy remains observable through every nested read and a full paint boundary.
  function afterPaint() {
    return new Promise((resolve) => {
      if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
      else setTimeout(resolve, 0);
    });
  }
  function launch(key, work) {
    if (inFlight.has(key)) return;
    const expectedEpoch = epoch, job = {};
    inFlight.add(key); jobs.add(job); clearError(); syncControls();
    void (async () => {
      try { await work(expectedEpoch); }
      catch (exc) { if (expectedEpoch === epoch) showError(exc); }
      finally {
        await afterPaint();
        if (expectedEpoch === epoch) { inFlight.delete(key); jobs.delete(job); syncControls(); }
      }
    })();
  }
  function bind(id, type, fn) {
    const target = $(id); target.addEventListener(type, fn); bindings.push([target, type, fn]);
  }
  function unbindSession() { bindings.splice(0).forEach(([target, type, fn]) => target.removeEventListener(type, fn)); }
  function fence() {
    epoch += 1;
    controllers.forEach((controller) => controller.abort()); controllers.clear();
    jobs.clear(); inFlight.clear(); selectionSerial += 1; sourceSerial += 1;
    unbindSession();
  }
  function resetConsents() {
    ["review-form", "publication-form", "supplement-form", "reconcile-form", "cancel-publication-form", "execute-form"].forEach((id) => $(id).reset());
    reviewBinding = publicationBinding = supplementBinding = "";
  }
  function logout(message = "已退出；内存令牌、证据与人工输入已清除。") {
    fence(); token = ""; me = null; ready = false; data = emptyData();
    snapshotId = taskId = recommendationId = publicationId = "";
    scenarios = {}; scenarioSource = ""; unknownWrites.clear(); needsReload = false; staleNotice = ""; lastReadback = null;
    resetConsents(); $("token").value = ""; $("scenario-select").value = ""; $("scenario-json").value = "";
    $("workbench").hidden = true; $("login-panel").hidden = false;
    clearError(); render(); notify(message);
  }
  function merge(kind, entry) {
    if (!entry || !own(entry)) return;
    const previous = object(kind, entry.object_id);
    if (previous && previous.version > entry.version) { staleNotice = "检测到延迟返回的旧版本，已保留较新的对象。请核对来源与当前版本。"; return; }
    if (previous && previous.version === entry.version && previous.content_hash !== entry.content_hash) {
      staleNotice = "同版本内容哈希不一致，已停止采用该响应；请重新读取核查。"; needsReload = true; return;
    }
    data[kind] = data[kind].filter((item) => item.object_id !== entry.object_id).concat(entry);
  }
  async function refresh(expectedEpoch) {
    const [identity, health, ...lists] = await Promise.all([request("/api/me", undefined, expectedEpoch), request("/health", undefined, expectedEpoch), ...KINDS.map((kind) => request(`/api/${kind}`, undefined, expectedEpoch))]);
    assertEpoch(expectedEpoch); me = identity;
    if (!Array.isArray(me.plants)) throw error("invalid_response", "身份缺少工厂范围。");
    needsReload = false;
    KINDS.forEach((kind, i) => {
      if (!Array.isArray(lists[i].items)) throw error("invalid_response", "列表结构不完整。");
      if (kind === "audit") data.audit = lists[i].items;
      else {
        const previous = data[kind]; data[kind] = [];
        lists[i].items.filter(own).forEach((item) => {
          const old = previous.find((entry) => entry.object_id === item.object_id);
          if (old) data[kind].push(old);
          merge(kind, item);
        });
      }
    });
    ready = true;
    $("provider-summary").textContent = `提供方：${health.provider || "未提供"}。TEST provider 仅验编排，不代表真实模型推理；CP-SAT、独验、合成执行分别留证。`;
    [ ["snapshots", snapshotId, () => { snapshotId = ""; }], ["tasks", taskId, () => { taskId = ""; }], ["recommendations", recommendationId, () => { recommendationId = ""; }], ["publications", publicationId, () => { publicationId = ""; }] ].forEach(([kind, id, clear]) => { if (id && !object(kind, id)) clear(); });
    linkSelected(); render();
  }
  function linkSelected() {
    const task = object("tasks", taskId);
    if (task) {
      snapshotId = payload(task).snapshot_id;
      const rec = data.recommendations.find((item) => payload(item).task_id === taskId);
      if (rec) recommendationId = rec.object_id;
    }
    const rec = object("recommendations", recommendationId);
    if (rec) {
      const pub = data.publications.find((item) => payload(item).recommendation_id === recommendationId);
      if (pub) publicationId = pub.object_id;
    }
  }
  async function loadScenarios(expectedEpoch, id = snapshotId) {
    if (!id) return;
    const serial = ++sourceSerial;
    const result = await request(`/api/snapshots/${encodeURIComponent(id)}/scenarios`, undefined, expectedEpoch);
    if (serial !== sourceSerial || id !== snapshotId) return;
    scenarios = result.items || {}; scenarioSource = id;
    $("scenario-help").textContent = `服务端提供：${Object.keys(scenarios).join(" / ") || "无"}。未提供的情景请显式输入完整 JSON；模型不能修改情景。`;
    syncControls();
  }
  function selectObject(kind, id) {
    selectionSerial += 1; const serial = selectionSerial;
    if (kind === "snapshots") {
      snapshotId = id; taskId = recommendationId = publicationId = "";
      scenarios = {}; scenarioSource = ""; $("scenario-select").value = ""; $("scenario-json").value = "";
    } else if (kind === "tasks") {
      taskId = id; recommendationId = publicationId = "";
      const task = object(kind, id); if (task) {
        snapshotId = payload(task).snapshot_id;
        $("scenario-select").value = payload(task).scenario?.kind || "";
        $("scenario-json").value = JSON.stringify(payload(task).scenario || {}, null, 2);
      }
      linkSelected();
    } else if (kind === "recommendations") {
      recommendationId = id; publicationId = "";
      const rec = object(kind, id); taskId = payload(rec).task_id || ""; snapshotId = payload(rec).snapshot_id || snapshotId;
      linkSelected();
    } else {
      publicationId = id; const pub = object(kind, id); recommendationId = payload(pub).recommendation_id || "";
      const rec = object("recommendations", recommendationId); taskId = payload(rec).task_id || ""; snapshotId = payload(rec).snapshot_id || snapshotId;
    }
    lastReadback = null; render();
    if (!id) return;
    launch(`detail:${serial}`, async (expectedEpoch) => {
      const result = await request(`/api/${kind}/${encodeURIComponent(id)}`, undefined, expectedEpoch);
      merge(kind, result);
      if (serial === selectionSerial) { linkSelected(); render(); await loadScenarios(expectedEpoch); }
    });
  }
  function strictInteger(text, max) {
    if (!/^(0|[1-9][0-9]*)$/.test(text)) return null;
    const value = Number(text); return Number.isSafeInteger(value) && value <= max ? value : null;
  }
  function authorizedScenario() {
    const kind = $("scenario-select").value, text = $("scenario-json").value;
    if (!SCENARIOS.includes(kind) || !text || text.length > 65536) throw error("scenario_input", "请先选择情景并检查完整 JSON。");
    let value;
    try { value = JSON.parse(text); } catch { throw error("scenario_json", "情景必须是有效 JSON。"); }
    if (!value || Array.isArray(value) || value.kind !== kind) throw error("scenario_kind", "JSON kind 必须与人工选择完全一致。");
    let count = 0;
    const inspect = (entry, depth) => {
      count += 1;
      if (depth > 16 || count > 5000) throw error("scenario_limit", "情景输入超过结构限制。");
      if (typeof entry === "number" && (!Number.isSafeInteger(entry) || entry < 0)) throw error("scenario_integer", "领域数量与分钟必须为非负安全整数。");
      if (entry && typeof entry === "object") Object.values(entry).forEach((item) => inspect(item, depth + 1));
    };
    inspect(value, 0); return value;
  }
  function staleSource(entry) {
    const binding = payload(entry).source_binding;
    if (!binding) return false;
    const source = object("snapshots", binding.object_id);
    return !source || source.version !== binding.version || source.content_hash !== binding.content_hash;
  }
  function scopedReady(obj) { return ready && !needsReload && !detailBusy() && own(obj); }
  function detailBusy() { return [...inFlight].some((key) => key.startsWith("detail:")); }
  function writeBusy() { return [...inFlight].some((key) => key.startsWith("write:")); }
  function canRun(task) {
    if (!scopedReady(task) || staleSource(task) || inFlight.has(`run:${task?.object_id}`) || needsReload) return false;
    return task.status === "running" || (task.status === "cancelled" && payload(task).unknown_calls > 0) || (["created", "blocked"].includes(task.status) && !(payload(task).unknown_calls > 0));
  }
  function canReconcile(task) { return scopedReady(task) && me.role === "admin" && ["running", "blocked", "cancelled"].includes(task.status) && payload(task).unknown_calls > 0; }
  function validNote(id) { return $(id).value.trim().length > 0 && $(id).value.length <= 2000; }
  function supplementAnswers() {
    return (payload(object("tasks", taskId)).awaiting_fields || []).map((field, i) => ({field, value: strictInteger($(`answer-${i}`).value, field.endsWith(".due_min") ? 1440 : 1000000000000)}));
  }
  function syncControls() {
    const busy = jobs.size > 0, locked = !ready || needsReload || writeBusy() || detailBusy();
    $("workbench").setAttribute("aria-busy", String(busy));
    $("connect-button").disabled = inFlight.has("login");
    $("reload-button").disabled = !token || inFlight.has("refresh");
    const task = object("tasks", taskId), rec = object("recommendations", recommendationId), pub = object("publications", publicationId), pp = payload(pub);
    ["snapshot-select", "scenario-select", "scenario-json", "task-select", "recommendation-select", "publication-select"].forEach((id) => { $(id).disabled = !ready || writeBusy(); });
    $("create-task").disabled = locked || !own(object("snapshots", snapshotId)) || !SCENARIOS.includes($("scenario-select").value) || !$("scenario-json").value.trim();
    $("run-task").disabled = !canRun(task) || writeBusy();
    $("run-task").textContent = task?.status === "cancelled" && payload(task).unknown_calls > 0 ? "核验已取消任务的遗留调用（不重启）" : task?.status === "running" ? "显式恢复 running（后端核验租约）" : "显式运行任务";
    $("cancel-task").disabled = !scopedReady(task) || !["created", "running", "awaiting_input", "blocked"].includes(task?.status) || inFlight.has(`cancel:${taskId}`);
    const missing = scopedReady(task) && task.status === "awaiting_input" && !staleSource(task);
    $("supplement-form").hidden = !missing;
    $("supplement-note").disabled = !missing || locked;
    const answers = missing ? supplementAnswers() : [];
    answers.forEach((_, i) => { $(`answer-${i}`).disabled = locked; });
    $("supplement-submit").disabled = locked || !missing || !answers.length || answers.some((item) => item.value === null) || !validNote("supplement-note");
    const reconcile = canReconcile(task);
    $("reconcile-form").hidden = !reconcile;
    ["reconcile-call", "reconcile-tokens", "reconcile-note", "reconcile-confirm"].forEach((id) => { $(id).disabled = !reconcile || locked; });
    $("reconcile-submit").disabled = !reconcile || locked || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/.test($("reconcile-call").value) || !validNote("reconcile-note") || !$("reconcile-confirm").checked || ($("reconcile-tokens").value !== "" && strictInteger($("reconcile-tokens").value, 100000) === null);
    const reviewing = scopedReady(rec) && !staleSource(rec) && planIsBound(payload(rec)) && rec.status === "pending_review" && ["reviewer", "admin"].includes(me.role);
    ["review-decision", "review-note"].forEach((id) => { $(id).disabled = locked || !reviewing; });
    $("review-submit").disabled = locked || !reviewing || !["approved", "rejected"].includes($("review-decision").value) || !validNote("review-note");
    const publishing = scopedReady(rec) && !staleSource(rec) && planIsBound(payload(rec)) && rec.status === "approved" && !data.publications.some((item) => payload(item).recommendation_id === recommendationId) && !unknownWrites.has(`publication:${recommendationId}`);
    $("publication-confirm").disabled = locked || !publishing;
    $("request-publication").disabled = locked || !publishing || !$("publication-confirm").checked;
    const receiverReady = scopedReady(pub) && !locked;
    $("deliver-publication").disabled = !receiverReady || staleSource(pub) || pp.delivery_status !== "not_sent" || pp.receiver_status !== "none" || unknownWrites.has(`deliver:${publicationId}`);
    $("advance-publication").disabled = !receiverReady || staleSource(pub) || pp.delivery_status !== "acknowledged" || pp.receiver_status !== "partial" || pp.execution_status !== "none" || unknownWrites.has(`advance:${publicationId}`);
    $("readback-publication").disabled = !receiverReady;
    const cancellable = receiverReady && pp.execution_status === "none" && pp.receiver_status !== "cancelled";
    $("cancel-publication-note").disabled = !cancellable;
    $("cancel-publication").disabled = !cancellable || !validNote("cancel-publication-note");
    const executable = receiverReady && !staleSource(pub) && pp.delivery_status === "acknowledged" && pp.receiver_status === "accepted" && pp.execution_status === "none" && !unknownWrites.has(`execute:${publicationId}`);
    $("execute-confirm").disabled = !executable;
    $("execute-publication").disabled = !executable || !$("execute-confirm").checked;
  }
  function renderSnapshot() {
    const entry = object("snapshots", snapshotId), p = payload(entry);
    if (!entry) { empty("snapshot-summary", "请选择服务端快照。不会自动选择最新来源。"); empty("snapshot-detail"); return; }
    put("snapshot-summary", pairs([["object_id", entry.object_id], ["owner", entry.owner], ["plant_id", entry.plant_id], ["version", entry.version], ["revision", p.revision], ["observed_at_ms", p.observed_at_ms], ["horizon_min", p.horizon_min], ["freeze_until_min", p.freeze_until_min]]));
    put("snapshot-detail", readable(p));
  }
  function renderTask() {
    const task = object("tasks", taskId), p = payload(task), evidence = p.evidence || {};
    if (!task) { empty("task-summary", "请选择任务或显式创建任务。"); empty("task-evidence", "尚无已签发分析证据。"); }
    else {
      put("task-summary", pairs([["object_id", task.object_id], ["status", task.status], ["version", task.version], ...["calls", "tools", "tokens", "unknown_calls", "elapsed_s", "error", "lease_id", "lease_until_ms", "provider_fingerprint"].map((key) => [key, p[key]]), ["已冻结的授权情景", p.scenario?.kind || "未提供"], ["人工补问（仅文本）", p.question]]));
      if (Object.keys(evidence).length) put("task-evidence", readable(evidence)); else empty("task-evidence", "尚无已签发分析证据。");
    }
    put("tool-chain", ...TOOLS.map((tool, i) => {
      const done = !!evidence[tool] || (tool === "submit_schedule" && task?.status === "submitted") || (tool === "request_human_input" && task?.status === "awaiting_input");
      const li = node("li", `${String(i + 1).padStart(2, "0")} ${word(tool)} · ${done ? "已记录" : "未记录 / 按需"}`, done ? "done" : "");
      li.append(node("small", tool)); li.dataset.tool = tool; return li;
    }));
    const solve = payload(evidence.solve_schedule);
    $("solver-summary").textContent = solve.baseline || solve.optimized ? `可行基线：${word(solve.baseline?.status || "未提供")}；优化：${word(solve.optimized?.status || "未提供")}。INFEASIBLE 是已证明无解；UNKNOWN 不是无解证明。` : "尚无求解结果。INFEASIBLE（已证明无解）与 UNKNOWN（尚无候选）严格区分。";
    const binding = `${task?.object_id || ""}:${task?.content_hash || ""}:${(p.awaiting_fields || []).join(",")}`;
    if (binding !== supplementBinding) {
      supplementBinding = binding; $("supplement-note").value = "";
      put("supplement-fields", ...(p.awaiting_fields || []).map((field, i) => {
        const wrapper = node("div"), label = node("label", field), input = node("input");
        label.setAttribute("for", `answer-${i}`); input.id = `answer-${i}`; input.dataset.field = field;
        input.setAttribute("inputmode", "numeric"); input.setAttribute("maxlength", "13"); input.setAttribute("required", "");
        input.addEventListener("input", syncControls); wrapper.append(label, input); return wrapper;
      }));
    }
  }
  function svg(tag, attributes = {}, text) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => el.setAttribute(name, String(value)));
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  const xAt = (minute, horizon) => 140 + 810 * minute / horizon;
  function chart(title, height, horizon) {
    const root = svg("svg", {viewBox: `0 0 1000 ${height}`, class: "chart", role: "img", "aria-label": title});
    root.append(svg("title", {}, title));
    for (let i = 0; i <= 6; i += 1) {
      const minute = Math.round(horizon * i / 6), x = xAt(minute, horizon);
      root.append(svg("line", {x1: x, y1: 25, x2: x, y2: height - 27, class: "grid-line"}), svg("text", {x, y: height - 9, "text-anchor": "middle"}, minute));
    }
    return root;
  }
  function drawGantt(problem, plan) {
    const snapshot = problem.snapshot, horizon = snapshot.horizon_min;
    const machines = snapshot.machines || [], root = chart("当前推荐已独验计划 · 机器换型与加工甘特图", 60 + machines.length * 55, horizon);
    machines.forEach((machine, i) => {
      const y = 32 + 55 * i;
      root.append(svg("text", {x: 12, y: y + 20, class: "chart-title"}, machine.machine_id));
      plan.slots.filter((slot) => slot.machine_id === machine.machine_id).forEach((slot) => {
        for (const [kind, start, end] of [["setup", slot.setup_start_min, slot.start_min], ["processing", slot.start_min, slot.end_min]]) {
          if (start === end) continue;
          const rect = svg("rect", {x: xAt(start, horizon), y, width: 810 * (end - start) / horizon, height: 30, rx: 3, class: kind});
          rect.append(svg("title", {}, `${machine.machine_id} / ${slot.operation_id} ${word(kind)} [${start}, ${end}) 分钟`)); root.append(rect);
          if (kind === "processing" && end - start > horizon / 13) root.append(svg("text", {x: xAt(start, horizon) + 4, y: y + 19, class: "bar-label"}, slot.operation_id));
        }
      });
    });
    put("gantt-chart", root);
    put("plan-slots", table(["工序", "机器", "换型开始", "加工开始", "加工结束", "机器前驱"], plan.slots.map((s) => [s.operation_id, s.machine_id, s.setup_start_min, s.start_min, s.end_min, s.predecessor_id]), "来自当前推荐 payload.candidate.slots，非模型文本"));
  }
  function occupancy(activities, resource) {
    const changes = new Map([[0, 0]]);
    activities.filter((a) => a.resource_kind === "resource" && a.resource_id === resource.resource_id).forEach((a) => {
      changes.set(a.start_min, (changes.get(a.start_min) || 0) + a.demand);
      changes.set(a.end_min, (changes.get(a.end_min) || 0) - a.demand);
    });
    let balance = 0;
    return [...changes.entries()].sort((a, b) => a[0] - b[0]).map(([at_min, change]) => { balance += change; return {at_min, balance}; });
  }
  function stepPath(points, horizon, yAt, initial) {
    let balance = initial, path = `M ${xAt(0, horizon)} ${yAt(balance)}`;
    points.forEach((point) => { path += ` H ${xAt(point.at_min, horizon)} V ${yAt(point.balance)}`; balance = point.balance; });
    return `${path} H ${xAt(horizon, horizon)}`;
  }
  function drawResources(problem, report) {
    const {resources, horizon_min: horizon} = problem.snapshot;
    if (!resources.length) { empty("resource-chart", "此来源未定义辅助资源；不构造占用数据。"); empty("resource-table"); return; }
    const root = chart("独验辅助资源实际累计占用与容量", 65 + resources.length * 100, horizon);
    resources.forEach((resource, i) => {
      const points = occupancy(report.activities, resource), y = 28 + 100 * i;
      const max = Math.max(resource.capacity, ...points.map((p) => p.balance), 1), yAt = (value) => y + 65 - 55 * value / max;
      root.append(svg("text", {x: 12, y: y + 20, class: "chart-title"}, resource.resource_id), svg("text", {x: 12, y: y + 40}, `容量 ${resource.capacity}`), svg("line", {x1: 140, x2: 950, y1: yAt(resource.capacity), y2: yAt(resource.capacity), class: "capacity"}), svg("path", {d: stepPath(points, horizon, yAt, 0), class: "trace"}));
      points.forEach((point) => { const dot = svg("circle", {cx: xAt(point.at_min, horizon), cy: yAt(point.balance), r: 2, class: "point"}); dot.append(svg("title", {}, `${resource.resource_id} / ${point.at_min} 分钟 / 占用 ${point.balance}`)); root.append(dot); });
    });
    put("resource-chart", root);
    put("resource-table", table(["资源", "工序", "阶段", "开始", "结束", "需求量"], report.activities.filter((a) => a.resource_kind === "resource").map((a) => [a.resource_id, a.operation_id, a.kind, a.start_min, a.end_min, a.demand])));
  }
  function drawInventory(problem, report) {
    const {materials, items, horizon_min: horizon} = problem.snapshot;
    const root = chart("独验逐物料库存事件阶梯，不是期末库存插值", 65 + materials.length * 100, horizon);
    materials.forEach((material, i) => {
      const points = report.inventory.filter((p) => p.item_id === material.item_id), y = 25 + 100 * i;
      const max = Math.max(material.initial_stock, ...points.map((p) => p.balance), 1), min = Math.min(0, ...points.map((p) => p.balance));
      const yAt = (value) => y + 67 - 54 * (value - min) / (max - min);
      const unit = items.find((item) => item.item_id === material.item_id)?.unit || "单位未提供";
      root.append(svg("text", {x: 12, y: y + 20, class: "chart-title"}, material.item_id), svg("text", {x: 12, y: y + 40}, `${min}…${max} ${unit}`), svg("path", {d: stepPath(points, horizon, yAt, material.initial_stock), class: "trace"}));
      points.forEach((point) => { const dot = svg("circle", {cx: xAt(point.at_min, horizon), cy: yAt(point.balance), r: 2.5, class: "point"}); dot.append(svg("title", {}, `${point.item_id} @${point.at_min}分钟：收料 ${point.received} / 扣料 ${point.consumed} / 余额 ${point.balance} ${unit}`)); root.append(dot); });
    });
    put("inventory-chart", root);
    put("inventory-table", table(["物料", "分钟", "实际收料", "实际扣料", "余额"], report.inventory.map((p) => [p.item_id, p.at_min, p.received, p.consumed, p.balance]), "独立验证器原始库存点，保留同刻事件与单位"));
  }
  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    return JSON.stringify(value);
  }
  function simulationView(value) {
    if (!value) return node("p", "尚无合成记录；不推断执行结果。", "muted");
    const wrapper = node("div");
    wrapper.append(pairs([["synthetic", value.synthetic], ["status", value.status], ["plan_hash", value.plan_hash], ["error", value.error || value.blocked_reason?.code || null]]));
    if (value.metrics) wrapper.append(readable(value.metrics));
    const detail = node("details"); detail.append(node("summary", "展开持久化模拟事件、库存与资源明细"), readable(value)); wrapper.append(detail);
    return wrapper;
  }
  function planIsBound(p) {
    const plan = p.candidate, report = p.validation, problem = p.compiled_problem;
    if (!plan || !report || report.valid !== true || !problem?.snapshot || !Array.isArray(plan.slots) || !Array.isArray(report.inventory) || !Array.isArray(report.activities)) return false;
    const comparisonBound = p.comparison?.selected_plan && canonical(p.comparison.selected_plan) === canonical(plan);
    return comparisonBound && report.violations?.length === 0 && canonical(report.objective) === canonical(plan.objective) && ["snapshot_hash", "problem_hash", "objective_hash"].every((key) => plan[key] === problem[key] && report[key] === problem[key]) && Number.isSafeInteger(problem.snapshot.horizon_min) && problem.snapshot.horizon_min > 0;
  }
  function renderRecommendation() {
    const rec = object("recommendations", recommendationId), p = payload(rec);
    const binding = `${rec?.object_id || ""}:${rec?.version || ""}:${rec?.content_hash || ""}`;
    if (binding !== reviewBinding) { reviewBinding = binding; $("review-form").reset(); $("publication-form").reset(); }
    const targets = ["comparison-summary", "objective-comparison", "gantt-chart", "plan-slots", "resource-chart", "resource-table", "inventory-chart", "inventory-table", "simulation-summary"];
    $("model-draft").textContent = p.untrusted_model_draft || "尚无模型说明。";
    $("plan-binding").textContent = "";
    if (!rec) { empty("recommendation-summary", "尚未选择推荐；不从其他任务拼接图表。"); targets.forEach((id) => empty(id)); return; }
    put("recommendation-summary", pairs([["object_id", rec.object_id], ["status", rec.status], ["version", rec.version], ["content_hash", rec.content_hash], ["observed_at_ms", p.source_binding?.observed_at_ms], ["scenario", word(p.scenario?.kind || "未提供")]]));
    if (!planIsBound(p)) { targets.forEach((id) => empty(id, "当前推荐缺少同源已独验 Plan；不绘制候选或捏造指标。")); return; }
    const comparison = p.comparison, weights = p.compiled_problem.snapshot.objective;
    if (comparison?.selected_plan && ["snapshot_hash", "problem_hash", "objective_hash"].every((key) => comparison.selected_plan[key] === p.candidate[key])) {
      put("comparison-summary", node("p", `同一个 compiled problem、snapshot hash 与目标权重。服务端实际选择：${word(comparison.selected)}；总目标改善（基线 − 所选）：${scalar(comparison.improvement)}。平局选择基线，不保证优化一定更好。`, "notice"));
      put("objective-comparison", table(["独验目标分项", "权重", "可行基线", "优化候选"], ["weighted_tardiness", "setup_minutes", "makespan", "load_spread", "total"].map((key) => [LABELS[key], key === "total" ? "加权求和" : weights[key], comparison.baseline_metrics?.[key], comparison.optimized_metrics?.[key]]), "仅比较同源候选；缺少的候选指标显示未提供，不填零"));
    } else { empty("comparison-summary", "没有完整同源 compare report；不能宣称改善。"); empty("objective-comparison"); }
    $("plan-binding").textContent = `所选 ${p.candidate.plan_id} / 独验 plan_hash ${p.validation.plan_hash} / problem_hash ${p.candidate.problem_hash}`;
    drawGantt(p.compiled_problem, p.candidate); drawResources(p.compiled_problem, p.validation); drawInventory(p.compiled_problem, p.validation);
    put("simulation-summary", simulationView(p.simulation));
  }
  function renderPublication() {
    const rec = object("recommendations", recommendationId), pub = object("publications", publicationId), p = payload(pub);
    const binding = `${pub?.object_id || ""}:${pub?.version || ""}:${pub?.content_hash || ""}`;
    if (binding !== publicationBinding) { publicationBinding = binding; $("execute-form").reset(); $("cancel-publication-form").reset(); }
    const stages = [ ["提出", !!rec], ["批准", rec?.status === "approved"], ["发布请求", !!pub], ["完整接收", p.receiver_status === "accepted"], ["合成执行完成", p.execution_status === "completed"] ];
    put("workflow-stages", ...stages.map(([name, done]) => node("li", `${name} · ${done ? "有记录" : "未确认"}`, done ? "done" : "")));
    if (!pub) { empty("publication-summary", "尚无发布对象；批准不等于已发布。"); empty("readback-summary", "尚未显式独立回读。"); empty("execution-summary", "尚无合成执行记录，不以模拟送审证据代替。"); return; }
    put("publication-summary", pairs([["object_id", pub.object_id], ["status", pub.status], ["version", pub.version], ...["delivery_status", "receiver_status", "execution_status", "receiver_sequence", "recommendation_hash", "plan_hash"].map((key) => [key, p[key]])]), node("p", `累计接收工序：${(p.accepted_operation_ids || []).join("、") || "无"}。`, "muted"));
    put("readback-summary", node("p", lastReadback?.id === publicationId ? `本页已独立请求回读；后端返回版本 v${lastReadback.version}。未发现新回执也不代表从未发送。权威接收/执行状态见上方。` : "当前是发布对象状态，不冒充本页执行过独立回读。请单独点击回读。", "muted"));
    if (p.execution_result) put("execution-summary", simulationView(p.execution_result));
    else empty("execution-summary", p.execution_status === "running" ? "合成执行 running，尚无最终事件。不得自动重跑；请独立回读并人工核查。" : "尚无合成执行结果；部分/完整接收都不等于执行完成。");
  }
  function render() {
    $("identity").textContent = me ? `${me.user_id} / ${word(me.role)} / 工厂 ${me.plants.join("、")} / ACL v${me.acl_version}` : "";
    optionList("snapshot-select", data.snapshots, snapshotId, "请选择快照");
    optionList("task-select", data.tasks, taskId, "请选择任务");
    optionList("recommendation-select", data.recommendations, recommendationId, "请选择推荐");
    optionList("publication-select", data.publications, publicationId, "请选择发布对象");
    renderSnapshot(); renderTask(); renderRecommendation(); renderPublication();
    const stale = [object("tasks", taskId), object("recommendations", recommendationId), object("publications", publicationId)].some((obj) => obj && staleSource(obj));
    $("stale-banner").hidden = !stale && !staleNotice && !needsReload;
    $("stale-banner").textContent = `${stale ? "来源版本或内容哈希已变化；旧任务/推荐仍可查看，但禁止继续求解、批准、发布和执行。请以当前快照重新创建任务。 " : ""}${staleNotice}${needsReload ? " 动作结果未完成确认，请先重新读取；页面不会自动重试。" : ""}`;
    if (data.audit.length) put("audit-list", readable(data.audit)); else empty("audit-list", "当前 owner + plant 暂无可见审计记录。");
    syncControls();
  }
  function mutate(key, path, body, select, success, options = {}) {
    launch(key, async (expectedEpoch) => {
      if (options.unknownKey) unknownWrites.add(options.unknownKey);
      let accepted = false;
      try {
        const result = await request(path, body, expectedEpoch); accepted = true;
        const kind = objectKinds[result.kind]; if (kind) merge(kind, result);
        if (select) select(result);
        if (options.unknownKey) unknownWrites.delete(options.unknownKey);
        if (options.readback) lastReadback = {id: result.object_id, version: result.version};
        render(); await refresh(expectedEpoch); notify(success);
      } catch (exc) {
        if (expectedEpoch === epoch) {
          if (!exc.code || accepted || exc.httpStatus >= 500 || options.unknownKey) needsReload = true;
          // Delivery may commit an intent or receiver state before an error response.
          // Uncertain mutations stay locked until authoritative reads; no blind retry.
          if (exc.code && exc.httpStatus < 500 && !accepted && options.unknownKey && !["deliver", "advance", "execute"].some((prefix) => options.unknownKey.startsWith(`${prefix}:`))) unknownWrites.delete(options.unknownKey);
          $("publication-confirm").checked = false; $("execute-confirm").checked = false;
          render();
        }
        throw exc;
      }
    });
  }
  function installSession() {
    unbindSession();
    [["snapshot-select", "snapshots"], ["task-select", "tasks"], ["recommendation-select", "recommendations"], ["publication-select", "publications"]].forEach(([id, kind]) => bind(id, "change", () => selectObject(kind, $(id).value)));
    bind("scenario-select", "change", () => {
      const kind = $("scenario-select").value;
      $("scenario-json").value = scenarioSource === snapshotId && scenarios[kind] ? JSON.stringify(scenarios[kind], null, 2) : "";
      syncControls();
    });
    ["scenario-json", "supplement-note", "review-note", "reconcile-call", "reconcile-tokens", "reconcile-note", "cancel-publication-note"].forEach((id) => bind(id, "input", syncControls));
    ["review-decision", "publication-confirm", "execute-confirm", "reconcile-confirm"].forEach((id) => bind(id, "change", syncControls));
    bind("create-task", "click", () => {
      if ($("create-task").disabled) return;
      let scenario; try { scenario = authorizedScenario(); } catch (exc) { showError(exc); return; }
      mutate("write:create", "/api/tasks", {snapshot_id: snapshotId, scenario, action_id: crypto.randomUUID()}, (obj) => { taskId = obj.object_id; recommendationId = publicationId = ""; }, "任务已创建。请另行点击运行；模型不能改变已授权情景。");
    });
    bind("run-task", "click", () => {
      if ($("run-task").disabled) return;
      const id = taskId;
      mutate(`run:${id}`, `/api/tasks/${encodeURIComponent(id)}/run`, {}, null, "本次运行请求已返回。请按状态核查；未自动批准或执行。");
    });
    bind("cancel-task", "click", () => {
      if ($("cancel-task").disabled) return;
      const id = taskId;
      mutate(`cancel:${id}`, `/api/tasks/${encodeURIComponent(id)}/cancel`, {action_id: crypto.randomUUID()}, null, "取消请求已完成。迟到模型用量仍须记账，未知记录不会被当作零。");
    });
    bind("supplement-form", "submit", (event) => {
      event.preventDefault(); if ($("supplement-submit").disabled) return;
      mutate("write:resume", `/api/tasks/${encodeURIComponent(taskId)}/resume`, {action_id: crypto.randomUUID(), answers: supplementAnswers(), note: $("supplement-note").value.trim()}, null, "缺失整数已补齐，源观察时间保持不变。请单独运行任务。");
    });
    bind("reconcile-form", "submit", (event) => {
      event.preventDefault(); if ($("reconcile-submit").disabled) return;
      mutate("write:reconcile", `/api/tasks/${encodeURIComponent(taskId)}/reconcile`, {action_id: crypto.randomUUID(), call_id: $("reconcile-call").value, tokens: $("reconcile-tokens").value === "" ? null : strictInteger($("reconcile-tokens").value, 100000), note: $("reconcile-note").value.trim(), no_result: true}, () => $("reconcile-form").reset(), "本次对账已返回；没有自动再次调用模型。取消任务保持取消。");
    });
    bind("review-form", "submit", (event) => {
      event.preventDefault(); if ($("review-submit").disabled) return;
      const rec = object("recommendations", recommendationId);
      mutate("write:review", `/api/recommendations/${encodeURIComponent(recommendationId)}/review`, {action_id: crypto.randomUUID(), recommendation_hash: rec.content_hash, decision: $("review-decision").value, note: $("review-note").value.trim()}, null, "审阅决定已记录；没有自动创建发布请求。");
    });
    bind("publication-form", "submit", (event) => {
      event.preventDefault(); if ($("request-publication").disabled) return;
      const id = recommendationId, rec = object("recommendations", id);
      mutate("write:publish", `/api/recommendations/${encodeURIComponent(id)}/request-publication`, {action_id: crypto.randomUUID(), recommendation_hash: rec.content_hash, confirmed: true}, (obj) => { publicationId = obj.object_id; }, "发布请求已冻结，尚未递送。请独立操作首次本地递送。", {unknownKey: `publication:${id}`});
    });
    [["deliver-publication", "deliver-local", "deliver", "首次本地递送已返回；多工序通常仅部分接收。"], ["advance-publication", "advance-receiver", "advance", "剩余接收请求已返回；接收不代表执行。"], ["readback-publication", "readback", "readback", "已独立查询接收器；没有重新递送或执行。"]].forEach(([id, action, key, success]) => bind(id, "click", () => {
      if ($(id).disabled) return;
      mutate(`write:${key}`, `/api/publications/${encodeURIComponent(publicationId)}/${action}`, {action_id: crypto.randomUUID()}, null, success, key === "readback" ? {readback: true} : {unknownKey: `${key}:${publicationId}`});
    }));
    bind("cancel-publication-form", "submit", (event) => {
      event.preventDefault(); if ($("cancel-publication").disabled) return;
      mutate("write:cancel-publication", `/api/publications/${encodeURIComponent(publicationId)}/cancel`, {action_id: crypto.randomUUID(), note: $("cancel-publication-note").value.trim()}, null, "权威撤销请求已返回；已执行历史不能撤销。");
    });
    bind("execute-form", "submit", (event) => {
      event.preventDefault(); if ($("execute-publication").disabled) return;
      mutate("write:execute", `/api/publications/${encodeURIComponent(publicationId)}/execute-synthetic`, {action_id: crypto.randomUUID(), confirmed: true}, null, "合成执行请求已返回，请核查最终状态与事件；这不证明现场收益。", {unknownKey: `execute:${publicationId}`});
    });
  }
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault(); if ($("connect-button").disabled) return;
    const entered = $("token").value.trim(); $("token").value = "";
    if (!entered) return;
    logout(""); token = entered;
    launch("login", async (expectedEpoch) => {
      try {
        await refresh(expectedEpoch); assertEpoch(expectedEpoch); installSession();
        $("workbench").hidden = false; $("login-panel").hidden = true; notify("已连接。请选择来源与授权情景；所有领域动作都需显式点击。");
      } catch (exc) {
        if (expectedEpoch === epoch) { logout("连接失败，令牌已清除。请核查本地实例与身份。"); showError(exc); }
      }
    });
  });
  $("logout-button").addEventListener("click", () => logout());
  $("reload-button").addEventListener("click", () => {
    if ($("reload-button").disabled) return;
    fence(); ready = false; resetConsents(); staleNotice = "";
    launch("refresh", async (expectedEpoch) => {
      await refresh(expectedEpoch); assertEpoch(expectedEpoch); installSession();
      if (snapshotId) await loadScenarios(expectedEpoch);
      notify("已重新读取。中断请求不代表后端取消；running 请显式恢复，unknown 递送请独立回读。没有自动执行领域动作。");
    });
  });
  window.addEventListener("pagehide", () => logout("已离开页面，内存会话已清除。"));
  render();
})();
