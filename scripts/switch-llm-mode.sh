#!/usr/bin/env bash
# Switch the agent between stub LLM and real OpenAI between demo segments.
#
# Usage:
#   ./scripts/switch-llm-mode.sh stub
#   ./scripts/switch-llm-mode.sh real
#
# For `real`, the script ensures a Secret named `openai-api-key` exists in the
# bank-heist namespace with key OPENAI_API_KEY. If missing, it reads from the
# OPENAI_API_KEY env var (export it before running).

set -euo pipefail

MODE="${1:-}"
NAMESPACE="${NAMESPACE:-bank-heist}"
RELEASE="${RELEASE:-agent}"
CHART="${CHART:-./deploy/agent}"
SECRET="${OPENAI_SECRET_NAME:-openai-api-key}"

if [[ "$MODE" != "stub" && "$MODE" != "real" ]]; then
  echo "usage: $0 <stub|real>" >&2
  exit 1
fi

if [[ "$MODE" == "real" ]]; then
  if ! kubectl -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1; then
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "Secret '$SECRET' not found in '$NAMESPACE' and OPENAI_API_KEY env not set." >&2
      echo "Export OPENAI_API_KEY=sk-... and re-run." >&2
      exit 1
    fi
    kubectl -n "$NAMESPACE" create secret generic "$SECRET" \
      --from-literal=OPENAI_API_KEY="$OPENAI_API_KEY"
  fi
  helm upgrade --reuse-values "$RELEASE" "$CHART" -n "$NAMESPACE" \
    --set agent.llm.mode=real \
    --set agent.llm.apiKeySecret="$SECRET"
else
  helm upgrade --reuse-values "$RELEASE" "$CHART" -n "$NAMESPACE" \
    --set agent.llm.mode=stub \
    --set agent.llm.apiKeySecret=""
fi

kubectl -n "$NAMESPACE" rollout restart deploy/"$RELEASE"
kubectl -n "$NAMESPACE" rollout status deploy/"$RELEASE" --timeout=180s

AGENT_POD=$(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/name=agent -o name | head -1 | cut -d/ -f2)
echo "---"
kubectl -n "$NAMESPACE" exec "$AGENT_POD" -- env | grep -E "STUB_LLM|AGENT_MODEL|OPENAI_API_KEY" \
  | sed 's/OPENAI_API_KEY=.*/OPENAI_API_KEY=<redacted>/'
echo "Switched to: $MODE"
