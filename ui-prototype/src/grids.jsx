/* eslint-disable */
const { useMemo: uM2, useEffect: uE2, useState: uS2 } = React;

/* ================ Agents grid — 10 rows, one per account-bound instance ================ */
function AgentsGrid({ state }) {
  const agents = state.agents;
  const customers = state.customers;
  const recentMap = uM2(() => {
    const m = new Map();
    for (const a of state.activity) {
      if (!m.has(a.agentId)) m.set(a.agentId, a.ts);
    }
    return m;
  }, [state.activity]);

  // No memo — agents are mutated in place, so memoizing on the array
  // reference would freeze these counts. 10 elements, trivial either way.
  let alive = 0, restarting = 0, dead = 0, idle = 0;
  for (const a of agents) {
    if (a.status === 'alive') alive++;
    else if (a.status === 'restarting') restarting++;
    else if (a.status === 'dead') dead++;
    else if (a.status === 'idle') idle++;
  }
  const counts = { alive, restarting, dead, idle };

  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label-md" style={{ color: 'var(--fg)' }}>Agents</span>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          {counts.idle > 0 && <Legend color="var(--fg-3)" label={`idle ${counts.idle}`} />}
          <Legend color="var(--green)" label={`alive ${counts.alive}`} />
          <Legend color="var(--amber)" label={`restarting ${counts.restarting}`} />
          <Legend color="var(--red)" label={`dead ${counts.dead}`} />
        </div>
      </div>
      <div style={{
        flex: 1, padding: '10px 14px', minHeight: 0, overflow: 'hidden',
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr)',
        gridTemplateRows: 'repeat(10, minmax(0, 1fr))',
        gap: 6,
      }}>
        {agents.map(a => {
          const recentTs = recentMap.get(a.id);
          const recent = recentTs && (performance.now() - recentTs < 350);
          return <AgentRow key={a.id} a={a} customer={customers[a.id]} recent={recent} />;
        })}
      </div>
    </div>
  );
}

function AgentRow({ a, customer, recent }) {
  // Derived from the account's actual balance, not a counter — can't
  // exceed 100 or drift from double-counted WS events.
  const rawCredited = customer ? (customer.balance - START_BAL) : (a.txCount || 0);
  const credited = Math.max(0, Math.min(100, Math.round(rawCredited)));
  const pct = credited / 100;

  // One status axis drives the row's color; no separate text label.
  let color = 'var(--green)', cardBg = 'var(--green-soft)', cardBd = '#c8e6d4';
  if (a.status === 'idle') {
    color = 'var(--fg-3)'; cardBg = '#f4f5f7'; cardBd = '#e3e6eb';
  } else if (a.status === 'restarting') {
    color = 'var(--amber)'; cardBg = 'var(--amber-soft)'; cardBd = '#f3deb3';
  } else if (a.status === 'dead') {
    color = 'var(--red)'; cardBg = 'var(--red-soft)'; cardBd = '#f1c5c8';
  }

  return (
    <div title={`agent-${String(a.id + 1).padStart(3, '0')} · crediting ${customer ? customer.name : 'no one yet'} · $${credited}/100`}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '6px 12px',
        background: cardBg,
        border: `1px solid ${cardBd}`,
        borderRadius: 8,
        minWidth: 0, minHeight: 0,
        boxShadow: recent && a.status === 'alive' ? '0 0 0 3px var(--accent-soft)' : 'none',
        transition: 'background 360ms ease, border-color 240ms ease, box-shadow 240ms ease',
      }}>
      {/* Identity avatar */}
      <div style={{
        width: 26, height: 26, borderRadius: '50%',
        background: color,
        color: '#fff', fontWeight: 600, fontSize: 11,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        letterSpacing: '-0.01em',
        transition: 'background 360ms ease',
        flexShrink: 0,
      }}>
        {String(a.id + 1).padStart(2, '0')}
      </div>

      {/* Bound account + progress bar, full remaining width */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{
          fontSize: 13, fontWeight: 600, color: 'var(--fg)', letterSpacing: '-0.01em',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          <span style={{ color: 'var(--fg-3)', fontWeight: 500 }}>→ </span>
          {customer ? customer.name : '—'}
        </span>
        <div style={{ height: 4, background: '#eef0f3', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{
            height: '100%',
            width: `${(pct * 100).toFixed(1)}%`,
            background: color,
            transition: 'width 240ms ease, background 360ms ease',
          }} />
        </div>
      </div>

      {/* Credit progress, right-aligned — $ credited so far out of 100 */}
      <div style={{ flexShrink: 0, textAlign: 'right' }}>
        <div className="mono" style={{
          fontSize: 15, fontWeight: 600, letterSpacing: '-0.02em',
          color, lineHeight: 1.2,
          transition: 'color 360ms ease',
        }}>
          ${credited}<span style={{ fontSize: 11, fontWeight: 500, color: 'var(--fg-3)' }}>/100</span>
        </div>
      </div>
    </div>
  );
}

function Legend({ color, label }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
      <span className="dot" style={{ background: color }} />
      <span className="mono" style={{ color: 'var(--fg-2)' }}>{label}</span>
    </div>
  );
}

/* ================ Customer accounts — 10 first-class rows ================ */
function CustomersGrid({ state }) {
  const customers = state.customers;
  const totalNow = customers.reduce((s, c) => s + c.balance, 0);
  const totalGoal = customers.length * TARGET_BAL;

  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <span className="label-md" style={{ color: 'var(--fg)' }}>Accounts</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)', whiteSpace: 'nowrap' }}>
            {fmtMoney(totalNow)}<span style={{ color: 'var(--fg-3)' }}>/{fmtMoney(totalGoal)}</span>
          </span>
        </div>
      </div>
      <div style={{
        flex: 1, padding: '10px 14px', minHeight: 0, overflow: 'hidden',
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr)',
        gridTemplateRows: 'repeat(10, minmax(0, 1fr))',
        gap: 6,
      }}>
        {customers.map(c => <CustomerRow key={c.id} c={c} />)}
      </div>
    </div>
  );
}

function CustomerRow({ c }) {
  const pct = Math.min(1, Math.max(0, (c.balance - START_BAL) / (TARGET_BAL - START_BAL)));
  const done = c.balance >= TARGET_BAL;
  const recent = c.lastTxAt && (performance.now() - c.lastTxAt < 500);
  const initial = (c.name || '?').charAt(0);

  const cardBg = done ? 'var(--green-soft)' : (recent ? 'var(--accent-soft)' : 'var(--surface)');
  const cardBd = done ? '#c8e6d4' : (recent ? 'var(--accent)' : 'var(--line)');
  const accent = done ? 'var(--green)' : 'var(--accent)';
  const balColor = done ? 'var(--green)' : 'var(--fg)';

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '6px 12px',
      background: cardBg,
      border: `1px solid ${cardBd}`,
      borderRadius: 8,
      minWidth: 0, minHeight: 0,
      boxShadow: recent && !done ? '0 0 0 3px var(--accent-soft)' : 'none',
      transition: 'background 360ms ease, border-color 240ms ease, box-shadow 240ms ease',
    }}>
      {/* Avatar */}
      <div style={{
        width: 26, height: 26, borderRadius: '50%',
        background: accent,
        color: '#fff', fontWeight: 600, fontSize: 12,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        letterSpacing: '-0.01em',
        transition: 'background 360ms ease',
        flexShrink: 0,
      }}>
        {initial}
      </div>

      {/* Name + progress bar, full remaining width */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span style={{
          fontSize: 13, fontWeight: 600, color: 'var(--fg)', letterSpacing: '-0.01em',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {c.name}
        </span>
        <div style={{ height: 4, background: '#eef0f3', borderRadius: 2, overflow: 'hidden' }}>
          <div style={{
            height: '100%',
            width: `${(pct*100).toFixed(1)}%`,
            background: accent,
            transition: 'width 240ms ease, background 360ms ease',
          }} />
        </div>
      </div>

      {/* Balance, right-aligned */}
      <div style={{ flexShrink: 0, textAlign: 'right' }}>
        <div className="mono" style={{
          fontSize: 15, fontWeight: 600, letterSpacing: '-0.02em',
          color: balColor, lineHeight: 1.2,
          transition: 'color 360ms ease',
        }}>
          {fmtMoney(c.balance)}
        </div>
        <div className="mono" style={{
          fontSize: 9, color: done ? 'var(--green)' : 'var(--fg-3)', fontWeight: 600,
          letterSpacing: '0.04em', textTransform: 'uppercase',
        }}>
          {done ? '✓ target' : `${(pct*100).toFixed(0)}%`}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { AgentsGrid, CustomersGrid });
