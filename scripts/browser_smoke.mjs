#!/usr/bin/env node
/**
 * Actual installed Chrome/CDP + original scheduling UI/API + deterministic TEST ONLY selector.
 * Import, --help and --self-test are inert: no children, network, directories or browser access.
 * Explicit --run-chrome creates its own private app/profile under an outside-source output dir.
 * No model, ambient configuration, real browser profile, user cookies or physical MES is used.
 */
import assert from 'node:assert/strict';
import {spawn, execFile} from 'node:child_process';
import {constants} from 'node:fs';
import {access, lstat, mkdtemp, open, readFile, readdir, realpath, rm, writeFile} from 'node:fs/promises';
import {request as httpRequest} from 'node:http';
import {dirname, isAbsolute, join, relative, resolve} from 'node:path';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

export const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const MAX_RUN_MS = 300_000;
const COMPUTATION = Object.freeze(['get_snapshot', 'build_scenario', 'compile_problem',
  'solve_schedule', 'validate_schedule', 'compare_schedules', 'simulate_schedule']);
export const SELECTORS = Object.freeze({
  token: '#token', login: '#connect-button', logout: '#logout-button', workspace: '#workbench',
  snapshot: '#snapshot-select', scenario: '#scenario-select', create: '#create-task', run: '#run-task',
  task: '#task-select', recommendation: '#recommendation-select', publication: '#publication-select',
  decision: '#review-decision', note: '#review-note', review: '#review-submit',
  publishConfirm: '#publication-confirm', publish: '#request-publication',
  deliver: '#deliver-publication', advance: '#advance-publication', readback: '#readback-publication',
  executeConfirm: '#execute-confirm', execute: '#execute-publication', refresh: '#reload-button',
});
const STATUS_TEXT = Object.freeze({
  created: '已创建', submitted: '已送审', awaiting_input: '等待人工补缺',
  pending_review: '待人工审阅', approved: '已批准', requested: '已请求发布',
  partial: '部分接收', accepted: '完整接收', completed: '合成执行完成', none: '尚无',
  not_sent: '尚未递送', acknowledged: '已确认回执',
});
const XSS_NOTE = '<img id="smoke-xss" src="x" onerror="globalThis.__smokeXss=1"> & <script>globalThis.__smokeXss=2</script>';
const NOT_TESTED = Object.freeze(['actualLLM', 'multi-tab', 'otherbrowsers', 'OStraffic', 'physicalMES',
  'real browser profile or cookie-file forensics', 'wall-clock/physical execution evidence']);
export const USAGE = `Scheduling TEST ONLY browser smoke — installed macOS Chrome, Node >=24.
Usage (from this project):
  node scripts/browser_smoke.mjs --help
  node scripts/browser_smoke.mjs --self-test
  node scripts/browser_smoke.mjs --run-chrome --artifacts-parent /private/tmp
      [--chrome /absolute/installed/Chrome] [--python /absolute/project/python]

No Chrome, uvicorn, network or files are touched without --run-chrome AND --artifacts-parent.
MAIN should run the last command only after UI/API source is stable. No install/download step.
Both listeners use explicit 127.0.0.1 and random ports. A NEW private profile and uvicorn child
use a minimal environment and deterministic SyntheticProvider, never an actual LLM or model config.
Root execution is refused. Browser sandbox/security remain enabled. No real profile/cookies.
The output parent must already exist outside source; a fresh 0700 scheduling-browser-* directory
retains only sanitized summary.json and console.log (0600). Own app/profile are removed after
tracked child shutdown. No screenshots, raw API dumps or plaintext tokens are retained.
UI lifecycle mutations use real clicks; read-only probes and explicitly labelled negative security
probes are separate. No response rewriting, fake Gantt values or arbitrary completion sleeps.
Token audit is limited to this run's exact reports, own app documents and current-origin DOM
storage. It does NOT scan Chrome cookie files or arbitrary user files.
NOT_TESTED: actualLLM, multi-tab, otherbrowsers, OStraffic, physicalMES.
`;

export function isWithin(parent, child) {
  const path = relative(parent, child);
  return path === '' || (path !== '..' && !path.startsWith('../') && !isAbsolute(path));
}
export function parseArgs(args) {
  if (!args.length || (args.length === 1 && args[0] === '--help')) return {mode: 'help'};
  if (args.length === 1 && args[0] === '--self-test') return {mode: 'self-test'};
  const result = {mode: 'run', chrome: DEFAULT_CHROME, python: join(PROJECT, '.venv/bin/python')};
  const seen = new Set();
  for (let i = 0; i < args.length; i++) {
    const flag = args[i];
    assert(!seen.has(flag), 'Duplicate argument'); seen.add(flag);
    if (flag === '--run-chrome') { result.authorized = true; continue; }
    assert(['--artifacts-parent', '--chrome', '--python'].includes(flag), 'Unknown argument');
    const value = args[++i];
    assert(typeof value === 'string' && isAbsolute(value) && !/[\x00-\x1f\x7f]/.test(value),
      'An explicit absolute path is required');
    result[{'--artifacts-parent': 'parent', '--chrome': 'chrome', '--python': 'python'}[flag]] = value;
  }
  assert(result.authorized && result.parent, '--run-chrome AND --artifacts-parent are required');
  assert(!isWithin(PROJECT, resolve(result.parent)), 'Artifacts must be outside source');
  return result;
}
export function sameOriginPath(value) {
  assert(typeof value === 'string' && /^\/(?:[A-Za-z0-9_.:-]+\/?)*$/.test(value)
    && !value.startsWith('//') && !value.split('/').some(part => part === '.' || part === '..'),
  'Only literal, bounded same-origin route paths are allowed');
  assert(value.length < 512, 'Route bound');
  return value;
}

// Private bootstrap only. No extra application routes, no lifecycle objects seeded.
// Tokens travel once over inherited stdout, never argv/env, disk or logs.
export const PY_SERVER = String.raw`
import json, os, secrets, socket, sys, threading, time
from pathlib import Path

def bootstrap():
    channel = sys.stdout
    sys.stdout = sys.stderr
    lock = threading.Lock()
    def send(value):
        with lock:
            channel.write(json.dumps(value, ensure_ascii=True) + '\n')
            channel.flush()
    try:
        os.umask(0o077)
        import uvicorn
        from scheduling_agent.api import create_app
        from scheduling_agent.fixtures import demo_snapshots
        from scheduling_agent.provider import SyntheticProvider
        root = Path(sys.argv[1])
        if root.exists():
            raise ValueError('requires a new owned app directory')
        app = create_app(root, SyntheticProvider())
        fixtures = demo_snapshots(time.time_ns() // 1_000_000)
        plants = sorted({item.plant_id for item in fixtures.values()})
        identities = {}
        for name, role in [('owner', 'admin'), ('other', 'reviewer')]:
            token = secrets.token_urlsafe(32)
            actor = app.state.store.create_user('browser-' + name, token, role, plants)
            identities[name] = {'token': token, 'user_id': actor.user_id, 'role': role}
            if name == 'owner':
                owner = actor
        sources = {}
        for name, snapshot in fixtures.items():
            obj = app.state.service.import_snapshot(owner, snapshot, action_id='browser-import-' + name)
            sources[name] = {'object_id': obj['object_id'], 'snapshot': snapshot.model_dump(mode='json')}
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', log_level='critical',
            access_log=False, log_config=None, server_header=False, proxy_headers=False))
        def control():
            for line in sys.stdin:
                message = {}
                try:
                    if len(line) > 2048:
                        raise ValueError('control bound')
                    message = json.loads(line)
                    command = message['command']
                    if command == 'owner_operator':
                        app.state.store.set_user(owner.user_id, role='operator')
                        value = {'role': 'operator'}
                    elif command == 'owner_admin':
                        app.state.store.set_user(owner.user_id, role='admin')
                        value = {'role': 'admin'}
                    elif command == 'shutdown':
                        server.should_exit = True
                        value = {'stopping': True}
                    else:
                        raise ValueError('unknown control')
                    send({'id': message['id'], 'value': value})
                except Exception:
                    send({'id': message.get('id'), 'error': 'control_failed'})
            server.should_exit = True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        threading.Thread(target=control, daemon=True).start()
        send({'type': 'ready', 'port': sock.getsockname()[1], 'identities': identities,
              'fixtures': sources, 'provider': app.state.provider.fingerprint})
        server.run(sockets=[sock])
    except BaseException:
        send({'type': 'failed', 'code': 'owned_test_server_failed'})
        raise SystemExit(1) from None

if __name__ == '__main__':
    bootstrap()
`;

function timeout(promise, ms, message, signal) {
  let timer, abort;
  const expiry = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
    abort = () => reject(new Error('Smoke interrupted'));
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, {once: true});
  });
  return Promise.race([promise, expiry]).finally(() => {
    clearTimeout(timer); signal?.removeEventListener('abort', abort);
  });
}
// Poll cadence, not a completion assumption: every wait has an explicit predicate + deadline.
function nextPoll(ms = 50) { return new Promise(resolvePromise => setTimeout(resolvePromise, ms)); }

// Diagnostics retain only fixed vocabulary and a bounded numeric protocol code.
// Never propagate protocol error.message/data, event payloads, URLs or exception stacks.
const CDP_METHODS = new Set([
  'Browser.getVersion', 'Target.createTarget', 'Target.attachToTarget', 'Page.enable',
  'Runtime.enable', 'Network.enable', 'Inspector.enable', 'Fetch.enable',
  'Page.addScriptToEvaluateOnNewDocument', 'Page.navigate', 'Runtime.evaluate',
  'Page.handleJavaScriptDialog', 'Fetch.continueRequest', 'Fetch.failRequest',
  'Network.getResponseBody', 'Input.dispatchMouseEvent', 'Input.insertText', 'Page.reload',
]);
const CDP_EVENTS = new Set([
  'Fetch.requestPaused', 'Network.requestWillBeSent', 'Network.responseReceived',
  'Network.loadingFinished', 'Network.loadingFailed', 'Runtime.consoleAPICalled',
  'Runtime.exceptionThrown', 'Page.javascriptDialogOpening', 'Inspector.targetCrashed',
  'Inspector.detached', 'Target.targetCrashed',
]);
const CDP_REASON_TEXT = new Map([
  ['Invalid InterceptionId.', 'invalid_interception_id'],
  ['Invalid InterceptionId', 'invalid_interception_id'],
  ['Invalid interceptionId.', 'invalid_interception_id'],
  ['No resource with given identifier found', 'resource_not_found'],
  ['Session with given id not found.', 'session_not_found'],
  ['Target closed.', 'target_closed'],
]);
const CDP_REASONS = new Set([...CDP_REASON_TEXT.values(), 'unclassified_protocol_error']);
const CDP_ERROR_TYPES = new Set(['Error', 'TypeError', 'RangeError', 'SyntaxError', 'AssertionError']);
function cdpCode(value) {
  return Number.isSafeInteger(value) && Math.abs(value) <= 2147483647 ? value : null;
}
function protocolFailure(method, payload) {
  const command = CDP_METHODS.has(method) ? method : 'other_method';
  const code = cdpCode(payload?.code);
  const reason = CDP_REASON_TEXT.get(payload?.message) || 'unclassified_protocol_error';
  return Object.assign(new Error(`CDP ${command} failed code=${code ?? 'unavailable'} reason=${reason}`),
    {cdpMethod: command, cdpCode: code, cdpReason: reason});
}
function observerDiagnostic(event, error) {
  const name = CDP_EVENTS.has(event?.method) ? event.method : 'other_event';
  const command = CDP_METHODS.has(error?.cdpMethod) ? error.cdpMethod : 'other_method';
  const code = cdpCode(error?.cdpCode);
  const reason = CDP_REASONS.has(error?.cdpReason) ? error.cdpReason : 'non_protocol_error';
  const type = CDP_ERROR_TYPES.has(error?.name) ? error.name : 'other_error';
  return `CDP observer failed event=${name} method=${command} code=${code ?? 'unavailable'} reason=${reason} type=${type}`;
}

class CDP {
  constructor(socket, fault) {
    this.socket = socket; this.next = 0; this.pending = new Map(); this.listeners = new Set();
    this.intentionalClose = false;
    socket.addEventListener('message', event => {
      let value;
      try { value = JSON.parse(event.data); } catch { fault('Malformed CDP message'); this.close(); return; }
      const pending = this.pending.get(value.id);
      if (pending) {
        clearTimeout(pending.timer); this.pending.delete(value.id);
        if (value.error) pending.reject(protocolFailure(pending.method, value.error));
        else pending.resolve(value.result);
      } else for (const listener of this.listeners) {
        try {
          Promise.resolve(listener(value)).catch(error => fault(observerDiagnostic(value, error)));
        } catch (error) {
          fault(observerDiagnostic(value, error));
        }
      }
    });
    const closed = () => { if (!this.intentionalClose) fault('Chrome CDP disconnected'); this.close(); };
    socket.addEventListener('close', closed); socket.addEventListener('error', closed);
  }
  send(method, params = {}, sessionId) {
    assert(this.socket.readyState === WebSocket.OPEN, 'CDP unavailable');
    return new Promise((resolvePromise, reject) => {
      const id = ++this.next;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP ${method} timeout`)); }, 15000);
      this.pending.set(id, {resolve: resolvePromise, reject, timer, method});
      this.socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  close() {
    this.intentionalClose = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer); pending.reject(new Error('CDP closed'));
    }
    this.pending.clear();
    if (this.socket.readyState === WebSocket.OPEN) this.socket.close();
  }
}

export class Smoke {
  constructor(options) {
    this.options = options; this.started = Date.now(); this.children = []; this.secrets = [];
    this.results = []; this.responses = []; this.requests = new Map(); this.controls = new Map();
    this.inflightRequests = new Set(); this.abortedRequestIds = new Set();
    this.faults = []; this.logs = []; this.external = new Set(); this.sequence = 0;
    this.owned = new Map(); this.knownProcesses = new Map(); this.stopping = false;
    this.abort = new AbortController(); this.lifecycle = []; this.coverage = {};
  }
  safe(value) {
    let text = String(value);
    for (const secret of this.secrets) text = text.split(secret).join('[REDACTED]');
    return text.split(/\r?\n/, 1)[0].replace(/[\x00-\x1f\x7f]/g, '').slice(0, 600);
  }
  safeReport(value) {
    let output = JSON.stringify(value, null, 2) + '\n';
    for (const secret of this.secrets) output = output.split(secret).join('[REDACTED]');
    return output;
  }
  record(name, status, detail) {
    this.results.push({name, status, ...(detail ? {detail: this.safe(detail)} : {})});
    this.logs.push(`${status} ${this.safe(name)}${detail ? ` — ${this.safe(detail)}` : ''}`);
  }
  async check(name, work) {
    this.activeCheck = name;
    await work(); this.assertLive(); await this.snapshotOwnedProcesses();
    this.record(name, 'PASS'); this.activeCheck = null;
  }
  assertLive() {
    assert(!this.stopping, 'Smoke interrupted');
    assert(!this.faults.length, this.faults.join('; '));
    assert(this.children.every(child => !child.launchFailed && child.exitCode === null && child.signalCode === null),
      'Owned server/Chrome terminated unexpectedly');
  }
  async until(predicate, label, ms = 15000, cleanup = false) {
    const end = Math.min(Date.now() + ms, cleanup ? Infinity : this.started + MAX_RUN_MS);
    while (Date.now() < end) {
      if (!cleanup) this.assertLive();
      const value = await predicate(); if (value) return value;
      await nextPoll();
    }
    throw new Error(`Timed out: ${label}`);
  }
  environment() {
    return {PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: this.runtime, TMPDIR: this.runtime,
      XDG_CONFIG_HOME: join(this.runtime, 'config'), XDG_CACHE_HOME: join(this.runtime, 'cache'),
      XDG_DATA_HOME: join(this.runtime, 'data'), LANG: 'en_US.UTF-8',
      PYTHONPATH: join(PROJECT, 'src'), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1',
      LANGSMITH_TRACING: 'false', LANGCHAIN_TRACING_V2: 'false'};
  }
  launch(executable, args, stdio, label) {
    assert(!this.stopping, 'No launches during cleanup');
    const child = spawn(executable, args, {cwd: this.runtime, env: this.environment(), detached: true, stdio});
    child.label = label; this.children.push(child);
    child.on('error', () => { child.launchFailed = true; this.faults.push(`${label} spawn error`); });
    child.on('exit', (code, signal) => {
      this.logs.push(`${label} exit code=${code === null ? 'none' : code} signal=${signal || 'none'}`);
      if (!this.stopping) this.faults.push(`${label} exited unexpectedly`);
    });
    child.stdin?.on('error', () => { if (!this.stopping) this.faults.push(`${label} control pipe failed`); });
    if (child.stderr) {
      // Stream only classified diagnostics. Never persist raw stderr, argv, URLs or credentials.
      createInterface({input: child.stderr}).on('line', line => {
        if (/Traceback|(?:^|\W)(?:ERROR|FATAL|FAILED|Killed|OOM)(?:\W|$)|crash|segmentation|assertion failed/i.test(line)) {
          this.logs.push(`${label} error diagnostic (raw text withheld)`);
          // Chrome emits benign platform ERROR diagnostics; exit/crash/CDP/DOM deadlines
          // remain authoritative. Explicit fatal/OOM/traceback states are never filtered away.
          const terminal = label === 'uvicorn' || /Traceback|\bFATAL\b|\bKilled\b|\bOOM\b|segmentation fault|assertion failed/i.test(line);
          if (terminal && !this.stopping) this.faults.push(`${label} terminal diagnostic`);
        }
      });
    }
    return child;
  }
  async remember(path) {
    const info = await lstat(path);
    assert(info.isDirectory() && !info.isSymbolicLink() && (info.mode & 0o077) === 0,
      'Owned directory must be private and not a symlink');
    this.owned.set(path, {dev: info.dev, ino: info.ino});
  }
  async ownedDirectory(path) {
    const info = await lstat(path), expected = this.owned.get(path);
    assert(expected && info.isDirectory() && !info.isSymbolicLink() && (info.mode & 0o077) === 0
      && info.dev === expected.dev && info.ino === expected.ino,
    'Owned directory identity changed; refusing access or recursive removal');
  }
  async start() {
    assert(process.platform === 'darwin', 'This smoke targets actual installed macOS Chrome');
    assert(Number(process.versions.node.split('.')[0]) >= 24 && typeof WebSocket === 'function',
      'Node >=24 with built-in WebSocket is required');
    assert(process.getuid?.() !== 0, 'Do not run this smoke as root');
    const parent = await realpath(this.options.parent);
    assert((await lstat(parent)).isDirectory() && !isWithin(await realpath(PROJECT), parent),
      'Output parent must exist outside source');
    await Promise.all([this.options.chrome, this.options.python].map(path => access(path, constants.X_OK)));
    this.root = await mkdtemp(join(parent, 'scheduling-browser-')); await this.remember(this.root);
    this.runtime = await mkdtemp(join(this.root, 'owned-runtime-')); await this.remember(this.runtime);
    this.appDirectory = join(this.runtime, 'app');
    this.server = this.launch(this.options.python, ['-u', '-c', PY_SERVER, this.appDirectory],
      ['pipe', 'pipe', 'pipe'], 'uvicorn');
    this.bootstrap = await timeout(new Promise((resolvePromise, reject) => {
      createInterface({input: this.server.stdout}).on('line', line => {
        let value;
        try { assert(line.length < 2_000_000); value = JSON.parse(line); }
        catch { reject(new Error('Invalid private bootstrap protocol')); return; }
        if (value.type === 'ready') {
          for (const identity of Object.values(value.identities || {})) {
            if (typeof identity.token === 'string') this.secrets.push(identity.token);
          }
          resolvePromise(value); return;
        }
        if (value.type === 'failed') {
          this.faults.push('Owned TEST server failed'); reject(new Error('Owned TEST server failed')); return;
        }
        const pending = this.controls.get(value.id);
        if (pending) {
          clearTimeout(pending.timer); this.controls.delete(value.id);
          if (value.error) pending.reject(new Error('Private TEST control failed')); else pending.resolve(value.value);
        }
      });
      this.server.once('exit', () => reject(new Error('TEST server exited before readiness')));
      this.server.once('error', () => reject(new Error('Project Python could not launch')));
    }), 30000, 'TEST server startup deadline', this.abort.signal);
    assert(Number.isInteger(this.bootstrap.port) && this.bootstrap.port > 0 && this.bootstrap.port <= 65535,
      'Invalid owned server port');
    assert(this.bootstrap.provider.startsWith('synthetic-test-') && this.secrets.length === 2,
      'Only TEST provider and ephemeral identities allowed');
    this.origin = `http://127.0.0.1:${this.bootstrap.port}`;
    await this.remember(this.appDirectory);
    const health = await this.until(async () => {
      const response = await this.http('/health').catch(() => null);
      return response?.status === 200 && response.body?.synthetic_only === true ? response : null;
    }, 'actual uvicorn HTTP health');
    assert.equal(health.body.provider, this.bootstrap.provider, 'TEST health identity mismatch');
    this.profile = await mkdtemp(join(this.runtime, 'new-chrome-profile-')); await this.remember(this.profile);
    this.chrome = this.launch(this.options.chrome, [
      '--headless=new', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
      `--user-data-dir=${this.profile}`, '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-default-apps', '--disable-sync',
      '--disable-breakpad', '--disable-crash-reporter', '--disable-domain-reliability',
      '--safebrowsing-disable-auto-update', '--disable-client-side-phishing-detection',
      '--metrics-recording-only', '--password-store=basic', '--use-mock-keychain',
      '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter,Translate,PasswordLeakDetection',
      '--window-size=1500,1200', 'about:blank',
    ], ['ignore', 'ignore', 'pipe'], 'Chrome');
    await this.snapshotOwnedProcesses();
    const active = await this.until(async () => {
      try {
        const path = join(this.profile, 'DevToolsActivePort');
        const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
        try {
          const info = await handle.stat(); assert(info.isFile() && info.size <= 1024, 'DevTools port file bound');
          const lines = (await handle.readFile('utf8')).trim().split('\n');
          return /^\d+$/.test(lines[0]) && Number(lines[0]) > 0 && Number(lines[0]) <= 65535
            && /^\/devtools\/browser\/[A-Za-z0-9-]+$/.test(lines[1] || '') ? lines : null;
        } finally { await handle.close(); }
      } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
    }, 'new-profile DevToolsActivePort', 30000);
    this.devtoolsPort = Number(active[0]);
    const socket = new WebSocket(`ws://127.0.0.1:${this.devtoolsPort}${active[1]}`);
    this.socket = socket;
    await timeout(new Promise((resolvePromise, reject) => {
      socket.addEventListener('open', resolvePromise, {once: true});
      socket.addEventListener('error', () => reject(new Error('Chrome CDP connection failed')), {once: true});
      socket.addEventListener('close', () => reject(new Error('Chrome CDP closed before ready')), {once: true});
    }), 10000, 'Chrome CDP connection deadline', this.abort.signal);
    this.cdp = new CDP(socket, message => this.faults.push(message));
    const version = await this.cdp.send('Browser.getVersion');
    assert(/Chrome\//.test(version.product), 'Actual Chrome product not reported');
    this.chromeVersion = version.product;
    const target = await this.cdp.send('Target.createTarget', {url: 'about:blank'});
    this.targetId = target.targetId;
    this.sessionId = (await this.cdp.send('Target.attachToTarget', {targetId: target.targetId, flatten: true})).sessionId;
    this.cdp.listeners.add(event => this.observe(event));
    await this.cmd('Page.enable'); await this.cmd('Runtime.enable'); await this.cmd('Network.enable');
    await this.cmd('Inspector.enable');
    await this.cmd('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
    await this.cmd('Page.addScriptToEvaluateOnNewDocument', {source: `(() => {
      globalThis.__smokeInnerHTMLWrites = 0;
      const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
      Object.defineProperty(Element.prototype, 'innerHTML', {...descriptor, set(value) {
        globalThis.__smokeInnerHTMLWrites++; return descriptor.set.call(this, value);
      }});
    })();`});
    await this.cmd('Page.navigate', {url: this.origin + '/'});
    await this.until(() => this.evaluate(`document.readyState === 'complete' && !!document.querySelector('#login-form')`),
      'original scheduling login DOM');
    this.record('actual_installed_Chrome_and_TEST_uvicorn', 'PASS', version.product);
  }
  cmd(method, params = {}) { return this.cdp.send(method, params, this.sessionId); }
  async evaluate(expression) {
    const result = await this.cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    assert(!result.exceptionDetails, 'Browser evaluation failed (payload withheld)');
    return result.result?.value;
  }
  async observe(event) {
    if (event.method === 'Target.targetCrashed' && event.params.targetId === this.targetId) {
      this.faults.push('Chrome target crashed'); return;
    }
    if (event.sessionId !== this.sessionId) return;
    const p = event.params;
    if (event.method === 'Inspector.targetCrashed' || event.method === 'Inspector.detached')
      if (!this.stopping) this.faults.push('Chrome target crashed or detached');
    if (event.method === 'Runtime.exceptionThrown') this.faults.push('Uncaught browser exception');
    if (event.method === 'Runtime.consoleAPICalled') {
      // Arguments stay in memory only, used solely to detect a credential leak.
      if (this.secrets.some(secret => JSON.stringify(p.args).includes(secret))) this.faults.push('Token in browser console');
      this.logs.push(`browser console ${/^[a-zA-Z]+$/.test(p.type) ? p.type : 'event'} (arguments withheld)`);
      if (p.type === 'error' || p.type === 'assert') this.faults.push('Browser console error/assert');
    }
    if (event.method === 'Page.javascriptDialogOpening') {
      this.faults.push('Unexpected browser dialog'); await this.cmd('Page.handleJavaScriptDialog', {accept: false});
    }
    if (event.method === 'Fetch.requestPaused') {
      const url = new URL(p.request.url);
      if (url.origin !== this.origin) {
        this.external.add('non_same_origin_request');
        await this.cmd('Fetch.failRequest', {requestId: p.requestId, errorReason: 'BlockedByClient'});
      } else await this.cmd('Fetch.continueRequest', {requestId: p.requestId});
    }
    if (event.method === 'Network.requestWillBeSent') {
      this.inflightRequests.add(p.requestId);
      const url = new URL(p.request.url);
      if (url.origin !== this.origin && url.protocol !== 'about:') this.external.add('non_same_origin_request');
      if (this.secrets.some(secret => p.request.url.includes(secret) || (p.request.postData || '').includes(secret)))
        this.faults.push('Token in URL or request body');
      const authorization = Object.entries(p.request.headers || {}).filter(([key]) => key.toLowerCase() === 'authorization');
      if (authorization.length && url.origin !== this.origin) this.faults.push('Authorization sent outside owned origin');
      // Header values, in particular Authorization, are never stored.
      this.requests.set(p.requestId, {path: url.pathname, origin: url.origin, method: p.request.method,
        rawRequest: p.request.postData, authorization: authorization.length > 0});
    }
    if (event.method === 'Network.responseReceived') {
      const request = this.requests.get(p.requestId);
      if (request) {
        const headers = {};
        for (const [key, value] of Object.entries(p.response.headers)) {
          if (['cache-control', 'content-security-policy', 'x-content-type-options', 'referrer-policy', 'x-frame-options']
            .includes(key.toLowerCase())) headers[key.toLowerCase()] = value;
          if (key.toLowerCase() === 'set-cookie') this.faults.push('Unexpected Set-Cookie');
        }
        this.responses.push({...request, requestId: p.requestId, status: p.response.status, headers,
          cached: Boolean(p.response.fromDiskCache || p.response.fromServiceWorker), done: false});
      }
    }
    if (event.method === 'Network.loadingFailed') {
      this.inflightRequests.delete(p.requestId); this.abortedRequestIds.add(p.requestId);
      const row = this.responses.find(item => item.requestId === p.requestId && !item.done);
      if (row) { row.done = true; row.aborted = true; row.body = null; }
      if (!this.allowAborted && !this.stopping) this.faults.push('Browser request failed or aborted');
    }
    if (event.method === 'Network.loadingFinished') {
      this.inflightRequests.delete(p.requestId);
      const row = this.responses.find(item => item.requestId === p.requestId && !item.done);
      if (!row) return;
      if (row.path.startsWith('/api/') || row.path === '/health') {
        try {
          const response = await this.cmd('Network.getResponseBody', {requestId: p.requestId});
          const raw = response.base64Encoded ? Buffer.from(response.body, 'base64').toString('utf8') : response.body;
          assert(raw.length <= 2_000_000, 'API body bound');
          if (this.secrets.some(secret => raw.includes(secret))) this.faults.push('Token in API response');
          row.body = JSON.parse(raw);
        } catch { row.body = null; }
      }
      row.done = true;
    }
  }
  async click(selector) {
    const point = await this.evaluate(`(() => {
      const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled || e.closest('[hidden]')) return null;
      e.scrollIntoView({block:'center', behavior:'instant'});
      const r = e.getBoundingClientRect(), x = r.x + r.width/2, y = r.y + r.height/2;
      if (!r.width || !r.height || !e.contains(document.elementFromPoint(x,y))) return null;
      return {x,y};
    })()`);
    assert(point, `Not a visible enabled click target: ${selector}`);
    await this.cmd('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...point});
    await this.cmd('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...point});
  }
  async fill(selector, value) {
    assert(await this.evaluate(`(() => {const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled || e.closest('[hidden]')) return false;
      e.scrollIntoView({block:'center', behavior:'instant'}); e.focus(); e.select(); return true;})()`),
    `Missing visible input: ${selector}`);
    await this.cmd('Input.insertText', {text: String(value)});
  }
  async choose(selector, value) {
    assert(await this.evaluate(`(() => {const e = document.querySelector(${JSON.stringify(selector)});
      if (!e || e.disabled || e.closest('[hidden]') || ![...e.options].some(o => o.value === ${JSON.stringify(value)})) return false;
      e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event('change', {bubbles:true})); return true;
    })()`), `Cannot choose option: ${selector}`);
    await this.idle();
  }
  async idle() {
    await this.until(() => this.evaluate(`!!document.querySelector('#workbench[aria-busy="false"]')`),
      'workbench aria-busy=false');
  }
  async responseAfter(mark, path, method = 'POST', ms = 45000) {
    sameOriginPath(path);
    return this.until(() => this.responses.slice(mark).find(row => row.path === path && row.method === method && row.done),
      `${method} ${path} response completed`, ms);
  }
  async submit(selector, path, method = 'POST', expected = 200) {
    await this.idle(); const mark = this.responses.length;
    await this.click(selector);
    const response = await this.responseAfter(mark, path, method); await this.idle();
    assert.equal(response.status, expected, `UI ${method} response HTTP status`);
    if (expected === 200) {
      assert(response.body, 'Actual backend response body unavailable');
      assert(await this.evaluate(`document.querySelector('#error-message').hidden && document.querySelector('#stale-banner').hidden`),
        'Successful response did not leave a clear, current UI state');
    }
    return response;
  }
  requestBody(response, keys) {
    const body = JSON.parse(response.rawRequest);
    assert.deepEqual(Object.keys(body).sort(), [...keys].sort(), 'UI request has extra or missing fields');
    return body;
  }
  // Read-only browser probes never create, run, approve, publish, deliver or execute.
  async probe(path, identity = 'owner') {
    sameOriginPath(path);
    const headers = identity ? {Authorization: `Bearer ${this.bootstrap.identities[identity].token}`} : {};
    return this.evaluate(`(async () => {const r = await fetch(${JSON.stringify(path)}, {
      headers:${JSON.stringify(headers)}, credentials:'omit', mode:'same-origin', redirect:'error', cache:'no-store'});
      return {status:r.status, body:await r.json()};})()`);
  }
  // Node's bare numeric-loopback socket is used for health and labelled forbidden-origin/role probes.
  // No proxy, caller URL, DNS resolution, redirects, cookies or automatic retries.
  async http(path, {method = 'GET', identity = null, body, origin} = {}) {
    sameOriginPath(path);
    return new Promise((resolvePromise, reject) => {
      const raw = body === undefined ? undefined : JSON.stringify(body);
      const headers = {};
      if (identity) headers.Authorization = `Bearer ${this.bootstrap.identities[identity].token}`;
      if (origin) headers.Origin = origin;
      if (raw !== undefined) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = Buffer.byteLength(raw); }
      const request = httpRequest({hostname: '127.0.0.1', port: this.bootstrap.port, path, method,
        headers, agent: false, timeout: 3000}, response => {
        let data = '';
        response.setEncoding('utf8');
        response.on('data', chunk => { data += chunk; if (data.length > 2_000_000) request.destroy(new Error('HTTP response bound')); });
        response.on('end', () => { try { resolvePromise({status: response.statusCode, body: JSON.parse(data)}); }
          catch { reject(new Error('HTTP JSON unavailable')); } });
        response.on('error', () => reject(new Error('Loopback response failed')));
      });
      request.on('timeout', () => request.destroy(new Error('Loopback HTTP timeout')));
      request.on('error', () => reject(new Error('Loopback HTTP request failed')));
      request.end(raw);
    });
  }
  async control(command) {
    const id = ++this.sequence;
    return new Promise((resolvePromise, reject) => {
      const timer = setTimeout(() => { this.controls.delete(id); reject(new Error('Private control deadline')); }, 5000);
      this.controls.set(id, {resolve: resolvePromise, reject, timer});
      this.server.stdin.write(JSON.stringify({id, command}) + '\n');
    });
  }
  async text(selector) { return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent || ''`); }
  async disabled(selector) { return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.disabled === true`); }
  count(path) { return [...this.requests.values()].filter(row => row.path === path && row.method === 'POST').length; }
  async rows(selector) {
    return this.evaluate(`[...document.querySelectorAll(${JSON.stringify(selector + ' tbody tr')})].map(row =>
      [...row.querySelectorAll('td')].map(cell => cell.textContent))`);
  }
  async storageCheck() {
    const state = await this.evaluate(`(async () => ({local:localStorage.length, session:sessionStorage.length,
      cookies:document.cookie, databases:await indexedDB.databases(), caches:await caches.keys(),
      workers:(await navigator.serviceWorker.getRegistrations()).length,
      tokenInput:document.querySelector('#token').value,
      dom:document.documentElement.outerHTML}))()`);
    assert(state.local === 0 && state.session === 0 && state.cookies === '' && state.databases.length === 0
      && state.caches.length === 0 && state.workers === 0 && state.tokenInput === '',
    'Current-origin DOM storage or token input not empty');
    assert(!this.secrets.some(secret => state.dom.includes(secret)), 'Token rendered into DOM');
  }

  async clearState() {
    await this.idle();
    await this.until(() => this.evaluate(`(() => {
      const hidden = document.querySelector('#workbench').hidden && !document.querySelector('#login-panel').hidden;
      const selects = ['snapshot-select','task-select','recommendation-select','publication-select'];
      const fields = ['token','scenario-json','review-note','supplement-note','cancel-publication-note','reconcile-note'];
      return hidden && selects.every(id => document.getElementById(id).value === '' && document.getElementById(id).options.length === 1)
        && fields.every(id => document.getElementById(id).value === '')
        && document.querySelector('#identity').textContent === ''
        && !document.querySelector('#gantt-chart svg, #resource-chart svg, #inventory-chart svg')
        && !document.querySelector('#publication-confirm').checked && !document.querySelector('#execute-confirm').checked;
    })()`), 'logout clear-state DOM and no retained evidence');
    await this.storageCheck();
  }
  async logout() {
    await this.idle(); await this.click(SELECTORS.logout); await this.clearState();
  }
  async login(identity = 'owner', role = this.bootstrap.identities[identity].role) {
    await this.fill(SELECTORS.token, this.bootstrap.identities[identity].token);
    const response = await this.submit(SELECTORS.login, '/api/me', 'GET');
    assert.equal(response.body.user_id, this.bootstrap.identities[identity].user_id, 'Actual login owner');
    assert.equal(response.body.role, role, 'Actual login role');
    await this.until(() => this.evaluate(`!document.querySelector('#workbench').hidden && document.querySelector('#login-panel').hidden`),
      'authenticated workbench visible');
    assert((await this.text('#provider-summary')).includes(this.bootstrap.provider), 'TEST provider disclosure');
    await this.storageCheck();
  }
  async status(selector, expected) {
    const value = await this.text(selector);
    assert(value.includes(STATUS_TEXT[expected]), `UI status not observed: ${expected}`);
  }
  async taskFacts(task) {
    const values = await this.evaluate(`[...document.querySelectorAll('#task-summary dd')].map(e => e.textContent)`);
    assert.equal(values[0], task.object_id, 'Task ID rendered from backend');
    assert(values[1].includes(STATUS_TEXT[task.status]), 'Task status rendered from backend');
    for (const [index, field] of [[3, 'calls'], [4, 'tools'], [5, 'tokens'], [6, 'unknown_calls']])
      assert.equal(values[index], String(task.payload[field]), `Task ${field} equals backend`);
  }
  async createBaseline(fixture) {
    await this.choose(SELECTORS.snapshot, fixture.object_id);
    assert(await this.evaluate(`document.querySelector('#scenario-select').value === ''`), 'Scenario requires explicit selection');
    await this.choose(SELECTORS.scenario, 'baseline');
    const scenario = await this.evaluate(`JSON.parse(document.querySelector('#scenario-json').value)`);
    assert.equal(scenario.kind, 'baseline', 'Explicit baseline kind');
    const response = await this.submit(SELECTORS.create, '/api/tasks');
    const request = this.requestBody(response, ['snapshot_id', 'scenario', 'action_id']);
    assert.equal(request.snapshot_id, fixture.object_id);
    assert.deepEqual(request.scenario, scenario, 'UI submitted exactly authorized scenario');
    const task = response.body;
    assert.equal(task.status, 'created'); assert.equal(task.payload.calls, 0); assert.equal(task.payload.tools, 0);
    assert.equal(this.count(`/api/tasks/${task.object_id}/run`), 0, 'Creation must not auto-run');
    await this.taskFacts(task); return task;
  }
  async completedTask(task, expectedCalls, expectedTools) {
    assert.equal(task.status, 'submitted', 'Actual backend task submitted');
    assert.equal(task.payload.calls, expectedCalls, 'Observed calls match issued workflow');
    assert.equal(task.payload.tools, expectedTools, 'Observed tool count includes submit, not nine-tool vocabulary');
    assert.equal(task.payload.unknown_calls, 0, 'No unknown TEST calls');
    assert.deepEqual(Object.keys(task.payload.evidence).sort(), [...COMPUTATION].sort(), 'Exactly seven computation envelopes');
    assert.equal(task.payload.provider_fingerprint, this.bootstrap.provider);
    for (const [tool, record] of Object.entries(task.payload.evidence)) {
      assert.equal(record.tool, tool); assert(record.evidence_id && /^[a-f0-9]{64}$/.test(record.hash), 'Issued evidence envelope');
    }
    await this.taskFacts(task);
    const chain = await this.evaluate(`[...document.querySelectorAll('#tool-chain li.done')].map(e => e.dataset.tool)`);
    assert.deepEqual(chain, [...COMPUTATION, 'submit_schedule'], 'UI seven computation stages plus submit');
    const audit = await this.probe('/api/audit'); assert.equal(audit.status, 200);
    const tools = audit.body.items.filter(item => item.object_id === task.object_id && item.action === 'tool_completed')
      .map(item => item.detail.tool).reverse();
    const expected = expectedCalls === 10 ? ['get_snapshot', 'request_human_input', ...COMPUTATION, 'submit_schedule']
      : [...COMPUTATION, 'submit_schedule'];
    assert.deepEqual(tools, expected, 'Actual persisted tool sequence, not vocabulary size');
    return tools;
  }
  async publicationFacts(publication, stage) {
    const p = publication.payload;
    await this.status('#publication-summary', publication.status);
    const values = await this.evaluate(`[...document.querySelectorAll('#publication-summary dd')].map(e => e.textContent)`);
    assert.equal(values[0], publication.object_id);
    for (const [index, key] of [[3,'delivery_status'], [4,'receiver_status'], [5,'execution_status']])
      assert(values[index].includes(STATUS_TEXT[p[key]]), `UI publication ${key} equals backend`);
    assert.equal(values[6], String(p.receiver_sequence), 'UI receiver sequence');
    this.lifecycle.push({stage, status: publication.status, delivery_status: p.delivery_status,
      receiver_status: p.receiver_status, execution_status: p.execution_status,
      receiver_sequence: p.receiver_sequence, accepted_operations: p.accepted_operation_ids.length});
  }
  async checkGantt(rec) {
    const plan = rec.payload.candidate, horizon = rec.payload.compiled_problem.snapshot.horizon_min;
    assert(plan.slots.length > 0 && rec.payload.validation.valid === true, 'Backend nonempty independently valid plan');
    const rows = await this.rows('#plan-slots');
    assert.deepEqual(rows, plan.slots.map(slot => [slot.operation_id, slot.machine_id, String(slot.setup_start_min),
      String(slot.start_min), String(slot.end_min), slot.predecessor_id ?? '未提供']), 'Exact plan slot table equals backend');
    const bars = await this.evaluate(`[...document.querySelectorAll('#gantt-chart svg rect')].map(e => ({
      kind:e.getAttribute('class'), title:e.querySelector('title')?.textContent,
      x:Number(e.getAttribute('x')), width:Number(e.getAttribute('width'))}))`);
    const expected = [];
    for (const machine of rec.payload.compiled_problem.snapshot.machines) for (const slot of plan.slots.filter(s => s.machine_id === machine.machine_id))
      for (const [kind, start, end, word] of [['setup', slot.setup_start_min, slot.start_min, '换型'],
        ['processing', slot.start_min, slot.end_min, '加工']]) if (start !== end)
        expected.push({kind, title:`${machine.machine_id} / ${slot.operation_id} ${word} [${start}, ${end}) 分钟`,
          x:140 + 810 * start / horizon, width:810 * (end - start) / horizon});
    assert(bars.length > 0); assert.deepEqual(bars, expected, 'Actual SVG Gantt intervals and geometry equal backend');
    this.coverage.gantt = {slots: plan.slots.length, bars: bars.length, source: 'recommendation.payload.candidate.slots'};
  }
  async checkResources(rec) {
    const p = rec.payload, snapshot = p.compiled_problem.snapshot, activities = p.validation.activities.filter(a => a.resource_kind === 'resource');
    assert(activities.length > 0 && snapshot.resources.length > 0, 'Nonempty backend resource activity');
    assert.deepEqual(await this.rows('#resource-table'), activities.map(a => [a.resource_id === 'operator' ? '操作员' : a.resource_id, a.operation_id,
      a.kind === 'setup' ? '换型' : '加工', String(a.start_min), String(a.end_min), String(a.demand)]), 'Resource table equals backend activities');
    const dots = await this.evaluate(`[...document.querySelectorAll('#resource-chart svg circle')].map(e => ({
      title:e.querySelector('title')?.textContent, x:Number(e.getAttribute('cx')), y:Number(e.getAttribute('cy'))}))`);
    const expected = [], paths = [];
    snapshot.resources.forEach((resource, index) => {
      const deltas = new Map([[0, 0]]);
      for (const a of activities.filter(a => a.resource_id === resource.resource_id)) {
        deltas.set(a.start_min, (deltas.get(a.start_min) || 0) + a.demand);
        deltas.set(a.end_min, (deltas.get(a.end_min) || 0) - a.demand);
      }
      let balance = 0;
      const points = [...deltas].sort((a,b) => a[0]-b[0]).map(([at_min, change]) => ({at_min, balance:balance += change}));
      const max = Math.max(resource.capacity, ...points.map(point => point.balance), 1);
      const x = minute => 140 + 810 * minute / snapshot.horizon_min, y = value => 28 + index*100 + 65 - 55*value/max;
      let path = `M ${x(0)} ${y(0)}`;
      for (const point of points) {
        expected.push({title:`${resource.resource_id} / ${point.at_min} 分钟 / 占用 ${point.balance}`, x:x(point.at_min), y:y(point.balance)});
        path += ` H ${x(point.at_min)} V ${y(point.balance)}`;
      }
      paths.push(`${path} H ${x(snapshot.horizon_min)}`);
    });
    assert.deepEqual(dots, expected, 'Resource SVG points equal backend half-open occupancy sweep');
    assert.deepEqual(await this.evaluate(`[...document.querySelectorAll('#resource-chart svg path.trace')].map(e => e.getAttribute('d'))`), paths,
      'Actual resource step lines equal backend occupancy');
    this.coverage.resources = {activities: activities.length, points: dots.length, source: 'recommendation.payload.validation.activities'};
  }
  async checkInventory(rec) {
    const p = rec.payload, snapshot = p.compiled_problem.snapshot, inventory = p.validation.inventory;
    assert(inventory.length > 0 && inventory.some(point => point.received || point.consumed), 'Actual eventful backend inventory');
    assert.deepEqual(await this.rows('#inventory-table'), inventory.map(point => [point.item_id, String(point.at_min),
      String(point.received), String(point.consumed), String(point.balance)]), 'Inventory table equals backend event points');
    const dots = await this.evaluate(`[...document.querySelectorAll('#inventory-chart svg circle')].map(e => ({
      title:e.querySelector('title')?.textContent, x:Number(e.getAttribute('cx')), y:Number(e.getAttribute('cy'))}))`);
    const expected = [], paths = [];
    snapshot.materials.forEach((material, index) => {
      const points = inventory.filter(point => point.item_id === material.item_id);
      const max = Math.max(material.initial_stock, ...points.map(point => point.balance), 1), min = Math.min(0, ...points.map(point => point.balance));
      const x = minute => 140 + 810 * minute / snapshot.horizon_min, y = value => 25 + index*100 + 67 - 54*(value-min)/(max-min);
      const unit = snapshot.items.find(item => item.item_id === material.item_id).unit;
      let path = `M ${x(0)} ${y(material.initial_stock)}`;
      for (const point of points) {
        expected.push({title:`${point.item_id} @${point.at_min}分钟：收料 ${point.received} / 扣料 ${point.consumed} / 余额 ${point.balance} ${unit}`,
          x:x(point.at_min), y:y(point.balance)});
        path += ` H ${x(point.at_min)} V ${y(point.balance)}`;
      }
      paths.push(`${path} H ${x(snapshot.horizon_min)}`);
    });
    assert.deepEqual(dots, expected, 'Inventory SVG points equal authoritative event balances');
    assert.deepEqual(await this.evaluate(`[...document.querySelectorAll('#inventory-chart svg path.trace')].map(e => e.getAttribute('d'))`), paths,
      'Actual inventory step lines equal backend receipt/deduction events');
    this.coverage.inventory = {points: dots.length, source: 'recommendation.payload.validation.inventory'};
  }
  async workflow() {
    await this.check('anonymous_and_invalid_token_refused', async () => {
      await this.clearState();
      const anonymous = await this.http('/api/me'); assert.equal(anonymous.status, 401);
      // Invalid login deliberately causes concurrent GETs to be aborted by the real logout handler.
      this.allowAborted = true;
      const mark = this.responses.length;
      await this.fill(SELECTORS.token, 'invalid-smoke-token'); await this.click(SELECTORS.login);
      await this.until(async () => this.responses.slice(mark).some(row => row.status === 401 && row.path.startsWith('/api/')),
        'actual invalid-login 401 (parallel refresh may abort sibling requests)');
      await this.clearState();
      await this.until(async () => this.inflightRequests.size === 0, 'invalid-login aborted requests drained');
      this.allowAborted = false;
    });
    await this.check('real_identity_and_memory_only_token', async () => {
      await this.login();
    });
    await this.check('UI_loads_original_backend_fixtures_without_auto_selection', async () => {
      const fixtureRows = this.responses.find(row => row.path === '/api/snapshots' && row.status === 200 && row.body)?.body.items;
      assert(fixtureRows && fixtureRows.length === Object.keys(this.bootstrap.fixtures).length, 'Actual fixture GET');
      const options = await this.evaluate(`[...document.querySelector('#snapshot-select').options].filter(o => o.value).map(o => o.value).sort()`);
      assert.deepEqual(options, Object.values(this.bootstrap.fixtures).map(fixture => fixture.object_id).sort(), 'UI fixture options equal backend');
      assert(await this.evaluate(`document.querySelector('#snapshot-select').value === '' && document.querySelector('#scenario-select').value === ''`),
        'No automatic source/scenario selection');
      for (const fixture of Object.values(this.bootstrap.fixtures)) {
        const loaded = fixtureRows.find(row => row.object_id === fixture.object_id);
        assert.deepEqual(loaded.payload, fixture.snapshot, 'Original synthetic fixture loaded unchanged');
      }
      this.coverage.fixtures = {loaded: fixtureRows.length, source: 'original demo_snapshots -> actual GET /api/snapshots -> UI select'};
    });
    let task, recommendation, publication;
    await this.check('explicit_baseline_create_is_not_run', async () => {
      task = await this.createBaseline(this.bootstrap.fixtures.normal);
      assert((await this.text('#snapshot-summary')).includes(String(this.bootstrap.fixtures.normal.snapshot.observed_at_ms)));
    });
    await this.check('baseline_actual_run_has_eight_calls_seven_computations_plus_submit', async () => {
      const response = await this.submit(SELECTORS.run, `/api/tasks/${task.object_id}/run`);
      this.requestBody(response, []); task = response.body;
      const sequence = await this.completedTask(task, 8, 8);
      this.coverage.baseline = {status:task.status, calls:task.payload.calls, tools:task.payload.tools,
        evidence:Object.keys(task.payload.evidence).length, sequence};
    });
    await this.check('recommendation_pending_is_not_approval_or_publication', async () => {
      const response = await this.probe(`/api/recommendations/${task.payload.recommendation_id}`);
      assert.equal(response.status, 200); recommendation = response.body;
      assert.equal(recommendation.status, 'pending_review'); assert.equal(recommendation.payload.review, null);
      await this.choose(SELECTORS.recommendation, recommendation.object_id);
      await this.status('#recommendation-summary', 'pending_review');
      assert.equal((await this.probe('/api/publications')).body.items.length, 0);
      assert(await this.disabled(SELECTORS.publish) && await this.disabled(SELECTORS.execute));
      this.lifecycle.push({stage:'recommendation', status:recommendation.status});
    });
    await this.check('Gantt_nonempty_intervals_equal_backend', () => this.checkGantt(recommendation));
    await this.check('resources_nonempty_occupancy_equal_backend', () => this.checkResources(recommendation));
    await this.check('inventory_nonempty_event_steps_equal_backend', () => this.checkInventory(recommendation));
    await this.check('independent_validation_and_selected_synthetic_evidence', async () => {
      const p = recommendation.payload;
      assert(p.validation.valid && p.validation.violations.length === 0);
      assert.equal(p.simulation.synthetic, true); assert.equal(p.simulation.status, 'completed');
      assert.equal(p.simulation.plan_hash, p.validation.plan_hash);
      const solve = task.payload.evidence.solve_schedule.payload;
      assert(['FEASIBLE','OPTIMAL'].includes(solve.baseline.status));
      assert(['FEASIBLE','OPTIMAL'].includes(solve.optimized.status));
      assert((await this.text('#solver-summary')).includes(solve.baseline.status));
      assert((await this.text('#solver-summary')).includes(solve.optimized.status));
      assert((await this.text('#plan-binding')).includes(p.validation.plan_hash));
      this.coverage.solver = {baseline:solve.baseline.status, optimized:solve.optimized.status,
        validation:p.validation.valid, simulation:p.simulation.status, synthetic:p.simulation.synthetic};
    });
    await this.check('operator_UI_and_API_cannot_review', async () => {
      await this.logout(); await this.control('owner_operator'); await this.login('owner', 'operator');
      await this.choose(SELECTORS.recommendation, recommendation.object_id);
      assert(await this.disabled(SELECTORS.review) && await this.disabled(SELECTORS.decision));
      const refused = await this.http(`/api/recommendations/${recommendation.object_id}/review`, {method:'POST', identity:'owner',
        origin:this.origin, body:{action_id:'browser-negative-role', recommendation_hash:recommendation.content_hash,
          decision:'approved', note:'negative role probe: must not approve'}});
      assert.equal(refused.status, 403, 'Server independently enforces reviewer role');
      assert.equal((await this.probe(`/api/recommendations/${recommendation.object_id}`)).body.status, 'pending_review');
      await this.logout(); await this.control('owner_admin');
    });
    await this.check('other_owner_lists_and_direct_object_read_are_isolated', async () => {
      await this.login('other');
      for (const selector of [SELECTORS.snapshot, SELECTORS.task, SELECTORS.recommendation, SELECTORS.publication])
        assert(await this.evaluate(`document.querySelector(${JSON.stringify(selector)}).options.length === 1`), 'Other owner list not empty');
      const refused = await this.http(`/api/recommendations/${recommendation.object_id}`, {identity:'other'});
      assert([403,404].includes(refused.status), 'Cross-owner direct read rejected');
      await this.logout(); await this.login(); await this.choose(SELECTORS.recommendation, recommendation.object_id);
    });
    await this.check('strict_same_origin_mutations_reject_different_loopback_port', async () => {
      const foreignOrigin = `http://127.0.0.1:${this.bootstrap.port === 65535 ? 65534 : this.bootstrap.port + 1}`;
      const refused = await this.http(`/api/recommendations/${recommendation.object_id}/review`, {method:'POST', identity:'owner',
        origin:foreignOrigin, body:{action_id:'browser-negative-origin', recommendation_hash:recommendation.content_hash,
          decision:'approved', note:'negative origin probe: must not approve'}});
      assert.equal(refused.status, 403); assert.equal(refused.body.error.code, 'cross_origin');
      assert.equal((await this.probe(`/api/recommendations/${recommendation.object_id}`)).body.status, 'pending_review');
    });
    await this.check('review_requires_explicit_decision_and_note', async () => {
      assert(await this.disabled(SELECTORS.review));
      await this.choose(SELECTORS.decision, 'approved'); assert(await this.disabled(SELECTORS.review));
      await this.fill(SELECTORS.note, XSS_NOTE);
      assert(!(await this.disabled(SELECTORS.review)));
    });
    await this.check('approval_is_separate_hash_bound_action_without_publication', async () => {
      const response = await this.submit(SELECTORS.review, `/api/recommendations/${recommendation.object_id}/review`);
      const request = this.requestBody(response, ['action_id','recommendation_hash','decision','note']);
      assert.equal(request.recommendation_hash, recommendation.content_hash); assert.equal(request.decision, 'approved');
      assert.equal(request.note, XSS_NOTE); recommendation = response.body;
      assert.equal(recommendation.status, 'approved'); assert.equal(recommendation.payload.review.note, XSS_NOTE);
      await this.status('#recommendation-summary', 'approved');
      assert.equal((await this.probe('/api/publications')).body.items.length, 0);
      assert(await this.disabled(SELECTORS.publish));
      this.lifecycle.push({stage:'review', status:recommendation.status});
    });
    await this.check('publication_requires_its_own_confirmation_without_delivery', async () => {
      assert(await this.evaluate(`!document.querySelector('#publication-confirm').checked`));
      await this.click(SELECTORS.publishConfirm);
      const response = await this.submit(SELECTORS.publish, `/api/recommendations/${recommendation.object_id}/request-publication`);
      const request = this.requestBody(response, ['action_id','recommendation_hash','confirmed']);
      assert.equal(request.recommendation_hash, recommendation.content_hash); assert.equal(request.confirmed, true);
      publication = response.body; assert.equal(publication.status, 'requested');
      assert.equal(publication.payload.delivery_status, 'not_sent'); assert.equal(publication.payload.receiver_status, 'none');
      assert.equal(publication.payload.execution_status, 'none'); assert.deepEqual(publication.payload.accepted_operation_ids, []);
      assert.equal(this.count(`/api/publications/${publication.object_id}/deliver-local`), 0);
      await this.publicationFacts(publication, 'request-publication');
    });
    await this.check('first_delivery_observes_partial_not_execution', async () => {
      const response = await this.submit(SELECTORS.deliver, `/api/publications/${publication.object_id}/deliver-local`);
      this.requestBody(response, ['action_id']); publication = response.body;
      assert.equal(publication.status, 'partial'); assert.equal(publication.payload.receiver_status, 'partial');
      assert.equal(publication.payload.execution_status, 'none');
      assert(publication.payload.accepted_operation_ids.length > 0
        && publication.payload.accepted_operation_ids.length < recommendation.payload.candidate.slots.length);
      assert(await this.disabled(SELECTORS.execute)); await this.publicationFacts(publication, 'deliver-local');
    });
    await this.check('advance_observes_full_acceptance_without_execution', async () => {
      const response = await this.submit(SELECTORS.advance, `/api/publications/${publication.object_id}/advance-receiver`);
      this.requestBody(response, ['action_id']); publication = response.body;
      assert.equal(publication.status, 'accepted'); assert.equal(publication.payload.execution_status, 'none');
      assert.deepEqual([...publication.payload.accepted_operation_ids].sort(), recommendation.payload.candidate.slots.map(s => s.operation_id).sort());
      assert(await this.disabled(SELECTORS.execute)); await this.publicationFacts(publication, 'advance-receiver');
    });
    await this.check('independent_acceptance_readback_does_not_execute', async () => {
      const previous = publication;
      const response = await this.submit(SELECTORS.readback, `/api/publications/${publication.object_id}/readback`);
      this.requestBody(response, ['action_id']); publication = response.body;
      assert.equal(publication.status, 'accepted'); assert.equal(publication.payload.execution_status, 'none');
      assert.equal(publication.payload.receiver_sequence, previous.payload.receiver_sequence);
      assert((await this.text('#readback-summary')).includes('本页已独立请求回读'));
      assert.equal(this.count(`/api/publications/${publication.object_id}/execute-synthetic`), 0);
      await this.publicationFacts(publication, 'readback-before-execute');
    });
    await this.check('separately_confirmed_synthetic_execution_observes_completed_events', async () => {
      assert(await this.evaluate(`!document.querySelector('#execute-confirm').checked`));
      await this.click(SELECTORS.executeConfirm);
      const response = await this.submit(SELECTORS.execute, `/api/publications/${publication.object_id}/execute-synthetic`);
      assert.equal(this.requestBody(response, ['action_id','confirmed']).confirmed, true); publication = response.body;
      assert.equal(publication.status, 'completed'); assert.equal(publication.payload.execution_status, 'completed');
      const result = publication.payload.execution_result;
      assert.equal(result.synthetic, true); assert.equal(result.status, 'completed'); assert(result.events.length > 0);
      assert((await this.text('#execution-summary')).includes(String(result.events[0].kind)), 'Backend execution event visibly rendered');
      await this.publicationFacts(publication, 'execute-synthetic');
      this.coverage.execution = {status:result.status, synthetic:result.synthetic, events:result.events.length};
    });
    await this.check('independent_final_readback_confirms_result_without_rerun', async () => {
      const previous = publication;
      const response = await this.submit(SELECTORS.readback, `/api/publications/${publication.object_id}/readback`);
      this.requestBody(response, ['action_id']); publication = response.body;
      assert.equal(publication.status, 'completed'); assert.equal(publication.payload.receiver_sequence, previous.payload.receiver_sequence);
      assert.deepEqual(publication.payload.execution_result, previous.payload.execution_result);
      assert.equal(this.count(`/api/publications/${publication.object_id}/execute-synthetic`), 1);
      assert.equal(this.count(`/api/publications/${publication.object_id}/readback`), 2);
      assert(await this.disabled(SELECTORS.execute)); await this.publicationFacts(publication, 'readback-after-execute');
    });
    await this.missingWorkflow();
    await this.check('all_actual_UI_mutations_same_origin_no_auto_domain_actions', async () => {
      const mutations = [...this.requests.values()].filter(row => row.method === 'POST');
      assert(mutations.length > 0 && mutations.every(row => row.origin === this.origin && row.authorization));
      assert.equal(this.external.size, 0, 'Page attempted non-same-origin request');
      const before = mutations.length;
      await this.submit(SELECTORS.refresh, '/api/me', 'GET');
      assert.equal([...this.requests.values()].filter(row => row.method === 'POST').length, before, 'Refresh auto-issued a domain action');
      const audit = (await this.probe('/api/audit')).body.items;
      for (const action of ['review','request_publication','deliver_local','advance_receiver','readback','execute_synthetic'])
        assert(audit.some(row => row.action === action), `Backend lifecycle audit ${action} absent`);
      this.coverage.ui_mutations = mutations.map(({path}) => path.replace(/\/(?:task|plan|pub)-[^/]+/g, '/<owned-object>'));
    });
    await this.check('security_headers_and_no_visible_raw_JSON', async () => {
      const rows = this.responses.filter(row => row.status === 200 && (row.path === '/' || row.path.startsWith('/static/') || row.path.startsWith('/api/')));
      assert(rows.length > 0);
      for (const row of rows) {
        assert.equal(row.headers['cache-control'], 'no-store'); assert.equal(row.headers['x-content-type-options'], 'nosniff');
        assert.equal(row.headers['referrer-policy'], 'no-referrer'); assert.equal(row.headers['x-frame-options'], 'DENY');
        assert(row.headers['content-security-policy'].includes("connect-src 'self'")); assert(!row.cached);
      }
      assert(await this.evaluate(`![...document.querySelectorAll('#workbench pre')].some(e => e.getClientRects().length && !e.closest('details:not([open])'))`));
      await this.storageCheck();
    });
    await this.check('logout_clears_identity_inputs_consents_and_evidence', () => this.logout());
    await this.check('page_reload_forgets_credentials_and_does_not_fetch_private_data', async () => {
      await this.login();
      const mark = this.requests.size;
      await this.cmd('Page.addScriptToEvaluateOnNewDocument', {source:'globalThis.__smokeReloadDocument = true;'});
      await this.cmd('Page.reload', {ignoreCache:true});
      await this.until(() => this.evaluate(`globalThis.__smokeReloadDocument === true && document.readyState === 'complete' && !!document.querySelector('#connect-button')`),
        'new reloaded anonymous document (not previous page readyState)');
      await this.clearState();
      assert(![...this.requests.values()].slice(mark).some(row => row.authorization || row.path.startsWith('/api/')),
        'Reload retained credentials or loaded private data');
    });
    this.workflowComplete = true;
  }
  async missingWorkflow() {
    let task, before, fields;
    await this.check('missing_fixture_stops_after_snapshot_and_human_question', async () => {
      before = this.bootstrap.fixtures.missing.snapshot;
      task = await this.createBaseline(this.bootstrap.fixtures.missing);
      const response = await this.submit(SELECTORS.run, `/api/tasks/${task.object_id}/run`);
      task = response.body; assert.equal(task.status, 'awaiting_input');
      assert.equal(task.payload.calls, 2); assert.equal(task.payload.tools, 2);
      assert.deepEqual(Object.keys(task.payload.evidence), ['get_snapshot']);
      fields = task.payload.awaiting_fields; assert.equal(fields.length, 2);
      await this.taskFacts(task);
      const rendered = await this.evaluate(`[...document.querySelectorAll('#supplement-fields input')].map(e => e.dataset.field)`);
      assert.deepEqual(rendered, fields, 'Exactly backend-issued missing paths displayed');
      assert(await this.disabled(SELECTORS.run));
    });
    await this.check('supplement_exact_missing_integers_preserves_source_time_and_does_not_run', async () => {
      const normal = this.bootstrap.fixtures.normal.snapshot;
      const answers = fields.map(field => {
        const match = /^(materials|orders)\[([0-9]+)\]\.(initial_stock|due_min)$/.exec(field);
        assert(match, 'Only issued index missing paths');
        const value = normal[match[1]][Number(match[2])][match[3]];
        assert(Number.isInteger(value)); return {field, value};
      });
      for (let i = 0; i < answers.length; i++) await this.fill(`#answer-${i}`, answers[i].value);
      await this.fill('#supplement-note', XSS_NOTE);
      const response = await this.submit('#supplement-submit', `/api/tasks/${task.object_id}/resume`);
      const request = this.requestBody(response, ['action_id','answers','note']); assert.deepEqual(request.answers, answers);
      task = response.body; assert.equal(task.status, 'created'); assert.equal(task.payload.calls, 2); assert.equal(task.payload.tools, 2);
      assert.deepEqual(task.payload.evidence, {}); assert.equal(this.count(`/api/tasks/${task.object_id}/run`), 1);
      const snapshot = (await this.probe(`/api/snapshots/${this.bootstrap.fixtures.missing.object_id}`)).body.payload;
      assert.equal(snapshot.observed_at_ms, before.observed_at_ms); assert.equal(snapshot.revision, before.revision + 1);
      for (const {field,value} of answers) {
        const match = /^(materials|orders)\[([0-9]+)\]\.(initial_stock|due_min)$/.exec(field);
        assert.equal(snapshot[match[1]][Number(match[2])][match[3]], value);
      }
    });
    await this.check('user_note_is_literal_text_no_innerHTML_XSS', async () => {
      const audit = (await this.probe('/api/audit')).body.items;
      assert(audit.some(item => item.action === 'human_supplement' && item.detail.note === XSS_NOTE));
      assert((await this.text('#audit-list')).includes(XSS_NOTE), 'User-controlled note visibly rendered literally');
      assert(await this.evaluate(`!document.querySelector('#smoke-xss') && globalThis.__smokeXss === undefined && globalThis.__smokeInnerHTMLWrites === 0`),
        'Unexpected user markup execution or innerHTML write');
    });
    await this.check('resumed_missing_task_observes_ten_calls_not_nine_tools', async () => {
      const response = await this.submit(SELECTORS.run, `/api/tasks/${task.object_id}/run`); task = response.body;
      const sequence = await this.completedTask(task, 10, 10);
      const rec = (await this.probe(`/api/recommendations/${task.payload.recommendation_id}`)).body;
      assert.equal(rec.status, 'pending_review');
      this.coverage.missing = {status:task.status, calls:task.payload.calls, tools:task.payload.tools,
        evidence:Object.keys(task.payload.evidence).length, sequence, source_observed_at_preserved:true};
    });
  }

  // Each successful entry is recorded only after actual browser/API observation.
  async run() {
    await this.start();
    await this.workflow();
  }

  async processTable() {
    const {stdout} = await promisify(execFile)('/bin/ps', ['-axo', 'pid=,ppid=,pgid=,lstart='],
      {maxBuffer: 2 * 1024 * 1024, timeout: 3000, env: {PATH: '/usr/bin:/bin'}});
    return stdout.trim().split('\n').filter(Boolean).map(line => {
      const [pid, parent, group, ...started] = line.trim().split(/\s+/);
      return {pid: Number(pid), parent: Number(parent), group: Number(group), started: started.join(' ')};
    });
  }
  async snapshotOwnedProcesses() {
    if (!this.children.length) return [];
    const table = await this.processTable();
    const pids = new Set(this.children.filter(child => child.pid && !child.launchFailed
      && child.exitCode === null && child.signalCode === null).map(child => child.pid));
    for (const row of table) if (this.knownProcesses.get(row.pid)?.started === row.started) pids.add(row.pid);
    let changed = true;
    while (changed) {
      changed = false;
      for (const row of table) if (pids.has(row.parent) && !pids.has(row.pid)) { pids.add(row.pid); changed = true; }
    }
    for (const row of table) if (pids.has(row.pid)) this.knownProcesses.set(row.pid, row);
    return table.filter(row => pids.has(row.pid));
  }
  killGroup(child, signal) {
    if (!child?.pid || child.launchFailed || child.exitCode !== null || child.signalCode !== null) return;
    try { process.kill(-child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
  async cleanup() {
    this.stopping = true; this.abort.abort();
    let owned = [], enumerationFailed = false;
    try { owned = await this.snapshotOwnedProcesses(); } catch { enumerationFailed = true; }
    this.cdp?.close();
    if (!this.cdp && this.socket) { try { this.socket.close(); } catch { /* child termination below closes transport */ } }
    for (const pending of this.controls.values()) { clearTimeout(pending.timer); pending.reject(new Error('Smoke cleanup')); }
    this.controls.clear();
    try {
      // Only our detached child groups, then exact previously observed PID + start-time descendants.
      for (const child of this.children) this.killGroup(child, 'SIGTERM');
      const ended = () => this.children.every(child => child.launchFailed || child.exitCode !== null || child.signalCode !== null);
      try { await this.until(async () => ended(), 'owned direct child exit after TERM', 2500, true); } catch { /* escalate below */ }
      for (const child of this.children) this.killGroup(child, 'SIGKILL');
      const current = await this.processTable();
      for (const row of owned.reverse()) if (current.some(item => item.pid === row.pid && item.started === row.started)) {
        try { process.kill(row.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
      await this.until(async () => {
        const remaining = await this.processTable();
        return ended() && !owned.some(row => remaining.some(item => item.pid === row.pid && item.started === row.started));
      }, 'tracked children and descendants gone', 5000, true);
      assert(!enumerationFailed, 'Owned descendants could not be enumerated');
      this.processesClean = true; this.record('tracked_owned_process_cleanup', 'PASS');
    } catch (error) {
      for (const child of this.children) { try { this.killGroup(child, 'SIGKILL'); } catch { /* reported below */ } }
      this.record('tracked_owned_process_cleanup', 'FAIL', error.message);
    }
    if (this.runtime) {
      if (!this.processesClean) {
        this.record('owned_runtime_cleanup', 'FAIL', 'Refusing directory deletion while process cleanup is unverified');
        return;
      }
      try {
        await this.ownedDirectory(this.root); await this.ownedDirectory(this.runtime);
        if (this.owned.has(this.appDirectory)) {
          await this.ownedDirectory(this.appDirectory);
          const audited = await this.auditDocuments(this.appDirectory);
          this.record('own_app_documents_no_plaintext_tokens', 'PASS', `${audited.files} own app files; Chrome profile and cookie files NOT scanned`);
        } else this.record('own_app_documents_no_plaintext_tokens', 'NOT_TESTED', 'App bootstrap was not completed');
      } catch (error) { this.record('own_app_documents_no_plaintext_tokens', 'FAIL', error.message); }
      try {
        await this.ownedDirectory(this.root); await this.ownedDirectory(this.runtime);
        await rm(this.runtime, {recursive: true, force: false});
        this.runtimeClean = true; this.record('owned_runtime_cleanup', 'PASS', 'Only this run\'s private runtime/profile removed');
      } catch (error) { this.record('owned_runtime_cleanup', 'FAIL', error.message); }
    }
  }
  async auditDocuments(directory) {
    assert(directory === this.appDirectory && this.owned.has(directory), 'Audit scope must be exact own app documents');
    let files = 0, bytes = 0;
    const walk = async path => {
      for (const entry of await readdir(path, {withFileTypes: true})) {
        assert(!entry.isSymbolicLink(), 'Unexpected symlink in own app documents');
        const file = join(path, entry.name);
        if (entry.isDirectory()) await walk(file);
        else if (entry.isFile()) {
          files++; assert(files <= 10000, 'Document audit file bound');
          const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
          try {
            const stat = await handle.stat(); assert(stat.isFile() && stat.nlink === 1, 'Only own regular single-link documents');
            bytes += stat.size; assert(bytes <= 256 * 1024 * 1024, 'Document audit size bound');
            await this.scanHandle(handle);
          } finally { await handle.close(); }
        } else throw new Error('Unexpected own app document type');
      }
    };
    await walk(directory); return {files, bytes};
  }
  async scanHandle(handle) {
    const needles = this.secrets.flatMap(secret => [Buffer.from(secret), Buffer.from(secret, 'utf16le')]);
    const overlap = Math.max(1, ...needles.map(needle => needle.length)) - 1;
    let previous = Buffer.alloc(0); const chunk = Buffer.alloc(65536);
    for (;;) {
      const {bytesRead} = await handle.read(chunk, 0, chunk.length, null); if (!bytesRead) break;
      const combined = Buffer.concat([previous, chunk.subarray(0, bytesRead)]);
      assert(!needles.some(needle => combined.includes(needle)), 'Plaintext token in owned document (content withheld)');
      previous = Buffer.from(combined.subarray(Math.max(0, combined.length - overlap)));
    }
  }
  async report() {
    if (!this.workflowComplete) this.record('remaining_UI_workflow', 'NOT_TESTED', 'Prerequisite failed; no UI/API bypass');
    if (this.root) {
      await this.ownedDirectory(this.root);
      const log = this.logs.map(line => this.safe(line)).join('\n') + '\n';
      await writeFile(join(this.root, 'console.log'), log, {flag: 'wx', mode: 0o600});
      const handle = await open(join(this.root, 'console.log'), constants.O_RDONLY | constants.O_NOFOLLOW);
      try { await this.scanHandle(handle); } finally { await handle.close(); }
      this.record('exact_owned_report_token_audit', 'PASS', 'Sanitized report strings and exact owned console.log only');
    }
    const counts = Object.fromEntries(['PASS', 'FAIL', 'NOT_TESTED'].map(status =>
      [status, this.results.filter(row => row.status === status).length]));
    const summary = {
      status: counts.FAIL || counts.NOT_TESTED ? 'FAIL' : 'PASS',
      mode: 'actual installed Chrome CDP + original scheduling API/UI + SyntheticProvider TEST ONLY; no actual LLM',
      browser: this.chromeVersion || null, elapsed_seconds: Number(((Date.now() - this.started) / 1000).toFixed(2)),
      counts, results: this.results, observed: this.coverage, lifecycle: this.lifecycle,
      scope: {source: PROJECT, origin: this.origin || null, fresh_profile: Boolean(this.profile),
        app_documents_audited: Boolean(this.owned.has(this.appDirectory)), profile_cookie_files_audited: false,
        page_network_only: true, physical_execution: false},
      NOT_TESTED,
      artifacts: this.runtimeClean ? 'Only sanitized summary.json and console.log retained; own runtime/profile removed'
        : 'Runtime cleanup NOT verified; inspect cleanup result, do not assume deletion',
    };
    const output = this.safeReport(summary);
    assert(!this.secrets.some(secret => output.includes(secret)), 'Report token redaction failed');
    if (this.root) {
      await this.ownedDirectory(this.root);
      await writeFile(join(this.root, 'summary.json'), output, {flag: 'wx', mode: 0o600});
      const handle = await open(join(this.root, 'summary.json'), constants.O_RDONLY | constants.O_NOFOLLOW);
      try { await this.scanHandle(handle); } finally { await handle.close(); }
      process.stdout.write(`Owned summary: ${this.safe(join(this.root, 'summary.json'))}\n`);
    }
    process.stdout.write(output);
    return summary.status === 'PASS' ? 0 : 1;
  }
}

export function selfTest() {
  assert.equal(parseArgs([]).mode, 'help'); assert.equal(parseArgs(['--help']).mode, 'help');
  assert.equal(parseArgs(['--self-test']).mode, 'self-test');
  for (const args of [['--run-chrome'], ['--artifacts-parent', '/private/tmp'],
    ['--run-chrome', '--artifacts-parent', PROJECT],
    ['--run-chrome', '--artifacts-parent', '/private/tmp', '--model-url', 'http://127.0.0.1'],
    ['--run-chrome', '--artifacts-parent', '/private/tmp', '--chrome', 'relative'],
    ['--run-chrome', '--run-chrome', '--artifacts-parent', '/private/tmp'],
    ['--run-chrome', '--artifacts-parent', '/private/tmp', '--profile', '/anywhere']])
    assert.throws(() => parseArgs(args));
  assert.equal(parseArgs(['--run-chrome', '--artifacts-parent', '/private/tmp']).authorized, true);
  assert(isWithin(PROJECT, join(PROJECT, 'scripts'))); assert(!isWithin(PROJECT, PROJECT + '-elsewhere'));
  assert.equal(sameOriginPath('/api/tasks/task-1/run'), '/api/tasks/task-1/run');
  const forbiddenQuery = ['/api?', 'token', '=', 'x'].join('');
  for (const path of ['//elsewhere', 'https://elsewhere', '/api/../health', forbiddenQuery, '/api\\x', '/%2f'])
    assert.throws(() => sameOriginPath(path));
  const smoke = new Smoke({}); smoke.secrets.push('INERT-TEST-SECRET');
  assert(!smoke.safeReport({message: 'INERT-TEST-SECRET'}).includes('INERT-TEST-SECRET'));
  assert.equal(smoke.children.length, 0); assert.equal(smoke.owned.size, 0);
  assert.equal(COMPUTATION.length, 7);
  assert(NOT_TESTED.includes('actualLLM') && NOT_TESTED.includes('physicalMES'));
  const protocol = protocolFailure('Fetch.continueRequest', {code: -32602, message: 'Invalid InterceptionId.'});
  assert.equal(protocol.cdpCode, -32602); assert.equal(protocol.cdpReason, 'invalid_interception_id');
  assert.equal(observerDiagnostic({method: 'Fetch.requestPaused'}, protocol),
    'CDP observer failed event=Fetch.requestPaused method=Fetch.continueRequest code=-32602 reason=invalid_interception_id type=Error');
  for (const code of [null, undefined, '32602', true, NaN, Infinity, 1.5, 2147483648, -2147483648])
    assert.equal(cdpCode(code), null);
  for (const code of [0, -32602, 2147483647, -2147483647]) assert.equal(cdpCode(code), code);
  for (const [message, reason] of CDP_REASON_TEXT)
    assert.equal(protocolFailure('Fetch.failRequest', {code: -32000, message}).cdpReason, reason);
  const untrusted = 'INERT-DIAGNOSTIC-SENTINEL';
  const unknown = protocolFailure(untrusted, {code: untrusted, message: untrusted,
    data: {url: 'https://example.invalid/' + untrusted, html: '<div>' + untrusted + '</div>'}});
  assert.equal(unknown.message, 'CDP other_method failed code=unavailable reason=unclassified_protocol_error');
  assert(!Object.values(unknown).some(value => String(value).includes(untrusted)));
  assert.equal(observerDiagnostic({method: untrusted, params: {text: untrusted}},
    {name: untrusted, cdpMethod: untrusted, cdpCode: untrusted, cdpReason: untrusted, message: untrusted, stack: untrusted}),
  'CDP observer failed event=other_event method=other_method code=unavailable reason=non_protocol_error type=other_error');
  assert.equal(observerDiagnostic({method: 'Network.responseReceived'}, new TypeError(untrusted)),
    'CDP observer failed event=Network.responseReceived method=other_method code=unavailable reason=non_protocol_error type=TypeError');
  process.stdout.write('PASS inert arguments, no-launch default, path bounds, sanitized CDP diagnostics and contract self-tests; browser NOT_TESTED\n');
}
async function main() {
  let options;
  try { options = parseArgs(process.argv.slice(2)); }
  catch { process.stderr.write(USAGE); process.exitCode = 2; return; }
  if (options.mode === 'help') { process.stdout.write(USAGE); return; }
  if (options.mode === 'self-test') { selfTest(); return; }
  const smoke = new Smoke(options);
  let interrupt;
  const interrupted = new Promise((_, reject) => { interrupt = reject; });
  const stop = reason => {
    if (smoke.stopping) return;
    smoke.stopping = true; smoke.abort.abort(); smoke.cdp?.close(); interrupt(new Error(reason));
  };
  const timer = setTimeout(() => stop('300s absolute smoke deadline'), MAX_RUN_MS);
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  const handlers = signals.map(signal => { const handler = () => stop(`Interrupted by ${signal}`); process.on(signal, handler); return handler; });
  const work = smoke.run();
  try { await Promise.race([work, interrupted]); }
  catch (error) { smoke.record(smoke.activeCheck || 'browser_smoke', 'FAIL', error.message); }
  finally {
    smoke.stopping = true; smoke.abort.abort(); smoke.cdp?.close();
    // Let bounded in-flight directory creation settle before own-directory cleanup; no launch after stop.
    await work.catch(() => {}); clearTimeout(timer);
    await smoke.cleanup();
    try { process.exitCode = await smoke.report(); }
    catch { process.stderr.write('Scheduling browser smoke report could not be retained safely.\n'); process.exitCode = 1; }
    signals.forEach((signal, index) => process.removeListener(signal, handlers[index]));
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
