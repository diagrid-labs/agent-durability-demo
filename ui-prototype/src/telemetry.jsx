/* eslint-disable */
/**
 * Telemetry for Bank Heist Demo (Simple)
 *
 * Model:
 *  - N agents (default 100). Each agent is alive | restarting | dead | paused.
 *  - N customers (default 100), each starts at $100.00.
 *  - Each tick (~tickMs): every alive, unpaused agent does ONE transaction:
 *       picks one customer, +$1.00 to that customer's savings account.
 *  - Goal: every customer reaches $200 (i.e. 100 transactions per customer)
 *    over the run. With 100 agents x 100 customers and round-robin assignment
 *    (agent i serves customer i), 100 ticks per customer → fair.
 *    We use shuffled-bucket scheduling: each tick we draw a permutation so
 *    every customer is chosen by exactly one agent per tick (when N agents
 *    == N customers and all alive).
 *
 *  - Chaos modes:
 *       killRandom(n)   — n random agents go dead (auto-recover)
 *       killAZ()        — kill a contiguous block (33 agents)
 *       pauseFleet(ms)  — freeze all agents briefly
 *       latencyJitter() — slows ticks for a window
 *       dropTx()        — next tick: drop one tx, then Dapr re-delivers next tick
 *
 *  - Counters: txProcessed, txLost (always 0 on recovery), restarts.
 *
 *  - MCP server: simulated. On every Kth tx, the agent enriches the customer
 *    via mcp.query("SELECT * FROM customers WHERE id=...") with `mcpLatencyMs`.
 *    The server card shows last 30 req/resp and connection state.
 */

const TARGET_BAL = 200;
const START_BAL  = 100;

const FIRST_NAMES = ['Aiko','Bea','Chen','Mira','Esa','Faro','Gita','Hiro','Ines','Jonas',
  'Kira','Luca','Maya','Nadia','Omar','Priya','Quinn','Rosa','Sami','Tariq',
  'Uma','Vera','Wren','Xio','Yara','Zane','Asha','Bo','Cleo','Dev'];
const LAST_NAMES  = ['Okafor','Park','Wei','Volkov','Ruiz','Haddad','Lind','Tana','Osei','Roth',
  'Park','Costa','Singh','Aboud','Khan','Patel','Walsh','Diaz','Reed','Cole'];

function nameFor(id) {
  const f = FIRST_NAMES[(id * 7) % FIRST_NAMES.length];
  const l = LAST_NAMES[(id * 13 + 3) % LAST_NAMES.length];
  return `${f} ${l}`;
}

function nowMs() { return performance.now(); }

function createTelemetry(initial) {
  const cfg = {
    agentCount: 100,
    customerCount: 10,
    tickMs: 1200,       // every alive agent does 1 tx per tick
    mcpLatencyMs: 80,
    mcpEvery: 1,        // MCP enriched on every tx (turn down to reduce log spam)
    ...(initial || {}),
  };

  const listeners = new Set();
  let paused = false;

  const state = {
    cfg,
    clock: 0,
    startedAt: null,
    agents: [],
    customers: [],
    counters: { txProcessed: 0, txLost: 0, restarts: 0, mcpQueries: 0 },
    chaos: { activeWave: null, azDown: null, latencyUntil: 0, fleetPausedUntil: 0, dropNext: 0 },
    mcp: { connected: true, lines: [] }, // lines: {id, kind:'req'|'res'|'sys', text, ts}
    activity: [], // recent {agentId, customerId, ts} for highlight
  };

  function rebuild() {
    state.agents = Array.from({ length: cfg.agentCount }, (_, i) => ({
      id: i,
      label: `agent-${String(i+1).padStart(3,'0')}`,
      status: 'idle',      // idle (not yet started) | alive | restarting | dead
      restartingUntil: 0,
      txCount: 0,
      activatedAt: 0,
    }));
    state.customers = Array.from({ length: cfg.customerCount }, (_, i) => ({
      id: i,
      name: nameFor(i),
      balance: START_BAL,
      txCount: 0,
      lastTxAt: 0,
      enriched: false,
      tier: null,
    }));
    state.counters = { txProcessed: 0, txLost: 0, restarts: 0, mcpQueries: 0 };
    state.chaos = { activeWave: null, azDown: null, latencyUntil: 0, fleetPausedUntil: 0, dropNext: 0 };
    state.mcp = { connected: true, lines: [] };
    state.activity = [];
    state.startedAt = nowMs();
    state.clock = 0;
    nextActivationIdx = 0;
    nextActivationAt = state.startedAt + 50;
    pushMcp('sys', 'CONNECT mcp://postgres.bank.svc.diagrid.io · session opened');
    pushMcp('sys', 'PRAGMA target_balance = 200.00 · ' + cfg.customerCount + ' customer rows ready');
  }

  let mcpSeq = 0;
  function pushMcp(kind, text) {
    state.mcp.lines.unshift({ id: ++mcpSeq, kind, text, ts: state.clock });
    if (state.mcp.lines.length > 60) state.mcp.lines.length = 60;
  }

  function emit() { for (const fn of listeners) fn(state); }

  let last = nowMs();
  let nextTickAt = nowMs() + cfg.tickMs;
  let nextActivationIdx = 0;
  let nextActivationAt = nowMs() + 50;
  const ACTIVATION_INTERVAL_MS = 60; // agents come online ~60ms apart

  function loop() {
    const t = nowMs();
    const dt = t - last; last = t;
    if (!paused) {
      state.clock += dt;

      // Sequential activation: bring agents online one at a time
      while (nextActivationIdx < state.agents.length && t >= nextActivationAt) {
        const a = state.agents[nextActivationIdx];
        if (a && a.status === 'idle') {
          a.status = 'alive';
          a.activatedAt = t;
        }
        nextActivationIdx++;
        nextActivationAt = t + ACTIVATION_INTERVAL_MS;
      }

      // Auto-recover restarting agents
      for (const a of state.agents) {
        if (a.status === 'restarting' && t >= a.restartingUntil) {
          a.status = 'alive';
        }
        if (a.status === 'dead' && t >= a.restartingUntil) {
          a.status = 'restarting';
          a.restartingUntil = t + 800 + Math.random() * 600;
          state.counters.restarts++;
        }
      }

      // AZ recovery
      if (state.chaos.azDown && t >= state.chaos.azDown.endsAt) {
        const ids = state.chaos.azDown.ids;
        for (const id of ids) {
          const a = state.agents[id];
          if (a && a.status === 'dead') {
            a.status = 'restarting';
            a.restartingUntil = t + 600 + Math.random() * 800;
            state.counters.restarts++;
          }
        }
        state.chaos.azDown = null;
      }

      // Tick: each alive agent does one tx targeting a RANDOM not-done customer
      const fleetPaused = t < state.chaos.fleetPausedUntil;
      const lat = t < state.chaos.latencyUntil ? 1.8 : 1.0;
      const effectiveTick = cfg.tickMs * lat;

      if (!fleetPaused && t >= nextTickAt) {
        nextTickAt = t + effectiveTick;
        const M = state.customers.length;
        // Build pool of customers that still need money this tick
        const pool = [];
        for (let i = 0; i < M; i++) {
          if (state.customers[i].balance < TARGET_BAL) pool.push(i);
        }
        let dropped = false;
        for (let i = 0; i < state.agents.length; i++) {
          const agent = state.agents[i];
          if (agent.status !== 'alive') continue;
          if (pool.length === 0) break;
          const cIdx = pool[Math.floor(Math.random() * pool.length)];
          const cust = state.customers[cIdx];
          if (!cust) continue;
          if (state.chaos.dropNext > 0 && !dropped) {
            dropped = true;
            state.chaos.dropNext--;
            pushMcp('sys', `tx redelivery scheduled · ${agent.label} → cust-${String(cust.id+1).padStart(3,'0')}`);
            continue;
          }
          if (cust.balance >= TARGET_BAL) continue;
          cust.balance = Math.min(TARGET_BAL, cust.balance + 1);
          cust.txCount++;
          cust.lastTxAt = t;
          agent.txCount++;
          state.counters.txProcessed++;

          state.activity.unshift({ agentId: agent.id, customerId: cust.id, ts: t });
          if (state.activity.length > 60) state.activity.length = 60;

          // Drop customer from pool once full
          if (cust.balance >= TARGET_BAL) {
            const idx = pool.indexOf(cIdx);
            if (idx >= 0) pool.splice(idx, 1);
          }

          // MCP enrichment
          if (!cust.enriched && cfg.mcpEvery > 0 && (cust.txCount % cfg.mcpEvery) === 0) {
            pushMcp('req', `SELECT id,name,tier,risk FROM customers WHERE id=${cust.id+1}`);
            const lat = cfg.mcpLatencyMs;
            setTimeout(() => {
              const tiers = ['bronze','silver'];
              const tier = tiers[(cust.id * 3 + 5) % 2];
              cust.enriched = true;
              cust.tier = tier;
              state.counters.mcpQueries++;
              pushMcp('res', `1 row · id=${cust.id+1} tier=${tier} risk=low (${lat}ms)`);
              emit();
            }, lat);
          }
        }
      }

      // Chaos wave: kill cells across a sweep
      if (state.chaos.activeWave) {
        const w = state.chaos.activeWave;
        const elapsed = t - w.startedAt;
        if (elapsed >= w.duration) {
          state.chaos.activeWave = null;
        }
      }
    }
    emit();
    rafId = setTimeout(loop, 60);
  }
  let rafId;

  rebuild();
  rafId = setTimeout(loop, 60);

  const control = {
    isPaused() { return paused; },
    setPaused(b) { paused = !!b; emit(); },
    reset() {
      rebuild();
      paused = false;
      emit();
    },
    setCfg(patch) {
      const old = { ...cfg };
      Object.assign(cfg, patch);
      // If counts changed, rebuild
      if (patch.agentCount !== undefined && patch.agentCount !== old.agentCount) {
        rebuild();
      } else if (patch.customerCount !== undefined && patch.customerCount !== old.customerCount) {
        rebuild();
      }
      emit();
    },

    // Chaos
    killRandom(n) {
      const t = nowMs();
      const alive = state.agents.filter(a => a.status === 'alive');
      const pick = alive.sort(() => Math.random() - 0.5).slice(0, Math.min(n, alive.length));
      for (const a of pick) {
        a.status = 'dead';
        a.restartingUntil = t + 1200 + Math.random() * 1500;
      }
      state.chaos.activeWave = { startedAt: t, duration: 1500, level: n / state.agents.length };
      pushMcp('sys', `chaos · killed ${pick.length} agents · auto-restart in ~1-3s`);
      emit();
    },
    killAZ() {
      const t = nowMs();
      const start = Math.floor(state.agents.length / 3);
      const end = Math.floor((state.agents.length * 2) / 3);
      const ids = [];
      for (let i = start; i < end; i++) {
        const a = state.agents[i];
        if (!a) continue;
        a.status = 'dead';
        a.restartingUntil = Infinity;
        ids.push(i);
      }
      state.chaos.azDown = { ids, startedAt: t, endsAt: t + 5000 };
      pushMcp('sys', `chaos · AZ failure · ${ids.length} agents offline · 5s`);
      emit();
    },
    pauseFleet(ms = 3000) {
      const t = nowMs();
      state.chaos.fleetPausedUntil = t + ms;
      pushMcp('sys', `chaos · fleet frozen for ${ms/1000}s`);
      emit();
    },
    latencyJitter(ms = 4000) {
      state.chaos.latencyUntil = nowMs() + ms;
      pushMcp('sys', `chaos · network latency injected · ${ms/1000}s window`);
      emit();
    },
    dropTx() {
      state.chaos.dropNext = (state.chaos.dropNext || 0) + 1;
      pushMcp('sys', `chaos · 1 tx will be dropped · Dapr will redeliver`);
      emit();
    },
    fireAll() {
      // Demo: stagger several
      const t = nowMs();
      this.killRandom(20);
      setTimeout(() => this.latencyJitter(3000), 600);
      setTimeout(() => this.dropTx(), 1200);
      setTimeout(() => this.pauseFleet(1500), 1800);
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
