// Adapted from taylorwtf/hermes-antigravity-oauth-plus at b2bdf8c56966f26552168553bf8cc29470524971 (MIT).
// Optional public Hermes widgets. No core imports, API polling or inference.
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const METERS = [
  ['3p-5h', 'Claude/GPT 5h'], ['3p-weekly', 'Claude/GPT weekly'],
  ['gemini-5h', 'Gemini 5h'], ['gemini-weekly', 'Gemini weekly']
];
const ARGON_METERS = [
  ['gemini-argon-5h', 'Argon 5h'], ['gemini-argon-wtu-weekly', 'Argon weekly']
];
const LANES = [
  { pool: 'Claude/GPT', ids: ['3p-5h', '3p-weekly'], aliases: ['Claude and GPT models', 'claude_gpt', 'claude-gpt'], tone: 'primary' },
  { pool: 'Gemini', ids: ['gemini-5h', 'gemini-weekly'], aliases: ['Gemini Models', 'gemini'], tone: 'ok' }
];
const ARGON_LANE = {
  pool: 'Argon', ids: ['gemini-argon-5h', 'gemini-argon-wtu-weekly'],
  aliases: ['Gemini 4 Argon', 'gemini-argon', 'argon'], tone: 'ok', optionalWindows: true
};
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fraction = value => finite(value) && value >= 0 && value <= 1;
const array = value => Array.isArray(value) ? value : [];
const stamp = value => typeof value === 'string' && /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value))
  ? new Date(value).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : 'Unknown';
export function duration(value) {
  if (!finite(value) || value < 0) return 'Unknown';
  const seconds = Math.floor(value);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
}
const availability = value => value === true ? 'available' : value === false ? 'blocked/unverified' : 'Unknown';
const health = value => value === 'ok' || value === 'healthy' || value?.ok === true || value?.status === 'ok'
  ? 'ok' : value === 'error' || value?.ok === false || value?.status === 'error' ? 'error' : 'Unknown';
const errorCode = payload => /^[A-Za-z0-9_-]{1,64}$/.test(payload?.error?.code ?? '') ? payload.error.code : 'metadata_unavailable';

export function metadataRequest({ action = 'status' } = {}, { signal, execute = execFile } = {}) {
  if (!['status', 'refresh'].includes(action)) return Promise.reject(new Error('invalid_arguments'));
  if (signal?.aborted) return Promise.reject(new Error('cancelled'));
  const argv = [fileURLToPath(new URL('../usage.py', import.meta.url)), action, '--json'];
  return new Promise((resolve, reject) => {
    // An acquired refresh owns its writer lock and child-tree cleanup. UI cancellation
    // discards its result, not the supervisor; status reads have no child to supervise.
    execute(process.env.HERMES_PYTHON || 'python3', argv,
      { encoding: 'utf8', shell: false, timeout: action === 'refresh' ? 15000 : 4500,
        maxBuffer: 256 * 1024, windowsHide: true, signal: action === 'status' ? signal : undefined }, (error, stdout) => {
        if (signal?.aborted) return reject(new Error('cancelled'));
        try {
          const payload = JSON.parse(stdout);
          if (!payload || typeof payload.ok !== 'boolean' || (error && payload.ok)) throw new Error();
          resolve(payload);
        } catch { reject(new Error('metadata_unavailable')); }
      });
  });
}

const fit = (text, width) => String(text).slice(0, width).padEnd(width);
const row = (tone, text, width) => [tone, fit(text, width)];
const percent = value => fraction(value) ? `${(value * 100).toFixed(1)}%` : 'Unknown';
const resetIn = (meter, now) => {
  if (stamp(meter?.reset_time) === 'Unknown') return 'Unknown';
  const seconds = (Date.parse(meter.reset_time) - now) / 1000;
  return seconds <= 0 ? 'due!' : duration(seconds);
};
const isArgonMeter = m => (typeof m?.id === 'string' && /argon/i.test(m.id)) || (typeof m?.group === 'string' && /argon/i.test(m.group));
const meterWindow = m => m?.window === '5h' || m?.window === 'weekly' ? m.window : typeof m?.id === 'string' && m.id.toLowerCase().endsWith('5h') ? '5h' : 'weekly';
const meterFor = (snapshot, id) => {
  const exact = array(snapshot?.meters).find(m => m?.id === id);
  if (exact) return exact;
  if (ARGON_LANE.ids.includes(id)) {
    const targetWindow = id.endsWith('5h') ? '5h' : 'weekly';
    return array(snapshot?.meters).find(m => isArgonMeter(m) && meterWindow(m) === targetWindow) ?? {};
  }
  return {};
};
const poolFor = (snapshot, lane) => array(snapshot?.pools).find(p =>
  lane.aliases.includes(p?.pool) || (lane.optionalWindows && typeof p?.pool === 'string' && /argon/i.test(p.pool)));
const hasArgon = snapshot => array(snapshot?.meters).some(isArgonMeter) || Boolean(poolFor(snapshot, ARGON_LANE));
const lanesFor = snapshot => hasArgon(snapshot) ? [...LANES, ARGON_LANE] : LANES;
const argonMetersFor = snapshot => {
  const present = ARGON_METERS.filter(([id]) => Boolean(meterFor(snapshot, id).id));
  return present.length ? present : [ARGON_METERS[1]];
};
const metersFor = snapshot => hasArgon(snapshot) ? [...METERS, ...argonMetersFor(snapshot)] : METERS;
function observation(payload, now, retained) {
  const snapshot = payload?.ok === true ? payload.snapshot ?? {} : {};
  const freshness = ['fresh', 'stale', 'error', 'missing'].includes(snapshot.freshness) ? snapshot.freshness : 'Unknown';
  const observed = stamp(snapshot.observed_at);
  const age = observed !== 'Unknown' ? (now - Date.parse(snapshot.observed_at)) / 1000 : snapshot.age_seconds;
  const expired = finite(snapshot.freshness_limit_seconds) && finite(age) && age >= snapshot.freshness_limit_seconds;
  const resetDue = array(snapshot.meters).some(m => stamp(m?.reset_time) !== 'Unknown' && Date.parse(m.reset_time) <= now);
  const stale = retained || snapshot.stale === true || expired || resetDue || (finite(age) && age < 0) || ['stale', 'error', 'missing', 'Unknown'].includes(freshness);
  return { snapshot, freshness, observed, age, stale };
}
function nextFiveReset(snapshot, now) {
  const times = lanesFor(snapshot).map(lane => meterFor(snapshot, lane.ids[0])).filter(m => stamp(m.reset_time) !== 'Unknown').map(m => Date.parse(m.reset_time));
  if (!times.length) return 'Unknown';
  return resetIn({ reset_time: new Date(Math.min(...times)).toISOString() }, now);
}
const acquisitionCaption = action => action === 'status' ? 'Reading cache' : 'Refreshing quota';

// A six-cell travelling signal, not a progress bar or fullscreen sweep.
export function acquisitionLines({ action = 'status', elapsedMs = 0, width = 70 } = {}) {
  width = Math.max(1, Math.floor(width));
  const elapsed = finite(elapsedMs) ? Math.max(0, elapsedMs) : 0;
  const head = Math.floor(elapsed / 250) % 6;
  const signal = Array.from({ length: 6 }, (_, i) => i === head ? '>' : '.').join('');
  return [row('primary', `${signal} ${acquisitionCaption(action)} | ${(elapsed / 1000).toFixed(1)}s elapsed`, width)];
}

const compactDuration = seconds => !finite(seconds) || seconds < 0 ? 'Unknown' : seconds >= 86400 ? `${Math.floor(seconds / 86400)}d${Math.floor(seconds % 86400 / 3600)}h` : seconds >= 3600 ? `${Math.floor(seconds / 3600)}h${Math.floor(seconds % 3600 / 60)}m` : duration(seconds);
const compactReset = (meter, now) => stamp(meter?.reset_time) === 'Unknown' ? 'Unknown' : Date.parse(meter.reset_time) <= now ? 'due!' : compactDuration((Date.parse(meter.reset_time) - now) / 1000);
export function poolBlocker(snapshot, lane, now) {
  const raw = lane.ids.map((id, i) => [i ? 'weekly' : '5h', meterFor(snapshot, id)]);
  const present = raw.filter(([, meter]) => Boolean(meter.id));
  const entries = lane.optionalWindows && present.length ? present : raw;
  for (const [window, meter] of entries) {
    if (meter.disabled) return `${window} disabled`;
    if (!fraction(meter.remaining_fraction) || stamp(meter.reset_time) === 'Unknown') return `${window} unknown`;
    if (Date.parse(meter.reset_time) <= now) return `${window} due/unconfirmed`;
  }
  for (const [window, meter] of entries) if (fraction(meter.remaining_fraction) && meter.remaining_fraction === 0) return `${window} depleted`;
  const pool = array(snapshot.pools).find(p => lane.aliases.includes(p?.pool));
  return pool?.available === true ? 'available' : 'unverified';
}

// Ambient design; countdowns for active windows, no oversized telemetry.
export function dockLines(payload, now = Date.now(), width = 78,
  { retained = false, loading = false, action = 'status', elapsedMs = 0, error = null } = {}) {
  width = Math.max(1, Math.floor(width));
  const { snapshot, freshness, age, stale } = observation(payload, now, retained);
  let header = `Quota | ${stale ? 'last/unverified' : freshness} | age ${duration(age)} | health ${health(snapshot.health)}`;
  if (loading) header = acquisitionLines({ action, elapsedMs, width: Math.max(width, 70) })[0][1].trimEnd() + (payload ? ' | last/unverified' : ' | Unknown');
  else if (error || payload?.ok === false) header = `Quota | ${error ?? errorCode(payload)} | last/unverified | age ${duration(age)}`;
  const lines = [row(error || payload?.ok === false ? 'error' : loading ? 'primary' : 'label', header, width)];
  for (const lane of lanesFor(snapshot)) {
    const [five, weekly] = lane.ids.map(id => meterFor(snapshot, id));
    const blocker = poolBlocker(snapshot, lane, now);
    const status = stale && blocker === 'available' ? 'unverified' : blocker;
    const fiveCell = lane.optionalWindows && !five.id && weekly.id
      ? '5h    n/a      '
      : `5h ${percent(five.remaining_fraction).padStart(6)} ${compactReset(five, now)}`;
    const weeklyCell = lane.optionalWindows && !weekly.id && five.id
      ? 'weekly    n/a      '
      : `weekly ${percent(weekly.remaining_fraction).padStart(6)} ${compactReset(weekly, now)}`;
    lines.push(row(lane.tone, `${lane.pool.padEnd(10)} ${fiveCell} | ${weeklyCell} | ${status}`, width));
  }
  lines.push(row('muted', `Next 5h reset: ${nextFiveReset(snapshot, now)} | /agy-usage details`, width));
  return lines;
}

// Clean details table: ordinary digits, active meters, countdowns and health.
export function usageLines(payload, now = Date.now(), width = 70, { retained = false } = {}) {
  width = Math.max(1, Math.floor(width));
  if (!payload || payload.ok !== true) return [row('error', `Metadata unavailable (${errorCode(payload)}). R to retry.`, width)];
  const { snapshot, freshness, observed, age, stale } = observation(payload, now, retained);
  const lines = [
    row('label', `Cached: ${stale ? 'stale/unverified' : freshness} | health: ${health(snapshot.health)} | age: ${duration(age)}`, width),
    row('muted', `Observed: ${observed}`, width),
    row(stale ? 'error' : 'muted', stale ? 'LAST OBSERVATION / UNVERIFIED - no confirmed refill. R to recheck.' : 'Observed remaining quota / metadata only / no inference', width)
  ];
  for (const [id, label] of metersFor(snapshot)) {
    const meter = meterFor(snapshot, id);
    lines.push(row(id.startsWith('3p') ? 'primary' : 'ok', `${label.padEnd(19)} ${percent(meter.remaining_fraction).padStart(7)} remaining | reset in ${resetIn(meter, now)}`, width));
    lines.push(row('muted', `  Reset: ${stamp(meter.reset_time) === 'Unknown' ? 'Unknown' : meter.reset_time}${resetIn(meter, now) === 'due!' ? ' (due/unconfirmed)' : ''}`, width));
  }
  for (const lane of lanesFor(snapshot)) {
    const pool = array(snapshot.pools).find(p => lane.aliases.includes(p?.pool)) ?? {};
    const constraints = lane.ids.filter(id => array(pool.constrained_by).includes(id));
    const available = stale && pool.available === true ? 'last available (unverified)' : availability(pool.available);
    lines.push(row(stale ? 'error' : 'muted', `${lane.pool} pool: ${available} / ${poolBlocker(snapshot, lane, now)}${constraints.length ? ' / ' + constraints.map(id => id.endsWith('5h') ? '5h' : 'weekly').join('+') : ''}`, width));
  }
  return lines;
}
function parseArguments(arg) {
  const text = arg.trim();
  if (['', 'status', 'details', 'refresh'].includes(text)) return { action: text === 'refresh' ? 'refresh' : 'status', details: text === 'details' };
  if (text === 'alerts on' || text === 'alerts off') return { action: 'status', alerts: text === 'alerts on' };
  return null;
}

const emptyAlerts = () => ({ enabled: false, lastObserved: null, lastObservationId: null, levels: {}, notices: [], sequence: 0 });
export function observeAlerts(alerts, payload, now, baseline = false) {
  if (!alerts.enabled || payload?.ok !== true) return alerts;
  const { snapshot, stale, age } = observation(payload, now, false);
  const observed = Date.parse(snapshot.observed_at);
  const observationId = typeof snapshot.observation_id === 'string' ? snapshot.observation_id : null;
  if (stale || snapshot.freshness !== 'fresh' || health(snapshot.health) !== 'ok' || !finite(age) || age < 0 || !finite(observed) ||
      (alerts.lastObserved !== null && observed < alerts.lastObserved)) return { ...alerts, notices: [] };
  if (alerts.lastObserved !== null && observed === alerts.lastObserved && (!observationId || observationId === alerts.lastObservationId)) return alerts;
  const levels = { ...alerts.levels }, notices = [];
  for (const [id, label] of metersFor(snapshot)) {
    const meter = meterFor(snapshot, id), value = meter.remaining_fraction;
    if (meter.disabled || !fraction(value) || stamp(meter.reset_time) === 'Unknown') continue;
    if (!baseline && fraction(levels[id]) && levels[id] > .1 && value <= .1) notices.push({ id, label, value });
    levels[id] = value;
  }
  return { ...alerts, lastObserved: observed, lastObservationId: observationId, levels, notices, sequence: alerts.sequence + notices.length };
}

export default function register(sdk, { request = metadataRequest, clock = Date.now,
  timers = { setInterval: (fn, ms) => setInterval(fn, ms), clearInterval: id => clearInterval(id) } } = {}) {
  const { h, React, Text, Box, Dialog, Overlay } = sdk;
  let generation = 0, dockApp, detailsApp, dockState = null, alerts = emptyAlerts();

  const initial = (args, previous = null) => ({ surface: 'quota-dock-v1', mount: ++generation, request: 0,
    action: args.action,
    payload: previous?.payload?.ok === true ? previous.payload : null,
    phase: 'loading', error: null, offset: 0, startedAt: clock(), alerts });
  const remember = (app, state) => {
    if (app === dockApp) {
      if (state.phase === 'ready') alerts = observeAlerts(alerts, state.payload, clock());
      state = { ...state, alerts }; dockState = state;
    }
    return state;
  };
  const result = (current, payload) => payload?.ok === true
    ? { ...current, payload, phase: 'ready', error: null, receivedAt: clock() }
    : { ...current, payload: current.payload?.ok === true ? current.payload : payload, phase: 'failed', error: errorCode(payload) };

  function Viewer({ state, cols, rows = 24, t, ambient }) {
    const app = ambient ? dockApp : detailsApp;
    if (ambient) dockState = state;
    const [now, setNow] = React.useState(clock);
    React.useEffect(() => {
      // No backend calls: request elapsed, observed age and reset countdowns.
      const timer = timers.setInterval(() => setNow(clock()), state.phase === 'loading' ? 250 : 1000);
      return () => timers.clearInterval(timer);
    }, [state.mount, state.request, state.phase]);
    React.useEffect(() => () => {
      if (ambient && dockState?.mount === state.mount) { dockState = null; alerts = emptyAlerts(); }
    }, [state.mount]);
    React.useEffect(() => {
      if (state.phase !== 'loading') return;
      const controller = new AbortController();
      let mounted = true;
      const land = fn => {
        if (!mounted || controller.signal.aborted) return;
        sdk.updateWidget(app, current => current.mount === state.mount && current.request === state.request
          ? remember(app, fn(current)) : current);
      };
      Promise.resolve().then(() => controller.signal.aborted ? Promise.reject(new Error('cancelled')) : request({ action: state.action }, { signal: controller.signal }))
        .then(payload => land(current => result(current, payload)),
          () => land(current => ({ ...current, phase: 'failed', error: 'metadata_unavailable' })));
      return () => { mounted = false; controller.abort(); };
    }, [state.mount, state.request]);


    const width = Math.max(1, ambient ? cols - 4 : Math.min(92, cols - 4));
    const inner = Math.max(1, width - (ambient ? 2 : 6));
    const line = ([tone, text], key) => h(Text, { key, color: t.color[tone], wrap: 'truncate-end' }, fit(text, inner));
    let lines;
    if (ambient) {
      lines = dockLines(state.payload, now, inner, { retained: state.phase !== 'ready', loading: state.phase === 'loading',
        action: state.action, elapsedMs: now - state.startedAt, error: state.error });
      if (state.alerts?.enabled) {
        lines[lines.length - 1] = row('muted', '/agy-usage details | alerts on (<=10%, new observations)', inner);
        if (state.phase === 'ready' && !observation(state.payload, now, false).stale) {
          for (const lane of lanesFor(state.payload?.snapshot)) {
            const notices = state.alerts.notices.filter(n => lane.ids.includes(n.id));
            if (notices.length) lines.push(row('error', `Low quota: ${lane.pool} ${notices.map(n => `${n.id.endsWith('5h') ? '5h' : 'weekly'} ${percent(n.value)}`).join(', ')} remaining`, inner));
          }
        }
      }
    }
    else {
      lines = usageLines(state.payload, now, inner, { retained: state.phase !== 'ready' });
      if (state.phase === 'loading') {
        if (!state.payload) lines = [];
        lines.unshift(...acquisitionLines({ action: state.action, elapsedMs: now - state.startedAt, width: inner }));
      }
      if (state.error) lines.unshift(['error', `Metadata unavailable (${state.error}). R to retry.`]);
    }
    const content = h(Box, { flexDirection: 'column' }, ...lines.map(line));
    if (ambient) return h(Box, { flexDirection: 'column', width, paddingX: 1 }, content);
    const left = Math.max(0, Math.floor((cols - width) / 2));
    const top = Math.max(1, Math.min(2, Math.floor((rows - 16) / 2)));
    return h(Box, { position: 'absolute', top, left, width },
      h(Dialog, { width, title: 'Quota details',
        hint: line(['muted', 'R refresh | Esc/q close details | metadata only'], 'hint') }, content));
  }
  detailsApp = sdk.defineWidgetApp({
    id: 'agy-usage-details', mode: 'modal', help: 'Quota details; R refresh, Esc/q close details',
    usage: '/agy-usage-details [status|refresh]',
    init(arg = '') {
      const args = parseArguments(arg); if (!args || args.alerts !== undefined) return null;
      return initial(args, dockState);
    },
    reduce(state, { ch = '', key = {} }) {
      if (key.escape || ch === 'q' || ch === 'Q') return null;
      if (ch.toLowerCase() === 'r') return { ...state, request: state.request + 1, action: 'refresh', offset: 0, phase: 'loading', error: null, startedAt: clock() };
      return state;
    },
    render: ctx => h(Viewer, { ...ctx, ambient: false })
  });
  dockApp = sdk.defineWidgetApp({
    id: 'agy-usage', mode: 'ambient', zone: 'dock-top', help: 'Toggle cached quota dock; refresh or details via slash command',
    usage: '/agy-usage [status|refresh|details|alerts on|alerts off]',
    init(arg = '') {
      const args = parseArguments(arg); if (!args) return null;
      if (args.alerts !== undefined) {
        alerts = { ...alerts, enabled: args.alerts, notices: [] };
        if (args.alerts && dockState?.payload) alerts = observeAlerts({ ...alerts, lastObserved: null, levels: {} }, dockState.payload, clock(), true);
        return dockState ? remember(dockApp, { ...dockState, alerts }) : remember(dockApp, initial({ action: 'status' }));
      }
      if (args.details) {
        sdk.openWidget(detailsApp, detailsApp.init(''));
        // Public host opens details as modal, then keeps/opens this ambient dock.
        return dockState ?? remember(dockApp, initial({ action: 'status' }));
      }
      return remember(dockApp, initial(args, dockState));
    },
    reduce(state, { ch = '', key = {} }) {
      // Hot reload may retain the old modal slot; only unmarked legacy state can close.
      if (state.surface === undefined && (key.escape || ch === 'q' || ch === 'Q')) return null;
      return state; // New ambient state stays inert, including R/q/Esc.
    },
    render: ctx => h(Viewer, { ...ctx, ambient: true })
  });
}
