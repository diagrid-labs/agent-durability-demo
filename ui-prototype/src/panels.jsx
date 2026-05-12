/* eslint-disable */
const { useState: uS3, useEffect: uE3, useRef: uR3 } = React;

/* ================ MCP Server card ================ */
function McpServer({ state }) {
  const ref = uR3(null);
  const lines = state.mcp.lines;
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <McpIcon />
          <span className="label-md" style={{ color: 'var(--fg)' }}>MCP Server</span>
          <span className="mono" style={{ fontSize: 11, color: 'var(--fg-3)' }}>postgres.bank · :5432</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={'pill ' + (state.mcp.connected ? 'green' : 'red')}>
            <span className="dot" style={{ background: state.mcp.connected ? 'var(--green)' : 'var(--red)' }} />
            {state.mcp.connected ? 'connected' : 'down'}
          </span>
          <span className="pill gray">{state.counters.mcpQueries} queries</span>
        </div>
      </div>
      <div ref={ref} style={{
        flex: 1, overflow: 'auto', padding: '8px 14px', minHeight: 0,
        fontFamily: 'var(--mono)', fontSize: 11.5, lineHeight: 1.55,
        background: '#fafbfc',
      }}>
        {lines.length === 0 && (
          <div style={{ color: 'var(--fg-3)', padding: 8 }}>Waiting for first query…</div>
        )}
        {lines.map(l => {
          const color = l.kind === 'req' ? 'var(--accent)' :
                        l.kind === 'res' ? 'var(--green)' :
                        'var(--fg-3)';
          const prefix = l.kind === 'req' ? '→' : l.kind === 'res' ? '←' : '·';
          return (
            <div key={l.id} className="mcp-line" style={{
              display: 'grid', gridTemplateColumns: '52px 14px 1fr', gap: 8,
              padding: '2px 0',
              alignItems: 'baseline',
            }}>
              <span style={{ color: 'var(--fg-3)', fontSize: 10 }}>{fmtClock(l.ts)}</span>
              <span style={{ color, fontWeight: 600 }}>{prefix}</span>
              <span style={{ color: l.kind === 'sys' ? 'var(--fg-2)' : 'var(--fg-1)', wordBreak: 'break-word' }}>
                {l.text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function McpIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <ellipse cx="8" cy="3.5" rx="5.5" ry="2" stroke="var(--accent)" strokeWidth="1.2" fill="var(--accent-soft)" />
      <path d="M2.5 3.5 V8 a5.5 2 0 0 0 11 0 V3.5" stroke="var(--accent)" strokeWidth="1.2" fill="var(--accent-soft)" />
      <path d="M2.5 8 V12.5 a5.5 2 0 0 0 11 0 V8" stroke="var(--accent)" strokeWidth="1.2" fill="var(--accent-soft)" />
    </svg>
  );
}

/* ================ Chaos panel ================ */
function ChaosPanel({ feed, state }) {
  const cfg = state.cfg;
  const inWave = !!state.chaos.activeWave || !!state.chaos.azDown ||
                 performance.now() < state.chaos.fleetPausedUntil ||
                 performance.now() < state.chaos.latencyUntil;
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label-md" style={{ color: 'var(--fg)' }}>Chaos experiment</span>
          {inWave && <span className="pill amber">active</span>}
        </div>
        <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>
          tick {cfg.tickMs}ms
        </span>
      </div>
      <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'auto', minHeight: 0 }}>
        <button className="btn primary" style={{ justifyContent: 'center' }} onClick={() => feed.control.fireAll()}>
          ▶ Run full chaos sequence
        </button>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
          <button className="btn sm" onClick={() => feed.control.killRandom(10)}>Kill 10 agents</button>
          <button className="btn sm" onClick={() => feed.control.killRandom(30)}>Kill 30 agents</button>
          <button className="btn sm" onClick={() => feed.control.killAZ()}>AZ failure</button>
          <button className="btn sm" onClick={() => feed.control.pauseFleet(3000)}>Freeze · 3s</button>
          <button className="btn sm" onClick={() => feed.control.latencyJitter(4000)}>Latency · 4s</button>
          <button className="btn sm" onClick={() => feed.control.dropTx()}>Drop 1 tx</button>
        </div>
        <div className="chaos-strip">
          <span className="dot amber" />
          <span>Recovery proof: <b>tx lost = 0</b>, every customer reaches <b>{fmtMoney(TARGET_BAL)}</b>.</span>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { McpServer, ChaosPanel });
