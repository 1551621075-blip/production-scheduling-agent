"""Inert native-DOM/UI contract checks; no browser, server, profile or network."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src/scheduling_agent/templates/index.html"
JS = ROOT / "src/scheduling_agent/static/app.js"
CSS = ROOT / "src/scheduling_agent/static/style.css"
NODE = shutil.which("node") or "node"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class WorkbenchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.elements, self.errors = [], [], []
        self.ids, self.options = {}, {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        form = next((e for e in reversed(self.stack) if e["tag"] == "form"), None)
        item = {"tag": tag, "attrs": attrs, "form": form["attrs"].get("id") if form else None}
        self.elements.append(item)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                self.errors.append(f"duplicate ID {attrs['id']}")
            self.ids[attrs["id"]] = item
        if tag == "option":
            if not self.stack or self.stack[-1]["tag"] != "select":
                self.errors.append("option must have an opening select")
            else:
                self.options.setdefault(self.stack[-1]["attrs"]["id"], []).append(item)
        if tag not in VOID:
            self.stack.append(item)

    def handle_endtag(self, tag):
        if tag in VOID or not self.stack or self.stack[-1]["tag"] != tag:
            self.errors.append(f"unmatched closing {tag}")
        else:
            self.stack.pop()


@pytest.fixture
def document():
    parser = WorkbenchParser()
    parser.feed(HTML.read_text())
    parser.close()
    assert not parser.errors, parser.errors
    assert not parser.stack, parser.stack
    return parser


def test_local_accessible_markup_and_stable_selectors(document):
    assert '<html lang="zh-CN">' in HTML.read_text()
    assert "NOT production" in HTML.read_text() and "绝非生产系统" in HTML.read_text()
    assert document.ids["workbench"]["attrs"]["aria-busy"] == "false"
    assert document.ids["error-message"]["attrs"]["role"] == "alert"
    assert document.ids["message"]["attrs"]["aria-live"] == "polite"
    assert document.ids["token"]["attrs"]["type"] == "password"
    assert "name" not in document.ids["token"]["attrs"]
    assert '[hidden] { display: none !important; }' in CSS.read_text()
    assert "@media" in CSS.read_text()
    for item in document.elements:
        attrs = item["attrs"]
        assert not any(name.lower().startswith("on") for name in attrs)
        assert "style" not in attrs
        if item["tag"] in {"script", "link"}:
            assert (attrs.get("src") or attrs.get("href")).startswith("/static/")
        if item["tag"] == "script":
            assert "defer" in attrs
        if item["tag"] == "label" and "for" in attrs:
            assert attrs["for"] in document.ids
        if item["tag"] == "a" and attrs.get("href", "").startswith("#"):
            assert attrs["href"][1:] in document.ids
    refs = set(re.findall(r'\$\("([a-z][a-z0-9-]*)"\)', JS.read_text()))
    assert refs <= document.ids.keys()


def test_explicit_scenario_decisions_and_unchecked_confirmations(document):
    for options in document.options.values():
        assert options[0]["attrs"]["value"] == ""
    assert [e["attrs"]["value"] for e in document.options["scenario-select"]] == [
        "", "baseline", "insert_order", "machine_outage", "supply_delay",
    ]
    assert [e["attrs"]["value"] for e in document.options["review-decision"]] == [
        "", "approved", "rejected",
    ]
    for identifier in ("publication-confirm", "execute-confirm", "reconcile-confirm"):
        attrs = document.ids[identifier]["attrs"]
        assert attrs["type"] == "checkbox" and "checked" not in attrs and "required" in attrs
    for identifier in ("review-note", "supplement-note", "reconcile-note", "cancel-publication-note"):
        attrs = document.ids[identifier]["attrs"]
        assert attrs["maxlength"] == "2000" and "required" in attrs


def test_script_is_csp_safe_memory_only_and_syntactically_valid():
    script = JS.read_text()
    for forbidden in (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
        "new Function", "localStorage", "sessionStorage", "indexedDB", "document.cookie",
        "sendBeacon", "WebSocket", "XMLHttpRequest", "console.", "location.",
    ):
        assert forbidden not in script
    assert 'credentials: "omit"' in script and 'cache: "no-store"' in script
    assert 'redirect: "error"' in script and "crypto.randomUUID()" in script
    assert "new AbortController()" in script and "expectedEpoch !== epoch" in script
    assert "epoch += 1" in script and "removeEventListener" in script
    assert 'window.addEventListener("pagehide"' in script
    assert "requestAnimationFrame" in script and "createElementNS" in script
    without_svg_namespace = script.replace("http://www.w3.org/2000/svg", "")
    assert not re.search(r"https?://|@import", HTML.read_text() + CSS.read_text() + without_svg_namespace)
    subprocess.run([NODE, "--check", str(JS)], check=True, capture_output=True, text=True)


# In-memory DOM and HTTP doubles execute the actual app.js. They deliberately
# ignore abort signals for delayed replies to verify generation fences as well.
DOM_HARNESS = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
const crypto = require("node:crypto");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const ids = new Map();
class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {...attrs}; this.children = [];
    this.listeners = {}; this.dataset = {}; this.className = ""; this._text = "";
    this.id = attrs.id || ""; this.value = attrs.value || "";
    this.hidden = "hidden" in attrs; this.disabled = "disabled" in attrs;
    this.required = "required" in attrs; this.checked = "checked" in attrs;
  }
  set id(value) { this._id = value; if(value) ids.set(value, this); }
  get id() { return this._id; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent || "").join(" "); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  removeEventListener(name, callback) { this.listeners[name] = (this.listeners[name] || []).filter(x => x !== callback); }
  reset() {
    for (const entry of input.elements.filter(e => e.form === this.id)) {
      const target = ids.get(entry.attrs.id);
      if (target) {target.value = entry.attrs.value || ""; target.checked = "checked" in entry.attrs;}
    }
  }
}
for (const e of input.elements) if(e.attrs.id) new Element(e.tag, e.attrs);
const $ = id => {assert(ids.has(id), `Unknown DOM id ${id}`); return ids.get(id);};
const clone = value => JSON.parse(JSON.stringify(value));
const digest = value => crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");
const requests = [], windowListeners = {}, frameQueue = [];
let holdFrames = false, intercept = null, role = "admin", serverNow = Date.now(), failDeliver = false;
let unknownRun = false, scenarioCounter = 0;
function obj(kind, id, status, p, version = 1, owner = "synthetic-admin") {
  return {kind, object_id: id, status, payload: p, version, owner, plant_id: "synthetic-plant", content_hash: digest([id,p,version,status])};
}
const scenarioItems = {baseline: {kind: "baseline"}, machine_outage: {kind: "machine_outage", machine_id: "M1", start_min: 22, end_min: 30}, supply_delay: {kind: "supply_delay", supply_id: "SUP", at_min: 20}, insert_order: {kind: "insert_order", lot: {lot_id: "EXTRA", quantity: 1}, order: {order_id: "EXTRA-ORDER", quantity: 1}}};
const snapshotPayload = id => ({snapshot_id: id, plant_id:"synthetic-plant", revision:1, observed_at_ms:serverNow, horizon_min:60, freeze_until_min:0,
  materials:[{item_id:"RAW", initial_stock:6}], items:[{item_id:"RAW",unit:"件"}],
  machines:[{machine_id:"M1"},{machine_id:"M2"}], resources:[{resource_id:"CREW",capacity:2}],
  orders:[{order_id:"ORDER",due_min:30}], objective:{weighted_tardiness:10,setup_minutes:2,makespan:1,load_spread:1}});
const state = {snapshots:[obj("snapshot","src-a","ready",snapshotPayload("normal")),obj("snapshot","src-b","ready",snapshotPayload("missing"))], tasks:[], recommendations:[], publications:[], audit:[]};
state.snapshots[1].payload.materials[0].initial_stock = null; state.snapshots[1].payload.orders[0].due_min = null;
state.snapshots[1].content_hash = digest(state.snapshots[1].payload);
function binding(source) {return {object_id:source.object_id,version:source.version,content_hash:source.content_hash,revision:source.payload.revision, observed_at_ms:source.payload.observed_at_ms, owner:source.owner,plant_id:source.plant_id};}
function task(id, sourceId="src-a", status="created", extra={}) {
 const source = state.snapshots.find(s=>s.object_id===sourceId);
 return obj("task",id,status,{snapshot_id:sourceId,source_binding:binding(source),scenario:scenarioItems.baseline,evidence:{},candidate:null,calls:0,tools:0,tokens:0,unknown_calls:0,error:null,awaiting_fields:[],...extra});
}
state.tasks=[task("task-a"),task("task-b","src-b","awaiting_input",{awaiting_fields:["materials[0].initial_stock","orders[0].due_min"]})];
function replace(kind, value) {const idx=state[kind].findIndex(v=>v.object_id===value.object_id); if(idx<0)state[kind].push(value);else state[kind][idx]=value;return value;}
function update(kind, old, status, p=old.payload) {return replace(kind,obj(old.kind,old.object_id,status,p,old.version+1,old.owner));}
const H = "a".repeat(64), P = "b".repeat(64), O = "c".repeat(64);
function recommendation(t) {
 const source=state.snapshots.find(s=>s.object_id===t.payload.snapshot_id);
 const metric={weighted_tardiness:0,setup_minutes:3,makespan:18,load_spread:9,total:33};
 const slots=[{operation_id:"OP-A",machine_id:"M1",setup_start_min:0,start_min:3,end_min:12,predecessor_id:null},{operation_id:"OP-B",machine_id:"M2",setup_start_min:12,start_min:12,end_min:18,predecessor_id:null}];
 const plan={plan_id:"PLAN",snapshot_hash:H,problem_hash:P,objective_hash:O,slots,objective:metric};
 const problem={snapshot_hash:H,problem_hash:P,objective_hash:O,snapshot:clone(source.payload)};
 const report={valid:true,violations:[],objective:metric,snapshot_hash:H,problem_hash:P,objective_hash:O,plan_hash:digest(plan),inventory:[{item_id:"RAW",at_min:0,received:0,consumed:0,balance:6},{item_id:"RAW",at_min:3,received:0,consumed:4,balance:2},{item_id:"RAW",at_min:12,received:3,consumed:2,balance:3}],activities:[{resource_id:"CREW",resource_kind:"resource",operation_id:"OP-A",kind:"setup",start_min:0,end_min:3,demand:2},{resource_id:"CREW",resource_kind:"resource",operation_id:"OP-A",kind:"processing",start_min:3,end_min:12,demand:1}]};
 const comparison={selected:"optimized",selected_plan:plan,baseline_metrics:{...metric,total:51,makespan:24},optimized_metrics:metric,improvement:18};
 const tools=["get_snapshot","build_scenario","compile_problem","solve_schedule","validate_schedule","compare_schedules","simulate_schedule"];
 const evidence=Object.fromEntries(tools.map(tool=>[tool,{evidence_id:`ev-${tool}`,tool,hash:H,payload:tool==="solve_schedule"?{baseline:{status:"FEASIBLE",plan},optimized:{status:"OPTIMAL",plan}}:{source_binding:binding(source)}}]));
 const simulation={synthetic:true,status:"completed",plan_hash:report.plan_hash,events:[{kind:"process_start",at_min:3},{kind:"execution_completed",at_min:18}]};
 const rec=obj("recommendation",`plan-${t.object_id}`,"pending_review",{task_id:t.object_id,snapshot_id:source.object_id,source_binding:binding(source),scenario:t.payload.scenario,candidate:plan,compiled_problem:problem,validation:report,comparison,simulation,evidence,review:null,untrusted_model_draft:'<img src=x onerror="attack()">模型不能改计划'});
 replace("recommendations",rec);update("tasks",t,"submitted",{...t.payload,evidence,candidate:{plan_hash:report.plan_hash,plan},tools:8,calls:8});return rec;
}
function response(value,status=200) {return {ok:status<400,status,json:async()=>clone(value)};}
function server(path, init, body) {
 if(path==="/health")return response({status:"ok",provider:"TEST · original synthetic",synthetic_only:true});
 if(path==="/api/me")return response({user_id:"synthetic-admin",role,plants:["synthetic-plant"],acl_version:1});
 const [kind,id,action]=path.replace(/^\/api\//,"").split("/"); const rows=state[kind];assert(rows,`Unknown route ${path}`);
 if(init.method==="GET") {
   if(kind==="snapshots"&&action==="scenarios")return response({items:scenarioItems});
   return response(id?rows.find(v=>v.object_id===id):{items:rows});
 }
 if(kind==="tasks"&&!id) {assert.deepEqual(Object.keys(body).sort(),["action_id","scenario","snapshot_id"]);const t=task(`task-new-${++scenarioCounter}`,body.snapshot_id);t.payload.scenario=body.scenario;return response(replace(kind,t));}
 let value=rows.find(v=>v.object_id===id);assert(value,`Missing fixture ${path}`);
 if(action==="run") {
   assert.deepEqual(body,{});
   if(value.status==="cancelled")return response(update(kind,value,"cancelled",{...value.payload,lease_id:null,lease_until_ms:0}));
   if(value.payload.lease_id&&value.payload.lease_until_ms>serverNow)return response({error:{code:"busy",message:"服务端租约仍活跃"}},409);
   if(value.payload.unknown_calls>0)return response(update(kind,value,"blocked",{...value.payload,error:"model_unknown",lease_id:null}));
   if(unknownRun)return response(update(kind,value,"no_schedule",{...value.payload,evidence:{solve_schedule:{tool:"solve_schedule",payload:{baseline:{status:"INFEASIBLE"},optimized:{status:"UNKNOWN"}}}}}));
   recommendation(value);return response(state.tasks.find(t=>t.object_id===id));
 }
 if(kind==="tasks"&&action==="cancel")return response(update(kind,value,"cancelled"));
 if(action==="resume") {
   assert.deepEqual(Object.keys(body).sort(),["action_id","answers","note"]);
   assert.deepEqual(body.answers.map(a=>a.field),["materials[0].initial_stock","orders[0].due_min"]);
   body.answers.forEach(a=>assert(Number.isSafeInteger(a.value)));
   const src=state.snapshots.find(s=>s.object_id===value.payload.snapshot_id),oldTime=src.payload.observed_at_ms;
   const changed=update("snapshots",src,"ready",{...src.payload,revision:2,materials:[{item_id:"RAW",initial_stock:body.answers[0].value}],orders:[{order_id:"ORDER",due_min:body.answers[1].value}]});
   assert.equal(changed.payload.observed_at_ms,oldTime);
   return response(update(kind,value,"created",{...value.payload,source_binding:binding(changed),awaiting_fields:[],evidence:{}}));
 }
 if(action==="reconcile") {
   assert.equal(role,"admin");assert.equal(body.no_result,true);
   if(value.payload.lease_id&&value.payload.lease_until_ms>serverNow)return response({error:{code:"busy",message:"活跃租约不得对账"}},409);
   return response(update(kind,value,value.status==="cancelled"?"cancelled":"created",{...value.payload,unknown_calls:0,error:null,lease_id:null}));
 }
 if(action==="review") {
   assert.equal(body.recommendation_hash,value.content_hash);assert(["approved","rejected"].includes(body.decision));assert(body.note.trim());
   assert.deepEqual(Object.keys(body).sort(),["action_id","decision","note","recommendation_hash"]);
   return response(update(kind,value,body.decision,{...value.payload,review:{decision:body.decision,note:body.note}}));
 }
 if(action==="request-publication") {
   assert.equal(value.status,"approved");assert.equal(body.confirmed,true);assert.equal(body.recommendation_hash,value.content_hash);
   return response(replace("publications",obj("publication",`pub-${id}`,"requested",{recommendation_id:id,recommendation_hash:body.recommendation_hash,source_binding:value.payload.source_binding,plan_hash:value.payload.validation.plan_hash,delivery_status:"not_sent",receiver_status:"none",execution_status:"none",receiver_sequence:0,accepted_operation_ids:[],execution_result:null,synthetic:true})));
 }
 if(action==="deliver-local") {
   assert.deepEqual(Object.keys(body),["action_id"]);assert.equal(value.payload.delivery_status,"not_sent");
   const changed=update(kind,value,failDeliver?"unknown":"partial",{...value.payload,delivery_status:failDeliver?"unknown":"acknowledged",receiver_status:failDeliver?"none":"partial",receiver_sequence:1,accepted_operation_ids:["OP-A"]});
   if(failDeliver)throw new TypeError("lost ACK");return response(changed);
 }
 if(action==="advance-receiver") {assert.equal(value.payload.receiver_status,"partial");return response(update(kind,value,"accepted",{...value.payload,receiver_status:"accepted",receiver_sequence:2,accepted_operation_ids:["OP-A","OP-B"]}));}
 if(action==="readback") {
   assert.deepEqual(Object.keys(body),["action_id"]);
   if(value.payload.delivery_status==="unknown")return response(update(kind,value,"partial",{...value.payload,delivery_status:"acknowledged",receiver_status:"partial"}));
   return response(value);
 }
 if(kind==="publications"&&action==="cancel") {
   assert(body.note.trim());if(value.payload.execution_status!=="none")return response({error:{code:"too_late",message:"执行已经开始"}},409);
   return response(update(kind,value,"cancelled",{...value.payload,receiver_status:"cancelled",delivery_status:"acknowledged"}));
 }
 if(action==="execute-synthetic") {
   assert.equal(body.confirmed,true);assert.equal(value.payload.receiver_status,"accepted");assert.equal(value.payload.execution_status,"none");
   return response(update(kind,value,"completed",{...value.payload,execution_status:"completed",execution_result:{synthetic:true,status:"completed",events:[{kind:"execution_completed",at_min:18}]}}));
 }
 throw new Error(`Unhandled route ${path}`);
}
async function fetchMock(path, init) {
 assert.equal(init.credentials,"omit");assert.equal(init.cache,"no-store");assert.equal(init.redirect,"error");
 assert.equal(init.headers.Authorization,["Bearer", "memory-only-test-token"].join(" "));assert(init.signal);assert(!path.includes("token"));
 const body=init.body?JSON.parse(init.body):undefined, record={path,init,body};requests.push(record);
 if(body&&!path.endsWith("/run")){assert.match(body.action_id,/^[a-f0-9-]{36}$/);for(const key of ["token","role","owner","fault","signature","keys","receiver_status"])assert(!(key in body));}
 if(intercept){const result=intercept(record);if(result!==undefined)return await result;}
 return server(path,init,body);
}
const sandbox={document:{getElementById:$,createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>{assert.equal(ns,"http://www.w3.org/2000/svg");return new Element(tag);}},window:{addEventListener:(name,fn)=>{windowListeners[name]=fn;},requestAnimationFrame:fn=>{if(holdFrames)frameQueue.push(fn);else setImmediate(fn);}},crypto:{randomUUID:()=>crypto.randomUUID()},fetch:fetchMock,AbortController,Date,setTimeout,clearTimeout};
vm.runInNewContext(input.script,sandbox,{filename:"app.js"});
const tick=async()=>{for(let i=0;i<15;i++)await new Promise(resolve=>setImmediate(resolve));};
async function emit(id,event) {for(const fn of [...($(id).listeners[event]||[])])fn({preventDefault(){}});await tick();}
async function choose(id,value) {$(id).value=value;await emit(id,"change");}
async function fill(id,value) {$(id).value=value;await emit(id,"input");}
async function check(id) {$(id).checked=true;await emit(id,"change");}
const mutations=()=>requests.filter(r=>r.init.method==="POST");
const descendants=e=>[e,...e.children.flatMap(descendants)];
async function login(){ $("token").value="memory-only-test-token";await emit("login-form","submit");assert.equal($("token").value,"");assert.equal($("workbench").hidden,false);}
async function openSubmitted(){recommendation(state.tasks[0]);await login();await choose("task-select","task-a");assert.equal($("recommendation-select").value,"plan-task-a");}
async function approve(){await choose("review-decision","approved");await fill("review-note","人工核对计划与独验来源");await emit("review-form","submit");}
async function publish(){await openSubmitted();await approve();await check("publication-confirm");await emit("publication-form","submit");assert.equal($("publication-select").value,"pub-plan-task-a");}
(async()=>{SCENARIO})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run_dom(document, scenario):
    result = subprocess.run(
        [NODE, "-e", DOM_HARNESS.replace("SCENARIO", scenario)],
        input=json.dumps({"elements": document.elements, "script": JS.read_text()}),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_explicit_task_creation_and_nine_tool_evidence(document):
    run_dom(document, r'''
      await login();assert.equal(mutations().length,0);
      assert.equal($("snapshot-select").value,"");
      await choose("snapshot-select","src-a");
      assert.equal($("create-task").disabled,true);
      await choose("scenario-select","baseline");
      assert.deepEqual(JSON.parse($("scenario-json").value),scenarioItems.baseline);
      await emit("create-task","click");assert.equal(mutations().length,1);
      await emit("run-task","click");assert.equal(mutations().length,2);
      assert.deepEqual(mutations()[1].body,{});
      assert.equal($("tool-chain").children.length,9);
      assert.match($("task-evidence").textContent,/ev-simulate_schedule/);
      assert.match($("solver-summary").textContent,/FEASIBLE/);
      assert.match($("solver-summary").textContent,/OPTIMAL/);
      assert.equal($("review-decision").value,"");assert.equal($("review-submit").disabled,true);
      assert.equal($("model-draft").children.length,0);assert.match($("model-draft").textContent,/<img src=x/);
      assert.equal(state.publications.length,0);
      assert.equal($("workbench").getAttribute("aria-busy"),"false");
    ''')


@pytest.mark.parametrize("scenario", ["baseline", "insert_order", "machine_outage", "supply_delay"])
def test_all_four_explicit_scenarios_are_frozen_in_task_body(document, scenario):
    run_dom(document, r'''
      await login();await choose("snapshot-select","src-a");await choose("scenario-select","CASE");
      assert.deepEqual(JSON.parse($("scenario-json").value),scenarioItems.CASE);
      await emit("create-task","click");assert.equal(mutations().length,1);
      assert.deepEqual(mutations()[0].body.scenario,scenarioItems.CASE);
      assert.equal(state.tasks.at(-1).payload.scenario.kind,"CASE");
      assert.equal(state.recommendations.length,0);
    '''.replace("CASE", scenario))


@pytest.mark.parametrize("bad", ['{"kind":"supply_delay"}', '{"kind":"baseline","x":1.1}', '{"kind":"baseline","x":1e999}'])
def test_invalid_or_mismatched_custom_scenario_never_posts(document, bad):
    run_dom(document, r'''
      await login();await choose("snapshot-select","src-a");await choose("scenario-select","baseline");
      await fill("scenario-json",BAD);await emit("create-task","click");
      assert.equal(mutations().length,0);assert.equal($("error-message").hidden,false);
    '''.replace("BAD", json.dumps(bad)))


def test_selected_validated_plan_native_gantt_resources_inventory_and_compare(document):
    run_dom(document, r'''
      await openSubmitted();
      const gantt=descendants($("gantt-chart"));
      assert.equal(gantt.filter(e=>e.tagName==="SVG").length,1);
      assert.equal(gantt.filter(e=>e.tagName==="RECT"&&e.attrs.class==="setup").length,1);
      assert.equal(gantt.filter(e=>e.tagName==="RECT"&&e.attrs.class==="processing").length,2);
      assert.match($("gantt-chart").textContent,/M1/);assert.match($("gantt-chart").textContent,/M2/);
      assert.match($("gantt-chart").textContent,/\[0, 3\)/);assert.match($("gantt-chart").textContent,/\[3, 12\)/);
      const inventory=descendants($("inventory-chart")).find(e=>e.tagName==="PATH");
      assert(inventory.attrs.d.includes(" H ")&&inventory.attrs.d.includes(" V "));
      assert.equal(descendants($("inventory-chart")).filter(e=>e.tagName==="CIRCLE").length,3);
      assert.match($("inventory-table").textContent,/RAW/);assert.match($("inventory-table").textContent,/6/);
      assert.match($("resource-chart").textContent,/容量 2/);
      assert.equal(descendants($("resource-chart")).filter(e=>e.attrs.class==="capacity").length,1);
      for(const key of ["加权迟期","换型分钟","最大完工时间","机器负荷极差","51","33"])assert($("objective-comparison").textContent.includes(key));
      assert.match($("comparison-summary").textContent,/18/);
      assert.match($("simulation-summary").textContent,/execution_completed/);
      assert.match($("execution-summary").textContent,/尚无/);
      assert.equal(mutations().length,0);
      await choose("recommendation-select","");
      assert.equal(descendants($("gantt-chart")).filter(e=>e.tagName==="SVG").length,0);
    ''')


def test_invalid_report_or_cross_problem_plan_is_never_plotted(document):
    run_dom(document, r'''
      const rec=recommendation(state.tasks[0]);rec.payload.validation.problem_hash="f".repeat(64);
      await login();await choose("recommendation-select",rec.object_id);
      assert.equal(descendants($("gantt-chart")).filter(e=>e.tagName==="SVG").length,0);
      assert.match($("objective-comparison").textContent,/不绘制/);
      assert.equal($("review-decision").disabled,true);assert.equal($("review-submit").disabled,true);
    ''')


def test_infeasible_and_unknown_remain_distinct_without_candidate(document):
    run_dom(document, r'''
      unknownRun=true;await login();await choose("task-select","task-a");await emit("run-task","click");
      assert.match($("solver-summary").textContent,/INFEASIBLE · 已证明无解/);
      assert.match($("solver-summary").textContent,/UNKNOWN · 未找到候选，不等于无解/);
      assert.equal(state.recommendations.length,0);assert.equal($("run-task").disabled,true);
      assert.equal($("request-publication").disabled,true);
    ''')


@pytest.mark.parametrize("invalid", ["1.0", "true", "1e2", "-1", "9007199254740992"])
def test_missing_answers_reject_floats_booleans_exponents_and_out_of_range(document, invalid):
    run_dom(document, r'''
      await login();await choose("task-select","task-b");
      assert.equal($("supplement-form").hidden,false);
      assert.equal($("answer-0").dataset.field,"materials[0].initial_stock");
      assert.equal($("answer-1").dataset.field,"orders[0].due_min");
      await fill("answer-0",INVALID);await fill("answer-1","30");await fill("supplement-note","人工确认来源");
      assert.equal($("supplement-submit").disabled,true);await emit("supplement-form","submit");assert.equal(mutations().length,0);
    '''.replace("INVALID", json.dumps(invalid)))


def test_supplement_preserves_exact_paths_observed_time_and_does_not_run(document):
    run_dom(document, r'''
      await login();await choose("task-select","task-b");const observed=state.snapshots[1].payload.observed_at_ms;
      await fill("answer-0","6");await fill("answer-1","30");await fill("supplement-note","人工补齐合成输入");
      await emit("supplement-form","submit");assert.equal(mutations().length,1);
      assert.deepEqual(mutations()[0].body.answers,[{field:"materials[0].initial_stock",value:6},{field:"orders[0].due_min",value:30}]);
      assert.equal(state.snapshots[1].payload.observed_at_ms,observed);
      assert.equal($("supplement-form").hidden,true);assert.equal($("run-task").disabled,false);
      assert.equal(state.recommendations.length,0);
    ''')


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_review_requires_explicit_decision_note_and_current_content_hash(document, decision):
    run_dom(document, r'''
      await openSubmitted();await emit("review-form","submit");assert.equal(mutations().length,0);
      await choose("review-decision","DECISION");await fill("review-note","   ");assert.equal($("review-submit").disabled,true);
      const hash=state.recommendations[0].content_hash;
      await fill("review-note","已核对当前独验候选和来源");await emit("review-form","submit");
      assert.equal(mutations().length,1);assert.equal(mutations()[0].body.recommendation_hash,hash);
      assert.equal(mutations()[0].body.decision,"DECISION");assert.equal(state.recommendations[0].status,"DECISION");
      assert.equal($("review-decision").value,"");assert.equal($("publication-confirm").checked,false);
      assert.equal($("request-publication").disabled,true);assert.equal(state.publications.length,0);
    '''.replace("DECISION", decision))


def test_owner_plant_isolation_and_operator_cannot_review(document):
    run_dom(document, r'''
      role="operator";recommendation(state.tasks[0]);
      const outsider=obj("snapshot","foreign","ready",snapshotPayload("foreign"),1,"other-owner");state.snapshots.push(outsider);
      await login();await choose("task-select","task-a");
      assert.equal($("snapshot-select").children.some(e=>e.value==="foreign"),false);
      assert.equal($("review-decision").disabled,true);assert.equal($("review-note").disabled,true);
      await choose("review-decision","approved");await fill("review-note","无权限");await emit("review-form","submit");
      assert.equal(mutations().length,0);
    ''')


def test_publish_receive_advance_readback_execute_are_independent(document):
    run_dom(document, r'''
      await publish();assert.equal(mutations().length,2);assert.equal(state.publications[0].status,"requested");
      assert.equal($("execute-publication").disabled,true);assert.equal($("advance-publication").disabled,true);
      await emit("deliver-publication","click");assert.equal(mutations().length,3);assert.equal(state.publications[0].status,"partial");
      assert.equal($("execute-publication").disabled,true);assert.equal($("deliver-publication").disabled,true);
      await emit("advance-publication","click");assert.equal(mutations().length,4);assert.equal(state.publications[0].status,"accepted");
      assert.equal($("execute-confirm").checked,false);assert.equal($("execute-publication").disabled,true);
      assert.match($("readback-summary").textContent,/不冒充/);
      await emit("readback-publication","click");assert.equal(mutations().length,5);
      assert.match($("readback-summary").textContent,/已独立请求回读/);assert.equal(state.publications[0].payload.execution_status,"none");
      await check("execute-confirm");await emit("execute-form","submit");assert.equal(mutations().length,6);
      assert.equal(state.publications[0].status,"completed");assert.match($("execution-summary").textContent,/execution_completed/);
      assert.equal($("execute-publication").disabled,true);assert.equal($("cancel-publication").disabled,true);
      assert.equal(new Set(mutations().map(r=>r.body.action_id)).size,6);
      await tick();assert.equal(mutations().length,6);
    ''')


def test_cancel_publication_is_a_separate_noted_action(document):
    run_dom(document, r'''
      await publish();assert.equal($("cancel-publication").disabled,true);
      await fill("cancel-publication-note","取消尚未执行的合成计划");await emit("cancel-publication-form","submit");
      assert.equal(mutations().length,3);assert.equal(state.publications[0].status,"cancelled");
      assert.equal($("deliver-publication").disabled,true);assert.equal($("execute-publication").disabled,true);
      assert.deepEqual(Object.keys(mutations()[2].body).sort(),["action_id","note"]);
    ''')


def test_lost_delivery_ack_requires_reload_then_independent_readback(document):
    run_dom(document, r'''
      await publish();failDeliver=true;await emit("deliver-publication","click");
      assert.equal(state.publications[0].status,"unknown");assert.match($("error-message").textContent,/network_unknown/);
      assert.equal($("deliver-publication").disabled,true);const count=mutations().length;
      await emit("deliver-publication","click");assert.equal(mutations().length,count);
      await emit("reload-button","click");assert.equal($("deliver-publication").disabled,true);
      assert.equal($("readback-publication").disabled,false);await emit("readback-publication","click");
      assert.equal(state.publications[0].status,"partial");assert.equal($("advance-publication").disabled,false);
      assert.equal(mutations().length,count+1);
    ''')


def test_server_error_after_delivery_intent_locks_retry_until_readback(document):
    run_dom(document, r'''
      await publish();intercept=r=>{
        if(!r.path.endsWith("/deliver-local"))return undefined;
        const old=state.publications[0];update("publications",old,"unknown",{...old.payload,delivery_status:"unknown"});
        return response({error:{code:"internal_error",message:"receiver acknowledgement unknown"}},500);
      };
      await emit("deliver-publication","click");assert.match($("error-message").textContent,/internal_error/);
      assert.equal($("deliver-publication").disabled,true);const count=mutations().length;
      await emit("deliver-publication","click");assert.equal(mutations().length,count);
      intercept=null;await emit("reload-button","click");assert.equal($("deliver-publication").disabled,true);
      assert.equal($("readback-publication").disabled,false);await emit("readback-publication","click");
      assert.equal(state.publications[0].status,"partial");assert.equal(mutations().length,count+1);
    ''')


def test_busy_covers_login_detail_reload_followup_and_two_paint_frames(document):
    run_dom(document, r'''
      let finish;intercept=r=>r.path==="/api/me"?new Promise(resolve=>{finish=()=>resolve(server(r.path,r.init,r.body));}):undefined;
      $("token").value="memory-only-test-token";await emit("login-form","submit");assert.equal($("workbench").getAttribute("aria-busy"),"true");
      holdFrames=true;finish();await tick();assert.equal($("workbench").getAttribute("aria-busy"),"true");
      frameQueue.shift()();await tick();assert.equal($("workbench").getAttribute("aria-busy"),"true");
      frameQueue.shift()();await tick();assert.equal($("workbench").getAttribute("aria-busy"),"false");holdFrames=false;
      intercept=r=>r.path==="/api/tasks/task-a"?new Promise(resolve=>{finish=()=>resolve(server(r.path,r.init,r.body));}):undefined;
      await choose("task-select","task-a");assert.equal($("workbench").getAttribute("aria-busy"),"true");finish();await tick();assert.equal($("workbench").getAttribute("aria-busy"),"false");
      intercept=null;await choose("snapshot-select","src-a");await choose("scenario-select","baseline");
      intercept=r=>r.path==="/api/audit"?new Promise(resolve=>{finish=()=>resolve(server(r.path,r.init,r.body));}):undefined;
      await emit("create-task","click");assert.equal($("workbench").getAttribute("aria-busy"),"true");finish();await tick();assert.equal($("workbench").getAttribute("aria-busy"),"false");
    ''')


def test_cancel_remains_available_during_run_and_old_reply_cannot_undo_it(document):
    run_dom(document, r'''
      await login();await choose("task-select","task-a");let finish;const old=clone(state.tasks[0]);
      intercept=r=>r.path.endsWith("/run")?new Promise(resolve=>{finish=resolve;}):undefined;
      await emit("run-task","click");assert.equal($("run-task").disabled,true);assert.equal($("cancel-task").disabled,false);
      await emit("cancel-task","click");assert.equal(state.tasks[0].status,"cancelled");assert.equal($("workbench").getAttribute("aria-busy"),"true");
      finish(response(old));await tick();assert.match($("task-summary").textContent,/已取消/);
      assert.equal($("workbench").getAttribute("aria-busy"),"false");assert.equal($("run-task").disabled,true);
      assert.equal($("stale-banner").hidden,false);assert.equal(state.recommendations.length,0);
    ''')


@pytest.mark.parametrize("action", ["logout", "reload", "pagehide"])
def test_generation_fences_ignore_delayed_response_and_handlers_reconnect(document, action):
    run_dom(document, r'''
      await login();await choose("task-select","task-a");let finish;const old=clone(state.tasks[0]);old.version=100;old.payload.error="LATE_RESPONSE_MUST_NOT_RENDER";
      intercept=r=>r.path.endsWith("/run")?new Promise(resolve=>{finish=resolve;}):undefined;
      await emit("run-task","click");const request=requests.find(r=>r.path.endsWith("/run"));
      if("ACTION"==="pagehide")windowListeners.pagehide();else await emit("ACTION"==="logout"?"logout-button":"reload-button","click");
      assert.equal(request.init.signal.aborted,true);finish(response(old));await tick();
      assert(!$("task-summary").textContent.includes("LATE_RESPONSE_MUST_NOT_RENDER"));
      assert.equal($("workbench").getAttribute("aria-busy"),"false");
      if("ACTION"!=="reload"){assert.equal($("workbench").hidden,true);assert.equal($("review-note").value,"");intercept=null;await login();}
      else intercept=null;
      await choose("snapshot-select","src-a");await choose("scenario-select","baseline");const before=mutations().length;
      await emit("create-task","click");assert.equal(mutations().length,before+1);
      assert.equal($("create-task").listeners.click.length,1);
    '''.replace("ACTION", action))


@pytest.mark.parametrize("status", ["running", "blocked", "cancelled"])
def test_admin_unknown_reconcile_available_in_all_backend_recovery_states(document, status):
    run_dom(document, r'''
      state.tasks[0]=task("task-a","src-a","STATUS",{calls:1,unknown_calls:1,lease_id:"expired",lease_until_ms:serverNow-1,error:"model_unknown"});
      await login();await choose("task-select","task-a");assert.equal($("reconcile-form").hidden,false);
      assert.equal($("reconcile-submit").disabled,true);assert.equal(mutations().length,0);
      await fill("reconcile-call","call-original");await fill("reconcile-note","核查进程日志确认无可用结果");await check("reconcile-confirm");await emit("reconcile-form","submit");
      assert.equal(mutations().length,1);assert.equal(mutations()[0].body.tokens,null);assert.equal(mutations()[0].body.no_result,true);
      assert.equal(state.tasks[0].status,"STATUS"==="cancelled"?"cancelled":"created");assert.equal(state.tasks[0].payload.unknown_calls,0);
      assert.equal($("reconcile-form").hidden,true);assert.equal($("run-task").disabled,"STATUS"==="cancelled");
      assert.equal(state.recommendations.length,0);
    '''.replace("STATUS", status))


def test_running_recovery_and_admin_busy_do_not_trust_browser_clock(document):
    run_dom(document, r'''
      serverNow=Date.now()-600000;state.tasks[0]=task("task-a","src-a","running",{unknown_calls:1,calls:1,lease_id:"active",lease_until_ms:Date.now()-300000});
      await login();await choose("task-select","task-a");assert.equal($("run-task").disabled,false);assert.match($("run-task").textContent,/后端核验租约/);
      await emit("run-task","click");assert.match($("error-message").textContent,/busy/);assert.equal(state.tasks[0].status,"running");
      await fill("reconcile-call","call-active");await fill("reconcile-note","只请求服务端核验");await check("reconcile-confirm");await emit("reconcile-form","submit");
      assert.match($("error-message").textContent,/busy/);assert.equal(mutations().length,2);assert.equal(state.tasks[0].payload.unknown_calls,1);
      await emit("logout-button","click");role="operator";await login();await choose("task-select","task-a");assert.equal($("reconcile-form").hidden,true);
      await emit("reconcile-form","submit");assert.equal(mutations().length,2);
    ''')


def test_explicit_expired_running_recovery_normalizes_unknown_without_new_call(document):
    run_dom(document, r'''
      serverNow=Date.now()+600000;state.tasks[0]=task("task-a","src-a","running",{unknown_calls:1,calls:1,lease_id:"server-expired",lease_until_ms:Date.now()+300000});
      await login();await choose("task-select","task-a");await emit("run-task","click");
      assert.equal(state.tasks[0].status,"blocked");assert.equal(state.tasks[0].payload.calls,1);assert.equal(state.tasks[0].payload.unknown_calls,1);
      assert.equal($("run-task").disabled,true);assert.equal($("reconcile-form").hidden,false);
      await emit("reload-button","click");assert.equal(mutations().length,1);assert.equal(state.recommendations.length,0);
    ''')


def test_source_version_change_resets_consent_and_blocks_stale_actions(document):
    run_dom(document, r'''
      await openSubmitted();await choose("review-decision","approved");await fill("review-note","原页面审阅");
      const old=state.snapshots[0];update("snapshots",old,"ready",{...old.payload,revision:2});
      await emit("reload-button","click");assert.equal($("stale-banner").hidden,false);assert.match($("stale-banner").textContent,/来源版本/);
      assert.equal($("review-submit").disabled,true);assert.equal($("review-decision").value,"");assert.equal($("run-task").disabled,true);
      assert.equal(mutations().length,0);
    ''')


def test_cancelled_unknown_can_explicitly_normalize_without_restart(document):
    run_dom(document, r'''
      state.tasks[0]=task("task-a","src-a","cancelled",{calls:1,unknown_calls:1,lease_id:"dead",lease_until_ms:serverNow-1});
      await login();await choose("task-select","task-a");assert.equal($("run-task").disabled,false);
      assert.match($("run-task").textContent,/不重启/);await emit("run-task","click");
      assert.equal(state.tasks[0].status,"cancelled");assert.equal(state.tasks[0].payload.calls,1);
      assert.equal(state.tasks[0].payload.unknown_calls,1);assert.equal($("reconcile-form").hidden,false);
      assert.equal(state.recommendations.length,0);assert.equal(mutations().length,1);
    ''')


def test_late_scenario_metadata_cannot_replace_new_selection(document):
    run_dom(document, r'''
      await login();let finish;
      intercept=r=>r.path==="/api/snapshots/src-a/scenarios"?new Promise(resolve=>{finish=resolve;}):undefined;
      await choose("snapshot-select","src-a");assert.equal($("workbench").getAttribute("aria-busy"),"true");
      await choose("snapshot-select","src-b");await choose("scenario-select","baseline");
      finish(response({items:{baseline:{kind:"baseline",bad_late_data:true}}}));await tick();
      assert.equal($("snapshot-select").value,"src-b");assert(!$("scenario-json").value.includes("bad_late_data"));
      assert.equal(mutations().length,0);assert.equal($("workbench").getAttribute("aria-busy"),"false");
    ''')


def test_same_source_but_different_selected_plan_cannot_render_as_current(document):
    run_dom(document, r'''
      const rec=recommendation(state.tasks[0]);rec.payload.comparison.selected_plan=clone(rec.payload.candidate);
      rec.payload.comparison.selected_plan.slots[0].start_min=5;
      await login();await choose("recommendation-select",rec.object_id);
      assert.equal(descendants($("gantt-chart")).filter(e=>e.tagName==="SVG").length,0);
      assert.match($("gantt-chart").textContent,/不绘制/);
    ''')


def test_execution_running_never_enables_repeat_execution_or_cancel(document):
    run_dom(document, r'''
      await publish();const old=state.publications[0];
      update("publications",old,"running",{...old.payload,delivery_status:"acknowledged",receiver_status:"accepted",execution_status:"running"});
      await emit("reload-button","click");assert.equal($("execute-publication").disabled,true);
      assert.equal($("cancel-publication").disabled,true);assert.equal($("readback-publication").disabled,false);
      assert.match($("execution-summary").textContent,/不得自动重跑/);
      const count=mutations().length;await emit("execute-form","submit");assert.equal(mutations().length,count);
    ''')


def test_acl_revocation_clears_all_identity_and_rendered_data(document):
    run_dom(document, r'''
      await openSubmitted();intercept=r=>r.path==="/api/me"?response({error:{code:"forbidden",message:"revoked"}},403):undefined;
      await emit("reload-button","click");assert.equal($("workbench").hidden,true);
      assert.equal($("snapshot-select").value,"");assert.equal($("token").value,"");
      assert.match($("message").textContent,/权限已变化/);assert.equal($("run-task").disabled,true);
      assert.equal($("identity").textContent,"");assert.equal(mutations().length,0);
    ''')
