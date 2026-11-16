# Bank Creditor demo run guide

Ten accounts start at $100. One agent per account credits $1 at a time until the account reaches $200. Kill a pod mid-run: the plain LangGraph agent loses accounts, and both agents on Diagrid finish every one. Each scenario below stands on its own.

## Before you start

- Sign in to [LangSmith](https://smith.langchain.com) with the 1Password item **LangSmith** in the **Diagrid Company Wide** vault (`hi@diagrid.io`). Otherwise the UIs' **Open in LangSmith ↗** button lands on the login page.
- Sign in to the [Diagrid Console](https://catalyst.diagrid.io) with your Diagrid account, org **Demo Production Diagrid**.
- Click **↺ Reset** in each UI you'll use.

## Plain LangGraph

UI: http://demo-prod-catalyst-plain.northeurope.cloudapp.azure.com/ · LangSmith project: [bank-creditor-plain](https://smith.langchain.com/o/ee448549-9340-4d81-a47d-1430acf42469/projects/p/394ac961-ebd4-48fb-9d18-c20bdb664e51)

1. Click **▶ Start run**.
2. While balances are climbing, click **Pod failure**. The accounts on that pod stop and stay below $200.
3. Click **Open in LangSmith ↗** and open a trace for a stopped account. It ends at the kill.

## LangGraph on Diagrid

UI: http://demo-prod-catalyst-agents.northeurope.cloudapp.azure.com/ · LangSmith project: [bank-creditor](https://smith.langchain.com/o/ee448549-9340-4d81-a47d-1430acf42469/projects/p/f6ab8b2a-0814-49e4-8a9a-ec1bcaeb070a) · Diagrid project: `resiliency-demo-langgraph`

1. Click **▶ Start run**.
2. While balances are climbing, click **Pod failure**. The accounts on that pod pause for a few seconds, then carry on.
3. Optionally, try **AZ failure**, **MCP Latency: 10s** and **MCP tool call failure**. Each slows the run down; none loses a credit.
4. Click **Open in LangSmith ↗** and open a trace. Every `credit_next` step is tagged with the pod that ran it; after the kill, the steps carry on from the surviving pod.
5. In the Diagrid Console, open **Workflows** and pick an account that was on the killed pod. Its history shows the `execute_node_activity` step that ran again after the kill. **Agents** shows the `banker` agent and its `credit_next` tool.
6. Let the run finish, in about 3 minutes: every account at $200, 1,000 transactions, 0 lost.

## Dapr Agents on Diagrid

UI: http://demo-prod-catalyst-dapr-agents.northeurope.cloudapp.azure.com/ · Diagrid project: `resiliency-demo-dapr-agents` · No LangSmith traces

1. Click **▶ Start run**.
2. While balances are climbing, click **Pod failure**. The accounts on that pod pause, then carry on. The other chaos buttons work as in the LangGraph scenario.
3. In the Diagrid Console, open **Agents** and **Workflows** in the `resiliency-demo-dapr-agents` project: one durable workflow per account.
4. Let the run finish: every account at $200, 1,000 transactions, 0 lost.

## If something goes wrong

- **The run finished before you killed a pod.** Click Reset and Start again.
- **A chaos button is greyed out.** No agent is running on a pod yet. Wait a few seconds.
- **The UI counters look wrong.** The MCP server was restarted and lost its run state. Click Reset.
- **A trace's top-level run shows as finished straight away.** That's a known quirk of how runs are started. Look at the steps inside it, not its duration.
