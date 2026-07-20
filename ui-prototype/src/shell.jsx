/* eslint-disable */
const { useState, useEffect, useMemo, useRef, useSyncExternalStore } = React;

function useTelemetry(feed) {
  if (!feed.__sub) {
    feed.__v = 0;
    feed.__sub = (cb) => {
      let first = true;
      return feed.subscribe(() => {
        if (first) { first = false; return; }
        feed.__v++;
        cb();
      });
    };
  }
  useSyncExternalStore(feed.__sub, () => feed.__v, () => feed.__v);
  return feed.state;
}

function fmtMoney(n) {
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtInt(n) { return Math.round(n).toLocaleString('en-US'); }
function fmtClock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

/* ================ TopBar — Catalyst-docs style ================ */
function TopBar({ state, feed, onTogglePause }) {
  const run = state.run || {};

  return (
    <div className="topbar">
      <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <img src="assets/diagrid-logo.png" alt="Diagrid" style={{ height: 26, width: 'auto', display: 'block' }} />
          <span style={{ color: 'var(--line-strong)', fontSize: 18, fontWeight: 300 }}>/</span>
          <span style={{ fontSize: 14, color: 'var(--fg-1)', fontWeight: 500 }}>Bank Creditor Demo</span>
          {run.executionRunId != null && (
            <span className="pill" style={{ fontSize: 11, padding: '2px 8px' }}
                  title="Active execution run">
              Run #{run.executionRunId}
            </span>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <span className="label" style={{ color: 'var(--fg-2)' }}>clock</span>
        <span className="mono" style={{ fontSize: 14, color: 'var(--fg)', fontWeight: 500 }}>
          {fmtClock(state.clock)}
        </span>
        {run.active
          ? <button className="btn sm" onClick={() => feed.control.stopRun()}>⏹ Stop run</button>
          : <button className="btn sm primary" disabled={!run.reachable}
              onClick={() => feed.control.startRun()}>▶ Start run</button>}
        <button className="btn sm ghost" onClick={onTogglePause}>
          {feed.control.isPaused() ? '▶ Resume' : '❚❚ Pause'}
        </button>
        <button className="btn sm ghost" onClick={() => feed.control.reset()}>↺ Reset</button>
      </div>
    </div>
  );
}

function DiagridMark() { return null; }

/* ================ Counters strip ================ */
function Counters({ state }) {
  const completed = state.customers.filter(c => c.balance >= TARGET_BAL).length;
  const totalAgents = state.agents.length;
  const alive = state.agents.filter(a => a.status === 'alive').length;
  const totalCustomers = state.customers.length;
  const targetTotal = totalCustomers * TARGET_BAL;
  const currentTotal = state.customers.reduce((s, c) => s + c.balance, 0);
  const pct = (currentTotal - totalCustomers * START_BAL) / Math.max(1, targetTotal - totalCustomers * START_BAL);

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(5, 1fr)',
      gap: 10,
      padding: '10px 14px 0',
    }}>
      <Counter label="Transactions processed" value={fmtInt(state.counters.txProcessed)} sub="$1 per tx" />
      <Counter label="Transactions lost" value={fmtInt(state.counters.txLost)}
        accent={state.counters.txLost === 0 ? 'var(--green)' : 'var(--red)'}
        sub={state.counters.txLost === 0 ? 'durable execution' : 'investigate'} />
      <Counter label="Agent restarts" value={fmtInt(state.counters.restarts)} sub="auto-recovered" />
      <Counter label="Agents alive" value={`${alive} / ${totalAgents}`}
        accent={alive === totalAgents ? 'var(--green)' : alive / totalAgents > 0.7 ? 'var(--amber)' : 'var(--red)'}
        sub={`${((alive/totalAgents)*100).toFixed(0)}% online`} />
      <Counter label="Customers at target" value={`${completed} / ${totalCustomers}`}
        accent={completed === totalCustomers ? 'var(--green)' : 'var(--accent)'}
        sub={`${(pct*100).toFixed(1)}% of run`} progress={Math.min(1, Math.max(0, pct))} />
    </div>
  );
}

function Counter({ label, value, sub, accent, progress }) {
  return (
    <div className="card" style={{ padding: '8px 12px' }}>
      <div className="label" style={{ fontSize: 9, marginBottom: 2 }}>{label}</div>
      <div className="hero-num" style={{ fontSize: 20, color: accent || 'var(--fg)', lineHeight: 1.15 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: 'var(--fg-3)', marginTop: 1 }}>{sub}</div>}
      {progress !== undefined && (
        <div className="bal-track" style={{ marginTop: 4, height: 3 }}>
          <div className="bal-fill" style={{ width: `${(progress*100).toFixed(1)}%` }} />
        </div>
      )}
    </div>
  );
}

Object.assign(window, { useTelemetry, TopBar, Counters, fmtMoney, fmtInt, fmtClock });
