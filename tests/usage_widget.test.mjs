import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { getEventListeners } from 'node:events';
import { mkdtemp, mkdir, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import register, { acquisitionLines, dockLines, metadataRequest, usageLines, observeAlerts } from '../tui/agy-usage.mjs';

const NOW = Date.parse('2026-01-01T00:00:30Z');
const payload = () => ({ ok: true, snapshot: { observed_at: '2026-01-01T00:00:00Z', age_seconds: 30,
  freshness_limit_seconds: 120, freshness: 'fresh', stale: false, health: { status: 'ok' },
  meters: ['3p-5h', '3p-weekly', 'gemini-5h', 'gemini-weekly'].map((id, i) => ({ id, remaining_fraction: [.75, .5, .25, .1][i], reset_time: '2026-01-01T01:00:00Z' })),
  pools: [{ pool: 'Claude and GPT models', available: true }, { pool: 'Gemini Models', available: true }] } });
const tick = () => new Promise(resolve => setImmediate(resolve));
const flatten = tree => !tree || typeof tree !== 'object' ? [] : [tree, ...((tree.children ?? []).flatMap(flatten))];
const textOf = tree => flatten(tree).flatMap(node => node.children ?? []).filter(value => typeof value === 'string').join('\n');
const linesText = lines => lines.map(([, text]) => text).join('\n');
const assertGeometry = (lines, width) => { for (const [, text] of lines) assert.equal(text.length, width); };

// Model public host semantics: ambient no-arg launch toggles; only modal gets keys.
function harness(options = {}) {
  const apps = new Map(), states = new Map(), instances = new Map();
  const requests = [], timers = new Map(), opens = [], composer = [], updates = [];
  let now = NOW, nextTimer = 0, modal = null, rendering = null;
  function unmount(id) {
    const instance = instances.get(id);
    for (const effect of instance?.effects ?? []) effect?.cleanup?.();
    instances.delete(id);
  }
  const sdk = {
    Text: 'Text', Box: 'Box', Dialog: 'Dialog', Overlay: 'Overlay',
    h: (type, props, ...children) => ({ type, props, children }),
    React: {
      useState(initial) {
        const instance = rendering, i = instance.cursor++;
        if (!instance.hooks[i]) instance.hooks[i] = { value: typeof initial === 'function' ? initial() : initial };
        return [instance.hooks[i].value, value => { instance.hooks[i].value = value; }];
      },
      useEffect(fn, deps) {
        const instance = rendering, i = instance.cursor++, old = instance.effects[i];
        if (!old || deps.some((v, j) => v !== old.deps[j])) instance.pending.push(() => {
          old?.cleanup?.(); instance.effects[i] = { deps, cleanup: fn() };
        });
      }
    },
    defineWidgetApp(app) { apps.set(app.id, app); return app; },
    openWidget(app, state) { opens.push(app.id); states.set(app.id, state); if (app.mode === 'modal') modal = app.id; },
    updateWidget(app, fn) { updates.push(app.id); if (states.has(app.id)) states.set(app.id, fn(states.get(app.id))); }
  };
  register(sdk, { request: (args, options) => new Promise((resolve, reject) => requests.push({ args, options, resolve, reject })),
    clock: () => now, ...options, timers: {
      setInterval(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay, nextAt: now + delay }); return id; },
      clearInterval(id) { timers.delete(id); }
    } });
  return {
    apps, requests, timers, opens, composer, updates, state: (id = 'agy-usage') => states.get(id),
    launch(arg = '', id = 'agy-usage') {
      const app = apps.get(id);
      if (app.mode === 'ambient' && !arg.trim() && states.has(id)) { states.delete(id); unmount(id); return null; }
      const state = app.init(arg); if (state === null) return null;
      states.set(id, state); if (app.mode === 'modal') modal = id; return state;
    },
    input(ch = '', key = {}) {
      if (!modal) { composer.push(ch); return; }
      const next = apps.get(modal).reduce(states.get(modal), { ch, key });
      if (next === null) { states.delete(modal); unmount(modal); modal = null; } else states.set(modal, next);
    },
    advance(ms) {
      const end = now + ms;
      while (timers.size) {
        const next = Math.min(...[...timers.values()].map(timer => timer.nextAt)); if (next > end) break;
        now = next;
        for (const timer of [...timers.values()]) if (timer.nextAt === next) { timer.nextAt += timer.delay; timer.callback(); }
      }
      now = end;
    },
    render(id = 'agy-usage', cols = 80) {
      let instance = instances.get(id);
      if (!instance) { instance = { cursor: 0, hooks: [], effects: [], pending: [] }; instances.set(id, instance); }
      rendering = instance; instance.cursor = 0;
      const color = Object.fromEntries(['label', 'muted', 'ok', 'error', 'primary'].map(tone => [tone, `theme-${tone}`]));
      const component = apps.get(id).render({ state: states.get(id), cols, rows: 24, t: { color } });
      const tree = component.type(component.props);
      for (const fn of instance.pending.splice(0)) fn(); rendering = null; return tree;
    },
    dispose() { for (const id of [...instances.keys()]) unmount(id); }
  };
}

async function ready(h) { h.launch(); h.render(); await tick(); h.requests[0].resolve(payload()); await tick(); h.render(); }

test('two registered apps, inert registration/init, ambient host toggle and no key capture', async () => {
  const h = harness();
  try {
    assert.equal(h.apps.get('agy-usage').mode, 'ambient'); assert.equal(h.apps.get('agy-usage').zone, 'dock-top');
    assert.equal(h.apps.get('agy-usage-details').mode, 'modal'); assert.deepEqual(h.opens, []);
    const state = h.launch(); assert.deepEqual(JSON.parse(JSON.stringify(state)), state);
    assert.equal(h.requests.length, 0); assert.equal(h.timers.size, 0);
    for (const [ch, key] of [['R', {}], ['q', {}], ['hello', {}], ['', { escape: true }]]) {
      assert.strictEqual(h.apps.get('agy-usage').reduce(state, { ch, key }), state);
      h.input(ch, key); assert.strictEqual(h.state(), state);
    }
    assert.deepEqual(h.composer, ['R', 'q', 'hello', '']);
    h.render(); await tick(); assert.equal(h.requests[0].args.action, 'status');
    assert.equal(h.launch(), null); assert.equal(h.state(), undefined);
    assert.equal(h.requests[0].options.signal.aborted, true); assert.equal(h.timers.size, 0);
    h.requests[0].resolve(payload()); await tick(); assert.equal(h.state(), undefined);
  } finally { h.dispose(); }
});

test('80-column dock is four readable rows with four percentages and no modal chrome', async () => {
  const h = harness();
  try {
    await ready(h); const tree = h.render(), text = textOf(tree);
    assert.equal(tree.props.width, 76);
    assert.equal(flatten(tree).filter(n => n.type === 'Text').length, 4);
    assert.ok(!flatten(tree).some(n => ['Overlay', 'Dialog', 'ShimmerRows'].includes(n.type)));
    for (const value of ['75.0%', '50.0%', '25.0%', '10.0%']) assert.ok(text.includes(value));
    assert.match(text, /Claude\/GPT.*5h.*weekly/); assert.match(text, /Gemini.*5h.*weekly/);
    assert.match(text, /age 30s/); assert.match(text, /Next 5h reset: 59m/); assert.match(text, /\/agy-usage details/);
    assert.doesNotMatch(text, /QUOTA CONTROL|SONNET|FIVE-HOUR LEFT|_\||#{3}/);
    for (const n of flatten(tree).filter(n => n.type === 'Text')) assert.match(n.props.color, /^theme-/);
    for (const width of [1, 16, 30, 70, 78, 100]) {
      const lines = dockLines(payload(), NOW, width); assert.equal(lines.length, 4); assertGeometry(lines, width);
    }
    assertGeometry(usageLines(payload(), NOW, 70), 70);
    assert.ok(usageLines(payload(), NOW, 70).length <= 16);
  } finally { h.dispose(); }
});

test('slash refresh replaces dock state, keeps prior observation, sanitizes failures', async () => {
  const h = harness();
  try {
    await ready(h); const saved = h.state().payload;
    h.launch('refresh'); const tree = h.render(); await tick();
    assert.equal(h.requests[1].args.action, 'refresh');
    assert.strictEqual(h.state().payload, saved); assert.match(textOf(tree), /Refreshing quota/);
    assert.match(textOf(tree), /last\/unverified/); assert.match(textOf(tree), /75.0%/);
    h.requests[1].reject(new Error('RAW SECRET')); await tick();
    const text = textOf(h.render()); assert.equal(h.state().phase, 'failed');
    assert.match(text, /metadata_unavailable/); assert.doesNotMatch(text, /SECRET/); assert.match(text, /unverified/);
    h.launch('refresh'); h.render(); await tick();
    h.requests[2].resolve({ ok: false, error: { code: 'process_exit', message: 'SECRET' } }); await tick();
    assert.strictEqual(h.state().payload, saved); assert.match(textOf(h.render()), /process_exit/);
  } finally { h.dispose(); }
});

test('explicit details opens supported modal, Esc/q close only details, dock remains', async () => {
  const h = harness();
  try {
    await ready(h); const dock = h.state();
    assert.strictEqual(h.launch('details'), dock); assert.deepEqual(h.opens, ['agy-usage-details']);
    const tree = h.render('agy-usage-details'); await tick();
    assert.ok(flatten(tree).some(n => n.type === 'Box' && n.props?.position === 'absolute'));
    assert.equal(flatten(tree).find(n => n.type === 'Dialog').props.title, 'Quota details');
    assert.match(textOf(tree), /Claude\/GPT.*75.0%/); assert.match(textOf(tree), /Gemini.*10.0%/);
    h.input('', { escape: true }); assert.strictEqual(h.state(), dock); assert.equal(h.state('agy-usage-details'), undefined);
    assert.equal(h.requests[1].options.signal.aborted, true);
    h.launch('', 'agy-usage-details'); h.render('agy-usage-details'); await tick(); h.input('q');
    assert.strictEqual(h.state(), dock); assert.equal(h.state('agy-usage-details'), undefined);
    h.input('R'); assert.equal(h.composer.at(-1), 'R');
  } finally { h.dispose(); }
});

test('mount and request fences protect reopened/replaced state before old effect cleanup', async () => {
  const h = harness();
  try {
    h.launch(); h.render(); await tick(); const old = h.requests[0];
    const replaced = h.launch('refresh'); old.resolve(payload()); await tick(); assert.strictEqual(h.state(), replaced);
    h.render(); await tick(); assert.equal(old.options.signal.aborted, true);
    const superseded = h.requests[1]; h.launch('refresh'); superseded.resolve(payload()); await tick();
    assert.equal(h.state().phase, 'loading'); assert.equal(h.state().payload, null);
    h.render(); await tick(); h.launch(); assert.equal(h.requests[2].options.signal.aborted, true);
    h.launch(); const reopened = h.state(); h.requests[2].resolve(payload()); await tick(); assert.strictEqual(h.state(), reopened);
  } finally { h.dispose(); }
});

test('Unknown is not zero; stale age and due resets never imply confirmed refill', () => {
  const result = payload(); result.snapshot.meters[0].remaining_fraction = null; result.snapshot.meters[0].reset_time = null;
  assert.match(linesText(dockLines(result, NOW, 78)), /Unknown/); assert.doesNotMatch(linesText(dockLines(result, NOW, 78)), /(?:^|\s)0\.0%/);
  assert.match(linesText(usageLines(result, NOW, 70)), /reset in Unknown/);
  const stale = linesText(usageLines(result, NOW + 3600000, 70));
  assert.match(stale, /stale\/unverified/); assert.match(stale, /due!/); assert.match(stale, /last available \(unverified\)/);
  for (const invalid of [undefined, NaN, Infinity, -.1, 1.1, '0']) {
    result.snapshot.meters.forEach(m => { m.remaining_fraction = invalid; });
    const text = linesText(dockLines(result, NOW, 78)); assert.match(text, /Unknown/); assert.doesNotMatch(text, /\d+\.\d+%/);
  }
  assert.match(linesText(dockLines(payload(), NOW, 78, { retained: true })), /last\/unverified/);
  assert.match(linesText(usageLines({ ok: true }, NOW, 70)), /Unknown/);
});

test('weekly resets and exact reset timestamps remain visible with depleted-window blockers', () => {
  const p = payload(); p.snapshot.meters[1].reset_time = '2026-01-08T01:00:00Z';
  p.snapshot.meters[1].remaining_fraction = 0; p.snapshot.pools[0].available = false;
  const dock = linesText(dockLines(p, NOW, 74));
  assert.match(dock, /weekly.*0\.0%.*7d/); assert.match(dock, /weekly depleted/);
  p.snapshot.meters[1].remaining_fraction = .5; p.snapshot.meters[0].remaining_fraction = 0;
  assert.match(linesText(dockLines(p, NOW, 74)), /5h depleted/);
  assert.match(linesText(usageLines(p, NOW, 70)), /2026-01-08T01:00:00Z/);
  assert.match(linesText(usageLines(p, NOW, 70)), /age: 30s/);
  const due = linesText(dockLines(p, NOW + 3600000, 74));
  assert.match(due, /due!/); assert.match(due, /unverified/);
  assertGeometry(dockLines(p, NOW, 74), 74);
});

test('optional Argon lane renders only when present and tolerates a missing 5h window', () => {
  assert.doesNotMatch(linesText(dockLines(payload(), NOW, 74)), /Argon/);
  const p = payload();
  p.snapshot.meters.push({ id: 'gemini-argon-wtu-weekly', group: 'Gemini 4 Argon', remaining_fraction: .4, reset_time: '2026-01-08T00:00:00Z' });
  p.snapshot.pools.push({ pool: 'Gemini 4 Argon', available: true });
  const dock = dockLines(p, NOW, 74), text = linesText(dock);
  assert.match(text, /Argon/); assert.match(text, /40\.0%/); assert.match(text, /n\/a/);
  assertGeometry(dock, 74);
  for (const value of ['75.0%', '50.0%', '25.0%', '10.0%']) assert.ok(text.includes(value));
  const details = linesText(usageLines(p, NOW, 70));
  assert.match(details, /Argon pool: /);
  assertGeometry(usageLines(p, NOW, 70), 70);
});

test('alert identity distinguishes new same-clock publications and suppresses unhealthy values', () => {
  const p = payload(); p.snapshot.observation_id = 'fixture-a';
  let alerts = observeAlerts({ enabled: true, levels: {}, notices: [], sequence: 0, lastObserved: null, lastObservationId: null }, p, NOW, true);
  p.snapshot.observation_id = 'fixture-b'; p.snapshot.meters[0].remaining_fraction = .05;
  alerts = observeAlerts(alerts, p, NOW); assert.equal(alerts.sequence, 1);
  assert.equal(observeAlerts(alerts, p, NOW).sequence, 1);
  for (const extra of [{ stale: true }, { freshness: 'error' }, { freshness: 'missing' }, { health: { status: 'error' } }]) {
    const next = payload(); next.snapshot.observation_id = 'fixture-c'; Object.assign(next.snapshot, extra);
    const suppressed = observeAlerts(alerts, next, NOW); assert.equal(suppressed.sequence, 1); assert.deepEqual(suppressed.notices, []);
  }
});

test('transport permits status/refresh only and registration never opens dock', async () => {
  let calls = 0;
  for (const action of ['status', 'refresh']) {
    const result = await metadataRequest({ action }, { execute: (bin, argv, options, done) => {
      calls++; assert.match(argv[0], /usage\.py$/); assert.deepEqual(argv.slice(1), [action, '--json']);
      assert.equal(options.shell, false); assert.equal(options.timeout, action === 'refresh' ? 15000 : 4500);
      assert.equal(options.env, undefined); done(null, '{"ok":true}');
    } }); assert.equal(result.ok, true);
  }
  for (const action of ['history', 'credits', 'poll', 'login']) await assert.rejects(metadataRequest({ action }));
  assert.equal(calls, 2);
  const h = harness(); assert.equal(h.requests.length, 0); assert.equal(h.timers.size, 0); assert.deepEqual(h.opens, []);
  for (const arg of ['refresh --credits', 'history', 'login', 'refresh;whoami']) assert.equal(h.launch(arg), null);
});

test('dock ticks locally without automatic refresh or cache subprocess polling', async () => {
  const h = harness();
  try {
    await ready(h); h.advance(180000); h.render(); await tick();
    assert.equal(h.requests.length, 1); assert.deepEqual([...h.timers.values()].map(t => t.delay), [1000]);
  } finally { h.dispose(); }
});

test('due reset is shown as unconfirmed even when last value is depleted', () => {
  const p = payload(); p.snapshot.meters[0].remaining_fraction = 0;
  const text = linesText(usageLines(p, NOW + 3600000, 100));
  assert.match(text, /5h due\/unconfirmed/); assert.match(text, /0.0%/);
});

test('opt-in warnings survive refresh, dedupe redraws and clear on close', async () => {
  const h = harness();
  try {
    await ready(h); h.launch('alerts on'); h.render(); await tick();
    assert.equal(h.requests.length, 1); assert.equal(h.state().alerts.enabled, true);
    async function refresh(value, id, extra = {}) {
      const p = payload(); p.snapshot.observation_id = id;
      p.snapshot.meters[0].remaining_fraction = value; Object.assign(p.snapshot, extra);
      h.launch('refresh'); h.render(); await tick(); h.requests.at(-1).resolve(p); await tick(); h.render();
    }
    await refresh(.09, 'fresh-a'); assert.equal(h.state().alerts.sequence, 1);
    assert.match(textOf(h.render()), /Low quota.*Claude\/GPT 5h.*9.0%/);
    await refresh(.09, 'fresh-a'); assert.equal(h.state().alerts.sequence, 1);
    await refresh(.5, 'bad', { freshness: 'error' }); assert.equal(h.state().alerts.sequence, 1);
    await refresh(.05, 'fresh-b'); assert.equal(h.state().alerts.sequence, 1);
    await refresh(.5, 'fresh-c'); await refresh(.1, 'fresh-d'); assert.equal(h.state().alerts.sequence, 2);
    h.launch(); h.launch(); assert.equal(h.state().alerts.enabled, false);
  } finally { h.dispose(); }
});

test('pre-aborted metadata requests never spawn', async () => {
  for (const action of ['status', 'refresh']) {
    const controller = new AbortController(); controller.abort();
    let calls = 0;
    await assert.rejects(metadataRequest({ action }, { signal: controller.signal,
      execute: () => { calls++; throw new Error('must not spawn'); } }), /cancelled/);
    assert.equal(calls, 0);
  }
});

// Real execFile + production refresh lock/runner, with only the acquisition replaced.
// No agy binary, saved session, keyring or remote quota is consulted.
const sleepingRefresh = `
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import usage

def forbidden(*args, **kwargs):
    raise AssertionError('authentication or agy lookup must not run')
usage.is_authenticated = usage.resolve_agy_command = forbidden

def fetch():
    child = r"import os,time; from pathlib import Path; p=Path(os.environ['QUOTA_TEST_CHILDREN']); p.open('a').write(str(os.getpid())+'\\n'); time.sleep(30)"
    return usage.run_agy(sys.executable, ('-c', child), time.monotonic() + 2.0)
usage.fetch_usage_report = fetch
raise SystemExit(usage.main(sys.argv[2:]))
`;
const alive = pid => { try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; throw error; } };
async function childPids(path) {
  try { return (await readFile(path, 'utf8')).trim().split('\n').filter(Boolean).map(Number); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}
async function waitForChild(path) {
  const deadline = Date.now() + 5000;
  while (!(await childPids(path)).length) {
    assert.ok(Date.now() < deadline, 'harmless child did not start'); await delay(20);
  }
}

for (const transition of ['close', 'replace']) test(`real refresh cancellation on ${transition} preserves supervisor cleanup and lock`,
  { skip: process.platform === 'win32' && 'POSIX SIGTERM/process-group regression', timeout: 12000 }, async t => {
    // realpath: on macOS TMPDIR lives under the /var -> /private/var symlink, which the
    // state-dir ancestry guard correctly refuses to traverse.
    const root = await realpath(await mkdtemp(join(process.env.TMPDIR || tmpdir(), 'quota-cancel-')));
    const home = join(root, 'home'), temporary = join(root, 'tmp'), children = join(root, 'children');
    await mkdir(home); await mkdir(temporary);
    const workers = [], requests = [];
    const execute = (bin, argv, options, done) => {
      const child = execFile(bin, ['-c', sleepingRefresh, ...argv], { ...options, env: { ...process.env,
        HOME: home, HERMES_HOME: join(home, 'profile'), TMPDIR: temporary,
        PYTHONDONTWRITEBYTECODE: '1', QUOTA_TEST_CHILDREN: children } }, done);
      workers.push({ child, exited: new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal }))) });
      return child;
    };
    const request = (args, options) => {
      const promise = metadataRequest(args, { ...options, execute });
      requests.push({ signal: options?.signal, settled: promise.then(value => ({ value }), error => ({ error: error.message })) });
      return promise;
    };
    const h = harness({ request });
    try {
      h.launch('refresh'); h.render(); await tick(); await waitForChild(children);
      const first = workers[0];
      if (transition === 'close') h.launch();
      else { h.launch('status'); h.render(); await tick(); await requests[1].settled; await tick(); }
      assert.equal(requests[0].signal.aborted, true); // Real Viewer AbortController cleanup.
      const replacement = h.state(), updates = h.updates.length;
      const busy = await request({ action: 'refresh' });
      const supervisorAliveWhileBusy = first.child.exitCode === null && first.child.signalCode === null;
      const countWhileBusy = (await childPids(children)).length;
      const outcome = await requests[0].settled, exit = await first.exited;
      await tick();
      const survivors = (await childPids(children)).filter(alive);
      const receipt = { transition, outcome, exit, supervisorAliveWhileBusy, busy,
        countWhileBusy, survivors, lateUpdates: h.updates.length - updates };
      t.diagnostic(JSON.stringify(receipt));
      assert.equal(exit.signal, null, 'UI cancellation must not SIGTERM the refresh supervisor');
      assert.equal(supervisorAliveWhileBusy, true);
      assert.equal(busy.error?.code, 'refresh_busy'); assert.equal(countWhileBusy, 1);
      assert.deepEqual(outcome, { error: 'cancelled' }); assert.deepEqual(survivors, []);
      assert.equal(getEventListeners(requests[0].signal, 'abort').length, 0);
      assert.strictEqual(h.state(), replacement); assert.equal(h.updates.length, updates);
      // The completed supervisor released its writer lock; no retries were scheduled.
      const released = await metadataRequest({ action: 'refresh' }, { execute });
      assert.equal(released.error?.code, 'refresh_unavailable');
      assert.equal(requests.length, transition === 'close' ? 2 : 3);
    } finally {
      h.dispose();
      // Only fixture-owned supervisors and groups; also reaps the old-transport red survivor.
      for (const { child } of workers) if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      for (const pid of await childPids(children)) if (alive(pid)) {
        try { process.kill(-pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
      await Promise.all(workers.map(worker => worker.exited));
      await rm(root, { recursive: true, force: true });
    }
  });

test('actual Python transport reads an isolated empty profile without agy', async t => {
  const { mkdtemp, rm, readdir, realpath } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const home = await realpath(await mkdtemp(join(process.env.TMPDIR || tmpdir(), 'quota-transport-')));
  const previous = process.env.HERMES_HOME; process.env.HERMES_HOME = home;
  t.after(async () => {
    if (previous === undefined) delete process.env.HERMES_HOME; else process.env.HERMES_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  const status = await metadataRequest({ action: 'status' });
  assert.equal(status.ok, true); assert.equal(status.snapshot.freshness, 'missing');
  assert.deepEqual(status.snapshot.meters, []); assert.deepEqual(await readdir(home), []);
});
