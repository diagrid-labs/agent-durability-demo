# Bank Creditor Demo Flow

A ~10-minute walkthrough. Opens in the Catalyst App Graph, drills through the agent into a workflow, then shows the live UI with chaos. Closes by returning to Catalyst to show workflow recovery, and back to the UI to land the invariant.

## 0. Pre-flight (before stage)

- [ ] Browser tab: Catalyst Console, `resiliency-demo` project, on the **App graph** tab
- [ ] Browser tab: the Bank Creditor UI (LoadBalancer IP, port 80)
- [ ] Click **Reset** in the UI so balances are at $100 and Operations History is empty
- [ ] If prior workflows clutter the Catalyst Workflows list, purge them so a new run is easy to spot

## 1. Catalyst App Graph — "the architecture in one picture"

**Screen:** App Graph for `resiliency-demo`

**Pitch:**
> "Here's the application we're going to look at. Two services: an AI agent on the left, `bank-agent-creditor`, and its Postgres-backed MCP server on the right, `bank-postgres-mcp`. The arrow shows the agent calling the MCP server — but that call goes through Catalyst's managed MCP proxy, not a direct connection. Catalyst governs exactly which tools this agent is allowed to call. That's the whole architecture — an agent using tools exposed by an MCP server, with Catalyst controlling access to both the workflow and the tool surface."

Quick beat, then move to Agents.

## 2. Agents tab — "the banker agent"

**Screen:** Agents tab

**Pitch:**
> "Our agent is called `banker`. Let me show you what's inside."

**Click:** into the `banker` agent's detail page.

**Walk through the right side of the page (the panels):**

- **Tools available**
  > "These are the tools the banker can use — get the next credit task, check a customer's balance, credit an account by $1, mark the task done. Each tool is just an HTTP call against the MCP server."

- **System prompt / instructions**
  > "This is the prompt that tells the agent how to do its job. Get a task, check the balance, credit if not at target, mark done."

- **Recent executions** (right side, list of workflows)
  > "Catalyst tracks every run the banker has ever done. Each entry is one credit task — one durable workflow."

**Click:** the most recent workflow in the Recent Executions list.

## 3. Workflow detail — "every step is durable"

**Screen:** a single workflow execution

**Pitch:**
> "Drilling into this workflow, you see every activity the agent ran to credit that one dollar. Each row is a durable activity — Catalyst persisted the input and output before moving to the next step."

**Click:** into one of the activities (e.g., `execute_node_activity` for the `tools` node — this is where the LangGraph tool call actually runs).

**Pitch:**
> "This activity is the moment the agent actually called `credit_account` on the MCP server. The input was the customer ID and dollar amount; the output was the new balance. If our pod had died between this activity and the next one, Catalyst would have replayed from here on a healthy pod — not from scratch. That's durable execution."

> "Now let's see this happen live at scale."

## 4. The Bank Creditor UI — "what we're about to run"

**Screen:** the Bank Creditor UI

**Walk through the panels before clicking Start.**

**Customers panel (left):**
> "Ten customers. Each one starts at $100. Our goal is to credit each one $1 at a time until they reach $200. That's 100 credits per customer, 1000 credits total."

**Agents panel (center heatmap):**
> "Each tile is a single agent workflow run. As we kick off the demo, you'll see these tiles change state — green for completed, amber for processing, others for restarting or dead. Each tile may represent many runs over the course of the demo since the orchestrator hands out work and the agent picks up a new task as soon as it finishes one."

**Operating Environment panel:**
> "This is the infrastructure we're running on. Azure, AKS, multiple nodes, 4 agent pods spread across an availability set. Real Kubernetes, real Azure — not simulated."

**Operations History (MCP Server panel, right):**
> "And this is the live log of MCP server activity — every transaction hitting the database in real time."

**Chaos panel (far right):**
> "And these are chaos experiments. We'll come back to those."

## 5. Run it — "watch 100 agents in flight"

**Pitch:** (before clicking)
> "Let me reset to make sure we start clean."

**Click:** **↺ Reset**

> "And now let me start the run."

**Click:** **▶ Start run**

**Narrate as it ramps:**
> "You can see Operations History filling up — those are real MCP tool calls hitting Postgres. Each row is a transaction. And watch the customer balances on the left climb in real time — they're going $100, $101, $102 ... as the credits land."

> "We can pause the run if we want, or reset it. But let me show you why the system is interesting."

## 6. Chaos — "the durability story"

**Pitch:** (before clicking anything)
> "These chaos experiments simulate what happens when things go wrong in production — the kind of failures every team eventually hits."

Hover the buttons to show tooltips:
> "Pod failure — a pod dies mid-work, like an OOM kill or eviction. AZ failure — a whole Availability Zone goes offline. MCP server latency — the database starts responding slowly, like a connection pool exhaustion. Force MCP call error — a transient 5xx error."

**Now run them, one at a time, mid-run:**

**Click: Pod failure**
> "I just killed one of our agent pods. Watch the tiles — some go dark, but the workflows on that pod don't disappear. Catalyst notices the pod is gone and re-dispatches the orphaned activities to a healthy worker. Within a few seconds, the heatmap recovers."

**Click: AZ failure**
> "Now I'm taking out an entire Availability Zone. Multiple pods gone at once. Surviving AZs absorb the load — same recovery pattern, larger scale."

**Click: MCP Server Latency · 10s**
> "Now MCP itself slows down. Watch the credit rate drop. Workflows are still completing correctly, just slower — connection pool exhausted, slow query, whatever the real-life cause. Once the latency window passes, it snaps back."

**Click: Force MCP Call Error**
> "And finally — a single MCP call returns a 5xx error. The workflow activity fails. Dapr's durable execution kicks in: same task ID retries, the database's idempotency gate absorbs the duplicate, the customer gets credited exactly once."

**Point at the counters:**
> "Transactions processed keeps climbing. Transactions lost is still zero. We've thrown four different failure modes at this system and we haven't lost a single credit."

## 7. Back to Catalyst — "every recovery is auditable"

**Screen:** Catalyst Workflows tab

**Pitch:**
> "Let's look at what Catalyst saw during all that chaos."

**Browse the workflow list — show the mix:**
> "Some workflows completed cleanly. Some retried after that pod kill. Some are still running. Catalyst tracked every one of them — every retry, every activity, every credit. If a customer comes back tomorrow asking 'where did my $5 go,' I can walk back through every workflow that touched their account and tell them exactly what happened."

> "And they're all working their way toward completion. No matter which chaos we injected — pod kill, AZ outage, latency, errors — the workflows know where they are and keep making progress."

## 8. Close — "the invariant"

**Screen:** back to the Bank Creditor UI

**Pitch:**
> "And here's the proof. Every customer has reached $200. 1000 transactions processed, zero lost. Despite pod failures, an availability zone outage, latency spikes, and explicit error injection — the system is correct."

> "That's durable execution. The runtime keeps track of where every workflow is, recovers from failure automatically, and guarantees you don't lose work or double-spend. Catalyst gives you that on top of Kubernetes."

## Backup / what-ifs

- **If a chaos button doesn't visibly affect the UI:** trigger during peak workflow activity, or wait a few seconds and try again. Latency in particular is most visible mid-run.
- **If a workflow appears stuck in Catalyst:** that's a real talking point — durable execution holds state until it CAN recover. Pivot to "this is why this matters."
- **If a run finishes too fast** (before chaos lands): Reset and re-Start. ~1–3 minutes per run at 100-agent concurrency.
- **If asked about real LLM mode:** `./scripts/switch-llm-mode.sh real` between segments, then re-Start. The `agent` node's `execute_node_activity` now makes a real OpenAI call under the hood — slower but the "real agent reasoning" story is more visible.
