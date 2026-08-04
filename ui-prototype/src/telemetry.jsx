/* eslint-disable */
/**
 * Telemetry — live mode. Polls orchestrator/customer/mcp-log endpoints and
 * projects responses into UI state. `dropTx`/`latencyJitter` call real
 * server endpoints; `killRandom`/`killAZ` also animate client-side (no
 * chaos-mesh in this demo).
 */

const TARGET_BAL = 200;
const START_BAL  = 100;

// Same origin when served from FastAPI at /. Override via window.API_BASE
// (e.g. opening the HTML from file:// against a different port).
const API_BASE = (typeof window !== 'undefined' && window.API_BASE) || '';

function nowMs() { return performance.now(); }

async function getJSON(path) {
  const r = await fetch(API_BASE + path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

async function postJSON(path, body) {
  const r = await fetch(API_BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? '{}' : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function createTelemetry(initial) {
  const cfg = {
    agentCount: 10,
    customerCount: 10,
    // Slow poll for reconciliation; per-tx updates arrive over WebSocket instead.
    tickMs: 1500,
    mcpLatencyMs: 80,    // unused server-side; kept for tweaks-panel compat
    ...(initial || {}),
  };

  const listeners = new Set();
  let paused = false;

  const state = {
    cfg,
    clock: 0,
    startedAt: nowMs(),
    agents: [],
    customers: [],
    counters: { txProcessed: 0, txLost: 0, restarts: 0, mcpQueries: 0 },
    chaos: {
      activeWave: null, azDown: null, latencyUntil: 0, dropNext: 0,
      // Pod fleet from /chaos/pods. `victim` = pod with the most workflows,
      // for the biggest-blast-radius Kill click.
      pods: [],
      victim: null,
      // AKS nodepools/nodes from /chaos/infra.
      nodepools: [],
      nodes: [],
      // impactedZones[zone] = unix-ms-until; pulses that zone's nodes in the UI.
      impactedZones: {},
    },
    mcp: { connected: false, lines: [] },
    activity: [],
    run: { active: false, spawnCount: 0, target: 0, error: null, reachable: false, executionRunId: null },
  };

  let lastApplied = 0;     // applied_total observed last tick
  let lastMcpId = 0;       // highest mcp log id seen
  let firstStatus = true;

  function emit() { for (const fn of listeners) fn(state); }

  function rebuildAgents() {
    state.agents = Array.from({ length: cfg.agentCount }, (_, i) => ({
      id: i,
      label: `workflow-${String(i+1).padStart(3,'0')}`,
      status: 'idle',
      restartingUntil: 0,
      txCount: 0,
      activatedAt: state.startedAt,
    }));
  }

  function ensureCustomerScaffold(n) {
    if (state.customers.length === n) return;
    state.customers = Array.from({ length: n }, (_, i) => ({
      id: i,
      name: `cust-${i+1}`,
      balance: START_BAL,
      txCount: 0,
      lastTxAt: 0,
      enriched: false,
      tier: null,
    }));
  }

  function pushMcpLine(kind, text, ts) {
    state.mcp.lines.unshift({ id: ++lastMcpId, kind, text, ts: ts ?? state.clock });
    if (state.mcp.lines.length > 60) state.mcp.lines.length = 60;
  }

  function distributeActivity(newTx) {
    if (newTx <= 0) return;
    // Client-side fallback for the WS-driven path — agent i is always
    // bound to customer i, so pair them directly.
    const alive = state.agents.filter(a => a.status === 'alive');
    if (alive.length === 0) return;
    const t = nowMs();
    const burst = Math.min(newTx, Math.max(20, alive.length));
    for (let i = 0; i < burst; i++) {
      const a = alive[Math.floor(Math.random() * alive.length)];
      const c = state.customers[a.id];
      a.txCount++;
      state.activity.unshift({ agentId: a.id, customerId: c ? c.id : a.id, ts: t });
    }
    if (state.activity.length > 120) state.activity.length = 120;
  }

  async function pollOnce() {
    if (paused) return;
    // Clock only advances while a run is active.
    if (state.run.active) {
      state.clock = nowMs() - state.startedAt;
    }

    // Local-only chaos timers
    const t = nowMs();
    for (const a of state.agents) {
      if (a.status === 'dead' && a.restartingUntil && t >= a.restartingUntil) {
        a.status = 'restarting';
        a.restartingUntil = t + 700 + Math.random() * 600;
        state.counters.restarts++;
      } else if (a.status === 'restarting' && a.restartingUntil && t >= a.restartingUntil) {
        a.status = state.run.active ? 'alive' : 'idle';
        a.restartingUntil = 0;
      }
    }
    if (state.chaos.azDown && t >= state.chaos.azDown.endsAt) {
      for (const id of state.chaos.azDown.ids) {
        const a = state.agents[id];
        if (a && a.status === 'dead') {
          a.status = 'restarting';
          a.restartingUntil = t + 800 + Math.random() * 700;
          state.counters.restarts++;
        }
      }
      state.chaos.azDown = null;
    }

    // Best-effort — failure just means the worker isn't up.
    try {
      const agent = await getJSON('/agent/status');
      state.run.reachable = true;
      state.run.error = null;
      state.run.active = !!agent.replenisher_running;
      state.run.spawnCount = Number(agent.spawn_count ?? 0);
      state.run.target = Number(agent.target_concurrency ?? 0);
    } catch (err) {
      state.run.reachable = false;
      state.run.active = false;
      state.run.error = err.message || String(err);
    }

    // Chaos states (dead/restarting) take priority — don't overwrite mid-recovery.
    const desired = state.run.active ? 'alive' : 'idle';
    for (const a of state.agents) {
      if (a.status !== 'dead' && a.status !== 'restarting') {
        a.status = desired;
      }
    }

    try {
      const [status, customers, mcpLog, podsResp, infraResp] = await Promise.all([
        getJSON('/orch/status'),
        getJSON('/orch/customers'),
        getJSON('/orch/mcp-log'),
        getJSON('/chaos/pods').catch(() => ({ pods: [], available: false })),
        getJSON('/chaos/infra').catch(() => ({ nodepools: [], nodes: [], available: false })),
      ]);
      state.chaos.nodepools = Array.isArray(infraResp.nodepools) ? infraResp.nodepools : [];
      state.chaos.nodes = Array.isArray(infraResp.nodes) ? infraResp.nodes : [];
      // Reconcile in case we missed the initial WS event (e.g. page refreshed).
      const serverImpacts = infraResp.impacted_zones || {};
      const merged = { ...state.chaos.impactedZones };
      for (const [zone, info] of Object.entries(serverImpacts)) {
        const remaining = Number(info.remaining_s || 0) * 1000;
        if (remaining > 0) {
          const until = Date.now() + remaining;
          if (!merged[zone] || merged[zone] < until) merged[zone] = until;
        }
      }
      for (const z of Object.keys(merged)) {
        if (merged[z] <= Date.now()) delete merged[z];
      }
      state.chaos.impactedZones = merged;
      // Victim = pod hosting the most workflows; tiebreak by name for a stable label.
      const pods = Array.isArray(podsResp.pods) ? podsResp.pods : [];
      state.chaos.pods = pods;
      state.chaos.victim = pods.length
        ? pods.slice().sort((a, b) => {
            const dc = (b.workflow_count || 0) - (a.workflow_count || 0);
            return dc !== 0 ? dc : a.pod.localeCompare(b.pod);
          })[0]
        : null;
      // Aggregate per zone; busiest zone is the AZ-kill victim.
      const byZone = new Map();
      for (const p of pods) {
        if (!p.zone) continue;
        const cur = byZone.get(p.zone) || { zone: p.zone, pods: 0, workflow_count: 0 };
        cur.pods += 1;
        cur.workflow_count += (p.workflow_count || 0);
        byZone.set(p.zone, cur);
      }
      const zones = Array.from(byZone.values()).sort((a, b) =>
        (b.workflow_count - a.workflow_count) || a.zone.localeCompare(b.zone)
      );
      state.chaos.zones = zones;
      state.chaos.zoneVictim = zones[0] || null;

      state.mcp.connected = true;

      // Real balances from Postgres
      ensureCustomerScaffold(customers.length || cfg.customerCount);
      for (const row of customers) {
        const idx = (row.id ?? row.customer_id ?? 1) - 1;
        if (idx < 0 || idx >= state.customers.length) continue;
        const c = state.customers[idx];
        const prevBal = c.balance;
        const newBal = Number(row.balance ?? c.balance);
        if (newBal !== prevBal) c.lastTxAt = nowMs();
        c.balance = newBal;
        c.name = row.name || c.name;
        c.tier = row.tier ?? c.tier;
        c.enriched = c.tier != null;
        c.txCount = Math.max(0, Math.round(newBal - START_BAL));
      }

      if (status.execution_run_id != null) {
        state.run.executionRunId = Number(status.execution_run_id);
        // Filters WS tx so stragglers from a just-reset run are ignored.
        wsExpectedRun = state.run.executionRunId;
      }

      const applied = Number(status.applied_total ?? 0);
      const delta = firstStatus ? 0 : Math.max(0, applied - lastApplied);
      firstStatus = false;
      lastApplied = applied;
      state.counters.txProcessed = applied;
      state.counters.mcpQueries = Number(status.mcp_queries ?? mcpLog.queries ?? 0);
      // tx lost stays 0 — the durability story.

      distributeActivity(delta);

      // Server already keeps a 60-entry ring; replace wholesale.
      state.mcp.lines = (mcpLog.lines || []).slice().reverse().map(l => ({
        id: l.id, kind: l.kind, text: l.text, ts: l.ts,
      }));
      if (state.mcp.lines.length > 60) state.mcp.lines.length = 60;
    } catch (err) {
      state.mcp.connected = false;
      // Only log the first failure per outage.
      if (!state.mcp.lines.length || state.mcp.lines[0].kind !== 'sys' ||
          !state.mcp.lines[0].text.startsWith('disconnected')) {
        pushMcpLine('sys', `disconnected — ${err.message || err}`);
      }
    }

    emit();
  }

  let timer = null;
  function startPolling() {
    if (timer) return;
    const loop = async () => {
      await pollOnce();
      timer = setTimeout(loop, Math.max(150, cfg.tickMs));
    };
    loop();
  }

  // --- WebSocket push channel ---
  // Server forwards each pg_notify('tx_committed') row as `{type:'tx', ...}`.
  // Reconnects with backoff; the poll loop repairs any drift from missed frames.
  let ws = null;
  let wsRetryMs = 200;
  let wsExpectedRun = null;
  function applyTx(msg) {
    if (msg.execution_run_id != null && wsExpectedRun != null &&
        Number(msg.execution_run_id) !== wsExpectedRun) {
      return; // stale tx from a prior run
    }
    const cid = Number(msg.customer_id);
    const idx = cid - 1;
    if (idx >= 0 && idx < state.customers.length) {
      const c = state.customers[idx];
      const amount = Number(msg.amount) || 0;
      const target = c.target || TARGET_BAL;
      const next = Math.min(target, c.balance + amount);
      if (next !== c.balance) c.lastTxAt = nowMs();
      c.balance = next;
      c.txCount = (c.txCount || 0) + 1;
    }
    // Each customer's agent shares its index.
    const agent = (idx >= 0 && idx < state.agents.length) ? state.agents[idx] : null;
    if (agent) {
      agent.txCount = (agent.txCount || 0) + 1;
      state.activity.unshift({ agentId: agent.id, customerId: cid, ts: nowMs() });
      if (state.activity.length > 120) state.activity.length = 120;
    }
    emit();
  }
  function openWs() {
    const proto = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss' : 'ws';
    const host = (typeof location !== 'undefined') ? location.host : 'localhost:8000';
    try {
      ws = new WebSocket(`${proto}://${host}/ws/telemetry`);
    } catch (_e) {
      setTimeout(openWs, Math.min(wsRetryMs *= 2, 5000));
      return;
    }
    ws.onopen = () => { wsRetryMs = 200; };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (!msg || !msg.type) return;
        if (msg.type === 'tx') {
          applyTx(msg);
          // A tx proves that customer's agent is alive — relight if marked dead.
          const idx = Number(msg.customer_id) - 1;
          const a = state.agents[idx];
          if (a && a.status !== 'alive') {
            a.status = 'alive';
            a.restartingUntil = 0;
          }
        } else if (msg.type === 'zone-state' && msg.zone) {
          const ttl = Number(msg.ttl_seconds || 6) * 1000;
          state.chaos.impactedZones = {
            ...state.chaos.impactedZones,
            [msg.zone]: Date.now() + ttl,
          };
          emit();
        } else if (msg.type === 'slot-state' && Array.isArray(msg.slots)) {
          const t = nowMs();
          const status = msg.status === 'dead' ? 'dead' : 'alive';
          for (const s of msg.slots) {
            const a = state.agents[Number(s) - 1];
            if (!a) continue;
            if (status === 'dead') {
              a.status = 'dead';
              a.restartingUntil = t + 1500 + Math.random() * 1500;
            } else {
              a.status = 'alive';
              a.restartingUntil = 0;
            }
          }
          emit();
        }
      } catch (_e) { /* drop malformed */ }
    };
    ws.onclose = () => {
      ws = null;
      setTimeout(openWs, Math.min(wsRetryMs *= 2, 5000));
    };
    ws.onerror = () => { try { ws && ws.close(); } catch (_e) {} };
  }

  rebuildAgents();
  ensureCustomerScaffold(cfg.customerCount);
  openWs();
  startPolling();

  const control = {
    isPaused() { return paused; },
    setPaused(b) { paused = !!b; emit(); },
    async reset() {
      // Stop replenisher first so stragglers drain before resetting balances.
      try { await postJSON('/agent/stop'); } catch (_e) {}
      await new Promise(r => setTimeout(r, 400));
      try {
        await postJSON('/orch/reset', { customers: cfg.customerCount, credits_per_customer: 100, target: 200 });
        await postJSON('/chaos/reset', {});
        await postJSON('/orch/mcp-log/clear', {});
      } catch (_e) {}
      lastApplied = 0;
      firstStatus = true;
      lastMcpId = 0;
      state.counters = { txProcessed: 0, txLost: 0, restarts: 0, mcpQueries: 0 };
      state.activity = [];
      state.chaos = { activeWave: null, azDown: null, latencyUntil: 0, dropNext: 0 };
      state.mcp.lines = [];
      state.startedAt = nowMs();
      state.clock = 0;
      rebuildAgents();
      emit();
    },
    setCfg(patch) {
      const old = { ...cfg };
      Object.assign(cfg, patch);
      if (patch.agentCount !== undefined && patch.agentCount !== old.agentCount) {
        rebuildAgents();
      }
      emit();
    },

    // Pod-level chaos — visual only (chaos-mesh out of scope)
    killRandom(_n) {
      // Kills the exact `victim` pod shown on the button label; cell-darkening
      // is driven by the server's WS `slot-state="dead"` broadcast.
      const victim = state.chaos.victim;
      const t = nowMs();
      state.chaos.activeWave = { startedAt: t, duration: 1500, level: 0.3 };
      setTimeout(() => { state.chaos.activeWave = null; emit(); }, 1600);
      emit();
      const body = victim ? { pod: victim.pod } : {};
      postJSON('/chaos/pod-kill', body).catch(() => {});
    },
    killAZ() {
      // Server deletes every agent pod in the busiest zone; surviving pods
      // backfill via the replenisher's /schedule-one calls.
      const z = state.chaos.zoneVictim;
      const t = nowMs();
      state.chaos.activeWave = { startedAt: t, duration: 1500, level: 0.5 };
      setTimeout(() => { state.chaos.activeWave = null; emit(); }, 1600);
      emit();
      postJSON('/chaos/az-kill', z ? { zone: z.zone } : {}).catch(() => {});
    },
    // Agent worker control (MCP server's /agent/spawn drives its in-process Replenisher)
    async startRun(opts) {
      const body = {
        customers: Number((opts && opts.customers) ?? cfg.customerCount),
        credits_per_customer: Number((opts && opts.creditsPerCustomer) ?? 100),
        target: Number((opts && opts.target) ?? 200),
      };
      try {
        const r = await postJSON('/agent/spawn', body);
        // Fresh run — wipe per-slot counters so the heatmap doesn't carry
        // forward the previous run.
        rebuildAgents();
        state.run.active = true;
        state.run.target = body.customers;
        state.run.error = null;
        state.startedAt = nowMs();
        state.clock = 0;
        emit();
        return r;
      } catch (err) {
        state.run.error = err.message || String(err);
        emit();
        throw err;
      }
    },
    async stopRun() {
      try {
        await postJSON('/agent/stop');
        state.run.active = false;
        emit();
      } catch (err) {
        state.run.error = err.message || String(err);
        emit();
      }
    },

    // Real server chaos
    async latencyJitter(durationMs = 10000) {
      state.chaos.latencyUntil = nowMs() + durationMs;
      try { await postJSON('/chaos/latency', { ms: 3000, duration_ms: durationMs }); } catch (_e) {}
      emit();
    },
    async dropTx() {
      state.chaos.dropNext = (state.chaos.dropNext || 0) + 1;
      try { await postJSON('/chaos/drop', { count: 1 }); } catch (_e) {}
      emit();
    },
    fireAll() {
      this.killRandom(20);
      setTimeout(() => this.latencyJitter(3000), 600);
      setTimeout(() => this.dropTx(), 1200);
      setTimeout(() => this.killAZ(), 2400);
    },
  };

  return {
    get state() { return state; },
    subscribe(fn) { listeners.add(fn); fn(state); return () => listeners.delete(fn); },
    control,
  };
}

window.createTelemetry = createTelemetry;
window.TARGET_BAL = TARGET_BAL;
window.START_BAL = START_BAL;
