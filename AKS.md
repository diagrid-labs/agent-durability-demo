# Deploying to AKS

Azure-specific provisioning for the [Bank Heist Demo](./README.md). Once the cluster exists and `kubectl` is pointing at it, follow the **[Deploying to Kubernetes](./README.md#deploying-to-kubernetes)** section of the README for the generic install steps (image build, Dapr install, helm releases).

## Cluster shape

Three nodepools — system / control / agents:

| Pool | Nodes | Purpose | Chaos target? |
|---|---|---|---|
| `system` | 1 | kube-system, dapr-system, ingress-nginx | Never |
| `control` | 1 | Postgres, MCP server | Never |
| `agents` | N (one per AZ your sub supports) | Agent worker pods | Yes |

The agents nodepool spreads across Azure AZs via `--zones`. AKS auto-labels each node with `topology.kubernetes.io/zone=<region>-N` so the agent Deployment's `topologySpreadConstraints` distribute one pod per AZ. That's the real failure domain behind the "kill AZ" chaos button.

## Prerequisites

- Azure subscription with [AZs enabled in your chosen region](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-region-support)
- `az` CLI logged in (`az login`)
- A VM size your subscription is allowed to use. Check with:

  ```bash
  az vm list-skus --location <region> --resource-type virtualMachines \
    --query "[?capabilities[?name=='vCPUs' && value=='2'] && length(restrictions)==\`0\`].name | sort(@)" \
    -o tsv | head -20
  ```

  `Standard_D2as_v7` works on most subscriptions. `Standard_B2s` and `D2s_v5` are commonly restricted.

## Provisioning

```bash
RG=martez                                  # your resource group
LOCATION=northeurope                       # any region with multiple AZs in your sub
CLUSTER=demo-production-catalyst-agents
SYS_VM=Standard_D2as_v7
AGENT_VM=Standard_D2as_v7

az group create --name $RG --location $LOCATION

# 1. Cluster + system nodepool. Taint applied separately because older CLIs
#    reject --node-taints on `aks create`.
az aks create \
  --resource-group $RG --name $CLUSTER --location $LOCATION \
  --node-count 1 --node-vm-size $SYS_VM \
  --nodepool-name system \
  --enable-managed-identity --generate-ssh-keys

az aks nodepool update --resource-group $RG --cluster-name $CLUSTER \
  --name system --node-taints CriticalAddonsOnly=true:NoSchedule

# 2. Control nodepool — Postgres, MCP. Pinned via the bank-heist.role label.
az aks nodepool add --resource-group $RG --cluster-name $CLUSTER \
  --name control --node-count 1 --node-vm-size $SYS_VM \
  --labels bank-heist.role=platform

# 3. Agents nodepool — one node per AZ. Match `--zones` to what your
#    subscription supports for this VM size in this region:
#       az vm list-skus --location $LOCATION --resource-type virtualMachines \
#         --query "[?name=='$AGENT_VM'].locationInfo[0].zones" -o tsv
az aks nodepool add --resource-group $RG --cluster-name $CLUSTER \
  --name agents --node-count 2 --node-vm-size $AGENT_VM \
  --zones 2 3 --labels bank-heist.role=agents

az aks get-credentials --resource-group $RG --name $CLUSTER --overwrite-existing

# Verify
kubectl get nodes --show-labels | grep -E 'bank-heist|topology.kubernetes.io/zone'
```

You should see one system node (tainted), one control node, and N agents nodes — one in each requested AZ.

If `--zones 1 2 3` fails with `AvailabilityZoneNotSupported`, your subscription is restricted to a subset. The error message lists what's available. Adjust `--zones` and `--node-count` to match. The agent Deployment scales fine with 1–3 AZs.

## After provisioning

Follow [**Deploying to Kubernetes**](./README.md#deploying-to-kubernetes) in the README from step 2 (image build/push).

A couple of AKS-flavored notes that apply when working through the generic steps:

- **Image registry** — Docker Hub is fine. If you prefer ACR: `az acr login --name <yourAcr>` first, then `export REGISTRY=<yourAcr>.azurecr.io`.
- **LoadBalancer external IP** — `kubectl -n ingress-nginx get svc ingress-nginx-controller` shows the public IP allocated by Azure. If it stays in `<pending>` for > 2 min, check `kubectl -n kube-system get pods` for cloud-controller-manager health.
- **Backend pool gotcha** — If the LB IP exists but external traffic times out (in-cluster probes succeed but external doesn't), the backend pool may not be populated. `az network lb address-pool show -g MC_<rg>_<cluster>_<region> --lb-name kubernetes --name kubernetes -o json | jq '.loadBalancerBackendAddresses | length'` should be > 0. Most common cause: `node.kubernetes.io/exclude-from-external-load-balancers` on a node — remove with `kubectl label node <name> node.kubernetes.io/exclude-from-external-load-balancers-`.

## Tear-down

```bash
az aks delete --resource-group $RG --name $CLUSTER --yes --no-wait
# Optional: delete the resource group entirely
az group delete --name $RG --yes --no-wait
```
