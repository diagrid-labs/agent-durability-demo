# Bank Creditor demo run guide

Ten accounts start at $100. One agent per account credits $1 at a time until the account reaches $200. You run the same LangGraph agent twice, once plain and once on Catalyst, and kill a pod mid-run in each.

## Where everything is

| What | Link | Sign in |
|---|---|---|
| Plain LangGraph UI | http://demo-prod-catalyst-plain.northeurope.cloudapp.azure.com/ | No login |
| LangGraph on Catalyst UI | http://demo-prod-catalyst-agents.northeurope.cloudapp.azure.com/ | No login |
| Dapr Agents on Catalyst UI | http://demo-prod-catalyst-dapr-agents.northeurope.cloudapp.azure.com/ | No login |
| LangSmith | https://smith.langchain.com | 1Password item **LangSmith**, **Diagrid Company Wide** vault (`hi@diagrid.io`) |
| LangSmith project, Catalyst | [bank-creditor](https://smith.langchain.com/o/ee448549-9340-4d81-a47d-1430acf42469/projects/p/f6ab8b2a-0814-49e4-8a9a-ec1bcaeb070a) | Same LangSmith login |
| LangSmith project, plain | [bank-creditor-plain](https://smith.langchain.com/o/ee448549-9340-4d81-a47d-1430acf42469/projects/p/394ac961-ebd4-48fb-9d18-c20bdb664e51) | Same LangSmith login |
| Catalyst console | https://catalyst.diagrid.io | Your Diagrid account, org **Demo Production Catalyst**, project `resiliency-demo-langgraph` |

Each demo UI's **Open in LangSmith ↗** button opens its LangSmith project. Sign in to LangSmith first, or the button lands on the login page.

## 1. Plain LangGraph

1. In the plain UI, click **↺ Reset**, then **▶ Start run**.
2. While balances are climbing, click **Pod failure**.
3. The accounts that were on that pod stop and stay below $200.
4. Click **Open in LangSmith ↗** and open a trace for a stopped account. It ends at the kill.

## 2. LangGraph on Catalyst

1. In the Catalyst UI, click **↺ Reset**, then **▶ Start run**.
2. While balances are climbing, click **Pod failure**. The accounts on that pod pause for a few seconds, then carry on.
3. Optionally, try **AZ failure**, **MCP Latency: 10s** and **MCP tool call failure**. Each one slows the run down; none of them loses a credit.
4. Click **Open in LangSmith ↗** and open a trace. Every `credit_next` step is tagged with the pod that ran it, and after the kill the steps carry on from the surviving pod.
5. Let the run finish: every account at $200, 1,000 transactions, 0 lost. A run takes about 3 minutes.

## 3. Catalyst console

In the `resiliency-demo-langgraph` project:

- **Agents**: the `banker` agent, its `credit_next` tool, and its recent runs.
- **Workflows**: open the workflow of an account that was on the killed pod. Its history shows the `execute_node_activity` step that ran again after the kill.

## If something goes wrong

- **The run finished before you killed a pod.** Click Reset and Start again.
- **A chaos button is greyed out.** No agent is running on a pod yet. Wait a few seconds.
- **The UI counters look wrong.** The MCP server was restarted and lost its run state. Click Reset.
- **LangSmith asks you to sign in.** Use the 1Password item **LangSmith** in the **Diagrid Company Wide** vault, then click the button again.
- **A trace's top-level run shows as finished straight away.** That's a known quirk of how runs are started. Look at the steps inside it, not its duration.
