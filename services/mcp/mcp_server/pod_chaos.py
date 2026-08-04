"""Real pod-kill chaos using the Kubernetes API.

Lists pods matching the configured label selector and deletes a random
subset; the Deployment reschedules them and in-flight workflows resume on
whichever pod picks them up — that's the durability story.

No-ops when not running in-cluster (e.g. local docker-compose): endpoints
respond with `available: false`.
"""

import logging
import os
import random
from pathlib import Path
from typing import Any

log = logging.getLogger("pod_chaos")


class PodChaosController:
    def __init__(self) -> None:
        self._enabled = False
        self._namespace: str | None = None
        self._v1 = None
        self._label_selector = os.environ.get(
            "POD_CHAOS_LABEL_SELECTOR", "app.kubernetes.io/name=agent"
        )
        # Catalyst's remote Dapr sidecar for this app-id, in the project's own
        # `prj-<id>` namespace. Empty means the capability is off — only
        # bank-creditor's mcp release sets these (see deploy/mcp).
        self._sidecar_namespace = os.environ.get("CATALYST_SIDECAR_NAMESPACE", "")
        self._sidecar_app_id = os.environ.get("CATALYST_SIDECAR_APP_ID", "")
        self._init_client()

    def _init_client(self) -> None:
        try:
            from kubernetes import client, config
        except ImportError:
            log.info("kubernetes client not installed — pod-kill disabled")
            return
        try:
            config.load_incluster_config()
        except Exception as e:  # noqa: BLE001
            log.info("not running in-cluster (%s) — pod-kill disabled", e)
            return
        ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        try:
            self._namespace = ns_path.read_text().strip()
        except OSError:
            self._namespace = os.environ.get("POD_NAMESPACE", "default")
        self._v1 = client.CoreV1Api()
        self._enabled = True
        log.info(
            "pod-chaos enabled · ns=%s selector=%s",
            self._namespace,
            self._label_selector,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self._enabled,
            "namespace": self._namespace,
            "label_selector": self._label_selector,
            "sidecar_restart_configured": bool(
                self._sidecar_namespace and self._sidecar_app_id
            ),
        }

    def _restart_catalyst_sidecar(self) -> dict[str, Any]:
        """Best-effort: delete Catalyst's sidecar pod for this app-id, called
        after every pod-kill/AZ-kill to test whether it unsticks activities
        orphaned by the kill (see CLAUDE.md's pod-kill-mid-activity gotcha).
        No-ops when not configured.

        Always unconditional, even for AZ-kill: the sidecar's single replica
        runs on the `control` nodepool (zone `"0"`), never the `agents`
        nodepool's real AZs, so it has no zone to match against."""
        if not self._enabled or not self._sidecar_namespace or not self._sidecar_app_id:
            return {"attempted": False}
        selector = f"app.kubernetes.io/name=sidecar,dapr-app-id={self._sidecar_app_id}"
        try:
            pods = self._v1.list_namespaced_pod(
                self._sidecar_namespace, label_selector=selector
            )
        except Exception as e:  # noqa: BLE001
            return {"attempted": True, "restarted": [], "error": str(e)}
        candidates = [p for p in pods.items if p.metadata.deletion_timestamp is None]
        restarted: list[str] = []
        errors: list[str] = []
        for p in candidates:
            try:
                self._v1.delete_namespaced_pod(p.metadata.name, self._sidecar_namespace)
                restarted.append(p.metadata.name)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.metadata.name}: {e}")
        result: dict[str, Any] = {"attempted": True, "restarted": restarted}
        if errors:
            result["errors"] = errors
        return result

    def list_live_pods(self) -> list[dict[str, Any]]:
        """Currently-running agent pods with node + zone, for the UI's
        killable-pod list. Empty when not in-cluster."""
        if not self._enabled:
            return []
        try:
            pods = self._v1.list_namespaced_pod(
                self._namespace, label_selector=self._label_selector
            )
        except Exception:  # noqa: BLE001
            return []
        node_zones = {n["name"]: n["zone"] for n in self.list_nodes()}
        out: list[dict[str, Any]] = []
        for p in pods.items:
            if p.metadata.deletion_timestamp is not None:
                continue
            if p.status.phase not in ("Running", "Pending"):
                continue
            node_name = p.spec.node_name or ""
            out.append({
                "pod": p.metadata.name,
                "node": node_name,
                "zone": node_zones.get(node_name),
                "phase": p.status.phase,
                "ip": p.status.pod_ip,
            })
        out.sort(key=lambda x: x["pod"])
        return out

    def kill_zone(self, zone: str | None = None) -> dict[str, Any]:
        """Delete every agent pod whose node lives in `zone`. If `zone` is
        omitted, picks the zone hosting the most agent pods (most visceral
        kill). Returns the chosen zone + list of deleted pods."""
        if not self._enabled:
            return {"available": False, "killed": [], "reason": "not in-cluster"}
        live = self.list_live_pods()
        by_zone: dict[str, list[str]] = {}
        for entry in live:
            z = entry.get("zone")
            if not z:
                continue
            by_zone.setdefault(z, []).append(entry["pod"])
        if not by_zone:
            return {"available": True, "killed": [], "zone": None,
                    "reason": "no zone-labelled agent pods"}
        if zone is None or zone not in by_zone:
            zone = max(by_zone, key=lambda z: len(by_zone[z]))
        targets = by_zone.get(zone, [])
        killed: list[str] = []
        errors: list[str] = []
        for name in targets:
            try:
                self._v1.delete_namespaced_pod(name, self._namespace)
                killed.append(name)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        result: dict[str, Any] = {
            "available": True,
            "zone": zone,
            "killed": killed,
            "candidates": len(targets),
        }
        if errors:
            result["errors"] = errors
        if killed:
            result["catalyst_sidecar"] = self._restart_catalyst_sidecar()
        return result

    def list_nodes(self) -> list[dict[str, Any]]:
        """Cluster-wide node summary: name, nodepool, zone, role, ready."""
        if not self._enabled:
            return []
        try:
            nodes = self._v1.list_node()
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for n in nodes.items:
            labels = n.metadata.labels or {}
            ready = "Unknown"
            for cond in (n.status.conditions or []):
                if cond.type == "Ready":
                    ready = cond.status
                    break
            out.append({
                "name": n.metadata.name,
                "nodepool": labels.get("agentpool"),
                "zone": labels.get("topology.kubernetes.io/zone"),
                "role": labels.get("bank-creditor.role"),
                "ready": ready,
            })
        out.sort(key=lambda x: (x.get("nodepool") or "zzz", x["name"]))
        return out

    def list_nodepools(self, nodes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Group nodes by nodepool. Cheap derivation from list_nodes()."""
        if nodes is None:
            nodes = self.list_nodes()
        pools: dict[str, dict[str, Any]] = {}
        for n in nodes:
            name = n.get("nodepool") or "unknown"
            p = pools.setdefault(name, {
                "name": name, "role": n.get("role"), "node_count": 0,
                "zones": set(), "ready": 0,
            })
            p["node_count"] += 1
            if n.get("ready") == "True":
                p["ready"] += 1
            if n.get("zone"):
                p["zones"].add(n["zone"])
        out = []
        for p in pools.values():
            p["zones"] = sorted(p["zones"])
            out.append(p)
        # Stable order: control first, agents next, then everything else.
        order = {"control": 0, "agents": 1, "system": 2}
        out.sort(key=lambda x: (order.get(x["name"], 99), x["name"]))
        return out

    def kill_named(self, pod_name: str) -> dict[str, Any]:
        """Delete one specific pod by name. Used when the UI picks a target
        upfront so the displayed label matches what dies."""
        if not self._enabled:
            return {"available": False, "killed": [], "reason": "not in-cluster"}
        try:
            self._v1.delete_namespaced_pod(pod_name, self._namespace)
        except Exception as e:  # noqa: BLE001
            return {"available": True, "killed": [], "error": str(e)}
        return {
            "available": True,
            "killed": [pod_name],
            "catalyst_sidecar": self._restart_catalyst_sidecar(),
        }

    def kill_random(self, count: int) -> dict[str, Any]:
        if not self._enabled:
            return {"available": False, "killed": [], "reason": "not in-cluster"}
        try:
            pods = self._v1.list_namespaced_pod(
                self._namespace, label_selector=self._label_selector
            )
        except Exception as e:  # noqa: BLE001
            return {"available": True, "killed": [], "error": str(e)}
        candidates = [
            p for p in pods.items
            if p.metadata.deletion_timestamp is None
            and p.status.phase in ("Running", "Pending")
        ]
        if not candidates:
            return {"available": True, "killed": [], "candidates": 0}
        # Always leave at least one survivor for recovery demos.
        max_killable = max(1, len(candidates) - 1)
        victims = random.sample(candidates, min(count, max_killable))
        killed: list[str] = []
        errors: list[str] = []
        for pod in victims:
            name = pod.metadata.name
            try:
                self._v1.delete_namespaced_pod(name, self._namespace)
                killed.append(name)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        result: dict[str, Any] = {
            "available": True,
            "killed": killed,
            "candidates": len(candidates),
        }
        if errors:
            result["errors"] = errors
        if killed:
            result["catalyst_sidecar"] = self._restart_catalyst_sidecar()
        return result
