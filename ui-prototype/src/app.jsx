/* eslint-disable */
const { useState: uS9, useEffect: uE9, useMemo: uM9 } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "agentCount": 100,
  "tickMs": 1200,
  "chaosIntensity": "medium",
  "mcpLatencyMs": 80
}/*EDITMODE-END*/;

function App() {
  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const feed = uM9(() => window.createTelemetry({
    agentCount: tweaks.agentCount,
    customerCount: 10,
    tickMs: tweaks.tickMs,
    mcpLatencyMs: tweaks.mcpLatencyMs,
  }), []);
  uE9(() => { window.__feed = feed; }, [feed]);
  const state = useTelemetry(feed);

  // Sync tweaks to feed cfg
  uE9(() => {
    feed.control.setCfg({
      agentCount: Number(tweaks.agentCount),
      tickMs: Number(tweaks.tickMs),
      mcpLatencyMs: Number(tweaks.mcpLatencyMs),
    });
  }, [tweaks.agentCount, tweaks.tickMs, tweaks.mcpLatencyMs]);

  return (
    <div style={{
      height: '100vh', width: '100vw',
      display: 'grid',
      gridTemplate: `
        "top    top    top    top"    auto
        "count  count  count  count"  auto
        "cust   agents fleet  side"   minmax(0, 1fr)
        / minmax(220px, 0.9fr) minmax(360px, 1.3fr) minmax(220px, 0.9fr) minmax(280px, 1fr)
      `,
      gap: 10,
      paddingBottom: 10,
    }}>
      <div style={{ gridArea: 'top' }}>
        <TopBar state={state} feed={feed} onTogglePause={() => feed.control.setPaused(!feed.control.isPaused())} />
      </div>
      <div style={{ gridArea: 'count' }}>
        <Counters state={state} />
      </div>
      <div style={{ gridArea: 'cust', minWidth: 0, minHeight: 0, height: '100%', display: 'flex', paddingLeft: 14 }}>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <CustomersGrid state={state} />
        </div>
      </div>
      <div style={{ gridArea: 'agents', minWidth: 0, minHeight: 0, height: '100%', display: 'flex' }}>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <AgentsGrid state={state} />
        </div>
      </div>
      <div style={{ gridArea: 'fleet', minWidth: 0, minHeight: 0, height: '100%', display: 'flex' }}>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <PodFleet state={state} />
        </div>
      </div>
      <div style={{ gridArea: 'side', minWidth: 0, minHeight: 0, height: '100%', display: 'flex', paddingRight: 14 }}>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <Sidebar feed={feed} state={state} />
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="Fleet" />
        <TweakSlider label="Agent count" value={tweaks.agentCount} min={10} max={400} step={10}
          onChange={(v) => setTweak('agentCount', v)} />
        <TweakSlider label="Tick interval" value={tweaks.tickMs} min={150} max={2000} step={50} unit="ms"
          onChange={(v) => setTweak('tickMs', v)} />

        <TweakSection label="MCP server" />
        <TweakSlider label="Query latency" value={tweaks.mcpLatencyMs} min={10} max={800} step={10} unit="ms"
          onChange={(v) => setTweak('mcpLatencyMs', v)} />

        <TweakSection label="Chaos" />
        <TweakRadio label="Intensity" value={tweaks.chaosIntensity}
          options={['low','medium','high']}
          onChange={(v) => setTweak('chaosIntensity', v)} />
        <TweakButton label="Run chaos sequence now" onClick={() => feed.control.fireAll()} />
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
