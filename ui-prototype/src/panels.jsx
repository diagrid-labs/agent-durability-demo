/* eslint-disable */
const { useState: uS3, useEffect: uE3, useRef: uR3 } = React;

/* ================ MCP Server card ================ */
function McpServer({ state }) {
  const ref = uR3(null);
  const lines = state.mcp.lines;
  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <McpIcon />
          <span className="label-md" style={{ color: 'var(--fg)' }}>MCP Server Calls</span>
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
        fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.55,
        background: '#fafbfc',
      }}>
        {lines.length === 0 && (
          <div style={{ color: 'var(--fg-3)', padding: 8 }}>Waiting for first query…</div>
        )}
        {lines.map(l => {
          const isChaos = l.kind === 'chaos';
          const color = l.kind === 'req' ? 'var(--accent)' :
                        l.kind === 'res' ? 'var(--green)' :
                        isChaos ? 'var(--red)' :
                        'var(--fg-3)';
          const prefix = l.kind === 'req' ? '→' : l.kind === 'res' ? '←' : isChaos ? '⚠' : '·';
          return (
            <div key={l.id} className="mcp-line" style={{
              display: 'grid', gridTemplateColumns: '52px 14px 1fr', gap: 8,
              padding: '2px 6px',
              margin: '0 -6px',
              alignItems: 'baseline',
              background: isChaos ? 'var(--red-soft)' : undefined,
              borderRadius: isChaos ? 4 : undefined,
            }}>
              <span style={{ color: 'var(--fg-3)', fontSize: 10 }}>{fmtClock(l.ts)}</span>
              <span style={{ color, fontWeight: 600 }}>{prefix}</span>
              <span style={{
                color: isChaos ? 'var(--red)' : l.kind === 'sys' ? 'var(--fg-2)' : 'var(--fg-1)',
                wordBreak: 'break-word',
                fontWeight: isChaos ? 500 : 'inherit',
              }}>
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
  const inWave = !!state.chaos.activeWave || !!state.chaos.azDown ||
                 performance.now() < state.chaos.latencyUntil;
  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label-md" style={{ color: 'var(--fg)' }}>Chaos experiment</span>
          {inWave && <span className="pill amber">active</span>}
        </div>
      </div>
      <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'auto', minHeight: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0 }}>
          <button className="btn sm" onClick={() => feed.control.killRandom()}
                  title="Pod dies mid-work (OOM, eviction, crash). Catalyst re-dispatches to a healthy worker."
                  disabled={!state.chaos.victim}
                  style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {state.chaos.victim
              ? `Pod failure (${state.chaos.victim.workflow_count} agents)`
              : 'Pod failure'}
          </button>
          <button className="btn sm" onClick={() => feed.control.killAZ()}
                  title="All pods in one Availability Zone die. Surviving AZs absorb the load."
                  disabled={!state.chaos.zoneVictim}
                  style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {state.chaos.zoneVictim
              ? `AZ failure: ${state.chaos.zoneVictim.zone} (${state.chaos.zoneVictim.workflow_count} agents)`
              : 'AZ failure'}
          </button>
          <button className="btn sm" onClick={() => feed.control.latencyJitter(10000)}
                  title="Slow downstream (connection pool timeout, slow query). Workflows complete, just slower."
                  style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            MCP Latency: 10s
          </button>
          <button className="btn sm" onClick={() => feed.control.dropTx()}
                  title="Next MCP call returns 5xx. Workflow retries; idempotency prevents double-credit."
                  style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            MCP tool call failure
          </button>
        </div>
      </div>
    </div>
  );
}

/* ================ Pod fleet + operating environment ================ */
function PodFleet({ state }) {
  const podsRaw = (state.chaos && state.chaos.pods) || [];
  const victim = state.chaos && state.chaos.victim;
  // Most-loaded pod (next kill target) first.
  const pods = podsRaw.slice().sort((a, b) =>
    (b.workflow_count || 0) - (a.workflow_count || 0) || a.pod.localeCompare(b.pod)
  );
  const max = pods.reduce((m, p) => Math.max(m, p.workflow_count || 0), 0) || 1;
  const nodepools = (state.chaos && state.chaos.nodepools) || [];
  const nodes = (state.chaos && state.chaos.nodes) || [];
  const impactedZones = (state.chaos && state.chaos.impactedZones) || {};
  const now = Date.now();
  const isImpacted = (zone) => zone && impactedZones[zone] && impactedZones[zone] > now;
  return (
    <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 }}>
      <div className="card-h">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="label-md" style={{ color: 'var(--fg)' }}>Kubernetes cluster</span>
        </div>
      </div>
      <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 12, overflow: 'auto', minHeight: 0 }}>
        {/* Nodepools — control / agents / system overview */}
        {nodepools.length > 0 && (
          <PodFleetSection title="Nodepools" count={nodepools.length}>
            {nodepools.map(np => {
              const npImpacted = (np.zones || []).some(isImpacted);
              return (
                <div key={np.name} className={npImpacted ? 'chaos-pulse' : ''} style={{
                  padding: '6px 10px', borderRadius: 6,
                  background: npImpacted ? 'var(--red-soft)' : 'var(--bg-2, #fafbfc)',
                  border: '1px solid ' + (npImpacted ? 'var(--red)' : 'var(--line)'),
                  transition: 'background 240ms, border-color 240ms',
                }}>
                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <span className="mono" style={{ fontSize: 11, fontWeight: 600, color: npImpacted ? 'var(--red)' : 'var(--fg-1)' }}>
                      {np.name}
                    </span>
                  </div>
                  <div className="mono" style={{ marginTop: 2, fontSize: 10, color: npImpacted ? 'var(--red)' : 'var(--fg-2)' }}>
                    {npImpacted ? 'pods evicted' : `${np.ready}/${np.node_count} ready`}
                  </div>
                </div>
              );
            })}
          </PodFleetSection>
        )}
        {/* Nodes */}
        {nodes.length > 0 && (
          <PodFleetSection title="Nodes" count={nodes.length}>
            {nodes.map(n => {
              const nImpacted = isImpacted(n.zone);
              return (
                <div key={n.name} className={nImpacted ? 'chaos-pulse' : ''} style={{
                  padding: '4px 8px', borderRadius: 4,
                  background: nImpacted ? 'var(--red-soft)' : 'var(--bg-2, #fafbfc)',
                  border: '1px solid ' + (nImpacted ? 'var(--red)' : 'var(--line)'),
                  transition: 'background 240ms, border-color 240ms',
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 6 }}>
                    <span className="mono" style={{ fontSize: 10, color: nImpacted ? 'var(--red)' : 'var(--fg-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {n.name}
                    </span>
                    <span className="dot" style={{
                      flexShrink: 0,
                      background: nImpacted ? 'var(--red)' : (n.ready === 'True' ? 'var(--green)' : 'var(--red)'),
                    }} />
                  </div>
                </div>
              );
            })}
          </PodFleetSection>
        )}
        {/* Pods — sorted desc by workflow count */}
        <PodFleetSection
          title="Agent pods"
          count={pods.length}
          hint={pods.length === 0 ? 'no live pods detected' : null}
        >
          {pods.map(p => {
            const isVictim = victim && p.pod === victim.pod;
            const pct = ((p.workflow_count || 0) / max) * 100;
            return (
              <div key={p.pod} style={{
                padding: '6px 10px', borderRadius: 6,
                background: isVictim ? 'var(--accent-soft)' : 'var(--bg-2, #fafbfc)',
                border: '1px solid ' + (isVictim ? 'var(--accent)' : 'var(--line)'),
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
                  <span className="mono" style={{ fontSize: 11, fontWeight: 500, color: 'var(--fg-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {p.pod}
                  </span>
                  <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: isVictim ? 'var(--accent)' : 'var(--fg)' }}>
                    {p.workflow_count || 0}
                  </span>
                </div>
                <div style={{ marginTop: 4, height: 4, background: 'var(--line)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    width: `${pct}%`, height: '100%',
                    background: isVictim ? 'var(--accent)' : 'var(--green)',
                    transition: 'width 600ms ease',
                  }} />
                </div>
                {p.node && (
                  <div style={{
                    marginTop: 3, fontSize: 10, color: 'var(--fg-3)',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {p.node}
                  </div>
                )}
              </div>
            );
          })}
        </PodFleetSection>
      </div>
    </div>
  );
}

function PodFleetSection({ title, count, hint, children }) {
  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6,
        paddingBottom: 4, borderBottom: '1px solid var(--line)',
      }}>
        <span className="label" style={{ fontWeight: 600, color: 'var(--fg-2)' }}>
          {title}
        </span>
        {count != null && (
          <span className="mono" style={{ fontSize: 10, color: 'var(--fg-3)' }}>{count}</span>
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {hint && <div style={{ color: 'var(--fg-3)', fontSize: 11, padding: 4 }}>{hint}</div>}
        {children}
      </div>
    </div>
  );
}

/* ================ Combined chaos + MCP log sidebar ================ */
function Sidebar({ feed, state }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0, height: '100%',
    }}>
      <div style={{ flex: '0 0 auto' }}>
        <ChaosPanel feed={feed} state={state} />
      </div>
      <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <McpServer state={state} />
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { McpServer, ChaosPanel, PodFleet, Sidebar });
