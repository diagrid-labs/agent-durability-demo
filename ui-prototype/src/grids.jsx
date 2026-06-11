/* eslint-disable */
const { useMemo: uM2, useEffect: uE2, useState: uS2 } = React;

/* ================ Agents grid — 10x10 (or sized to count) ================ */
function AgentsGrid({ state }) {
  const agents = state.agents;
  const cols = agents.length <= 100 ? 10 : Math.ceil(Math.sqrt(agents.length));
  const recentMap = uM2(() => {
    const m = new Map();
    for (const a of state.activity) {
      if (!m.has(a.agentId)) m.set(a.agentId, a.ts);
    }
    return m;
  }, [state.activity]);

  // No memo — `state.agents` is mutated in place (the WS slot-state handler
  // flips agent.status without replacing the array). Memoizing on the array
  // reference would freeze the counts. The loop is 100 elements, trivial.
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
          <span className="label-md" style={{ color: 'var(--fg)' }}>Agent Workflows</span>
          <span className="pill gray">{agents.length} workflows</span>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          {counts.idle > 0 && <Legend color="var(--fg-3)" label={`idle ${counts.idle}`} />}
          <Legend color="var(--green)" label={`alive ${counts.alive}`} />
          <Legend color="var(--amber)" label={`restarting ${counts.restarting}`} />
          <Legend color="var(--red)" label={`dead ${counts.dead}`} />
        </div>
      </div>
      <div style={{
        flex: 1, padding: 14, minHeight: 0, overflow: 'auto',
        display: 'grid',
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
        gridAutoRows: 'minmax(48px, 1fr)',
        gap: 7,
        alignContent: 'stretch',
      }}>
        {agents.map(a => {
          const recent = recentMap.get(a.id);
          const tx = recent && (performance.now() - recent < 350);
          let bg = 'var(--green-soft)', bd = '#c8e6d4', dot = 'var(--green)', fg = 'var(--green)';
          if (a.status === 'idle')      { bg = '#f4f5f7';            bd = '#e3e6eb'; dot = 'var(--fg-3)'; fg = 'var(--fg-3)'; }
          else if (a.status === 'restarting') { bg = 'var(--amber-soft)'; bd = '#f3deb3'; dot = 'var(--amber)'; fg = 'var(--amber)'; }
          else if (a.status === 'dead')  { bg = 'var(--red-soft)';   bd = '#f1c5c8'; dot = 'var(--red)';   fg = 'var(--red)'; }
          return (
            <div key={a.id} className={'agent-tile' + (tx ? ' tx' : '')}
              title={`${a.label} · ${a.status} · ${a.txCount} tx`}
              style={{
                background: bg, border: `1px solid ${bd}`, borderRadius: 6,
                padding: '7px 9px',
                display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
                minWidth: 0, minHeight: 0,
                position: 'relative',
              }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <span className="dot" style={{ background: dot, width: 7, height: 7 }} />
                <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)', fontWeight: 500, letterSpacing: '-0.01em' }}>
                  {String(a.id+1).padStart(3,'0')}
                </span>
              </div>
              <span className="mono" style={{ fontSize: 14, color: fg, fontWeight: 600, textAlign: 'right', letterSpacing: '-0.02em', lineHeight: 1 }}>
                {a.txCount}
              </span>
            </div>
          );
        })}
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
  const completed = customers.filter(c => c.balance >= TARGET_BAL).length;
  const totalNow = customers.reduce((s, c) => s + c.balance, 0);
  const totalGoal = customers.length * TARGET_BAL;

  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <span className="label-md" style={{ color: 'var(--fg)' }}>Customer accounts</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-2)', whiteSpace: 'nowrap' }}>
            {fmtMoney(totalNow)}<span style={{ color: 'var(--fg-3)' }}>/{fmtMoney(totalGoal)}</span>
          </span>
          <span className="pill teal">{completed}/{customers.length}</span>
        </div>
      </div>
      <div style={{
        flex: 1, padding: '10px 14px', minHeight: 0, overflow: 'hidden',
        display: 'grid',
        gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
        gridTemplateRows: 'repeat(5, minmax(0, 1fr))',
        gap: 8,
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
  const acctNum = `BNK-${String(c.id+1).padStart(4,'0')}-7741`;
  const initial = (c.name || '?').charAt(0);

  const cardBg = done ? 'var(--green-soft)' : (recent ? 'var(--accent-soft)' : 'var(--surface)');
  const cardBd = done ? '#c8e6d4' : (recent ? 'var(--accent)' : 'var(--line)');
  const accent = done ? 'var(--green)' : 'var(--accent)';
  const balColor = done ? 'var(--green)' : 'var(--fg)';

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '32px minmax(0, 1fr)',
      gridTemplateRows: 'auto auto auto',
      columnGap: 12,
      rowGap: 8,
      padding: '12px 14px',
      background: cardBg,
      border: `1px solid ${cardBd}`,
      borderRadius: 8,
      minHeight: 0,
      boxShadow: recent && !done ? '0 0 0 3px var(--accent-soft)' : 'none',
      transition: 'background 360ms ease, border-color 240ms ease, box-shadow 240ms ease',
    }}>
      {/* Row 1: avatar + name */}
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: accent,
        color: '#fff', fontWeight: 600, fontSize: 13,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        letterSpacing: '-0.01em',
        transition: 'background 360ms ease',
        flexShrink: 0,
        gridRow: '1 / 2', gridColumn: '1 / 2',
      }}>
        {initial}
      </div>
      <div style={{ gridRow: '1 / 2', gridColumn: '2 / 3', display: 'flex', alignItems: 'center', minWidth: 0 }}>
        <span style={{
          fontSize: 14, fontWeight: 600, color: 'var(--fg)', letterSpacing: '-0.01em',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {c.name}
        </span>
      </div>

      {/* Row 2: balance under name */}
      <div style={{
        gridRow: '2 / 3', gridColumn: '1 / 3',
        display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8,
      }}>
        <span className="mono" style={{
          fontSize: 22, fontWeight: 600, letterSpacing: '-0.025em',
          color: balColor,
          lineHeight: 1.05,
          transition: 'color 360ms ease',
        }}>
          {fmtMoney(c.balance)}
        </span>
        <span className="mono" style={{
          fontSize: 10, color: done ? 'var(--green)' : 'var(--fg-3)', fontWeight: 600,
          letterSpacing: '0.04em', textTransform: 'uppercase',
        }}>
          {done ? '✓ target' : `${(pct*100).toFixed(0)}%`}
        </span>
      </div>

      {/* Row 3: full-width progress bar */}
      <div style={{
        gridRow: '3 / 4', gridColumn: '1 / 3',
        height: 6, background: '#eef0f3', borderRadius: 3, overflow: 'hidden',
      }}>
        <div style={{
          height: '100%',
          width: `${(pct*100).toFixed(1)}%`,
          background: accent,
          transition: 'width 240ms ease, background 360ms ease',
        }} />
      </div>
    </div>
  );
}

Object.assign(window, { AgentsGrid, CustomersGrid });
