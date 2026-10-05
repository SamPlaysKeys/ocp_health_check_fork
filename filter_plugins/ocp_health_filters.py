#!/usr/bin/env python3
"""
Ansible filter plugins used by the OpenShift pre-upgrade health check playbook.

All filters are pure functions over the JSON/dict structures returned by
kubernetes.core.k8s_info, so they can be unit tested outside of Ansible too
(see tests/test_ocp_health_filters.py).
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

SA_USERNAME_RE = re.compile(r"^system:serviceaccount:([^:]+):(.+)$")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _get(d: dict, path: str, default=None):
    """Safe dotted-path getter, e.g. _get(node, 'status.conditions', [])."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def _condition(conditions: List[dict], cond_type: str) -> Optional[dict]:
    for c in conditions or []:
        if c.get("type") == cond_type:
            return c
    return None


def _cond_true(conditions: List[dict], cond_type: str) -> Optional[bool]:
    c = _condition(conditions, cond_type)
    if c is None:
        return None
    return str(c.get("status", "")).lower() == "true"


def _severity_rank(sev: str) -> int:
    return {"OK": 0, "INFO": 1, "WARNING": 2, "CRITICAL": 3}.get(sev, 0)


def _parse_ts(ts: Optional[str]):
    """Parse a Kubernetes RFC3339 timestamp (always UTC, 'Z' suffix). Returns
    None on anything unparseable rather than raising - callers treat that as
    'unknown age' and err on the side of flagging it."""
    if not ts:
        return None
    ts = str(ts).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _safe_json_list(value: Any) -> List[dict]:
    """Best-effort parse of an etcdctl `-w json` capture into a list of dicts.

    Deliberately does its OWN parsing here rather than leaning on the Jinja
    `from_json` filter in the task file: depending on Ansible/Jinja2-native
    settings, a `set_fact` of a JSON-looking string can end up already
    converted to a native Python list/dict by the time it reaches a filter
    plugin (Jinja2's NativeEnvironment runs `ast.literal_eval` on rendered
    output), which then makes `from_json` blow up with "the JSON object must
    be str, bytes or bytearray, not list". Accepting either shape here - and
    never raising on genuinely bad input (the "(...)" placeholder text used
    when an exec call failed, empty output, truncated JSON, etc.) - avoids
    that class of bug entirely and keeps the raw text as the report's real
    source of truth either way."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text or text.startswith("("):
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _parse_took_ms(took: Any) -> Optional[float]:
    """Parse etcdctl's human 'took' duration (e.g. '12.345ms', '1.2s', '900µs')
    from `endpoint health` output into milliseconds."""
    if not took:
        return None
    m = re.match(r"^([\d.]+)\s*(ms|s|µs|us)$", str(took).strip())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    return value * {"ms": 1.0, "s": 1000.0, "µs": 0.001, "us": 0.001}[unit]


def _safe_json_dict(value: Any) -> Dict[str, Any]:
    """Best-effort parse of a `ceph ... -f json` capture into a dict - the
    object-shaped sibling of _safe_json_list above (ceph status/df return a
    JSON OBJECT, not a list). Same defensive rationale and the same etcdctl
    lesson applies: accept an already-native dict (Jinja2's NativeEnvironment
    may have converted a set_fact string before it reaches this filter), a
    raw JSON string, or the "(...)" placeholder text used when an exec call
    failed - and never raise, so a bad capture degrades to an empty dict
    rather than crashing the play."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else {}
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text or text.startswith("("):
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return parsed[0] if parsed and isinstance(parsed[0], dict) else {}
    return {}


def _parse_ocp_version(value: Any):
    """Parse a 'major.minor.patch[-suffix]' OCP version string into
    (major, minor, patch) ints, or None if it doesn't look like one."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


# ----------------------------------------------------------------------------
# 1. etcd cluster health
# ----------------------------------------------------------------------------
# Deliberately read-only: `etcdctl endpoint health`/`endpoint status`/`alarm
# list` only ever read state. We do NOT run `etcdctl check perf` here - it's
# a write-load generator and upstream etcd issues report it can grow the DB
# size significantly (etcd-io/etcd#9326) and produce false FAILs depending on
# load profile (etcd-io/etcd#10609, #13455) - not something to fire
# automatically against a production control plane right before an upgrade.
# For actual disk-hardware validation, Red Hat documents an `fio`-based test;
# that's surfaced as a manual, opt-in checklist item instead (see the
# etcd_manual_checklist item built in tasks/15_etcd_health.yml) rather than
# executed here, since it writes a sustained load to the etcd data disk.
#
# "IO health" here instead comes from etcdctl's own `endpoint health` round
# trip timer (the `took` field - a real linearizable-read latency measurement
# etcdctl already performs for you) plus dbSize-vs-quota from `endpoint
# status`. Both are safe, already-computed numbers - no extra load generated.
def etcd_health_report(
    pods: List[dict],
    health_items: Any,
    status_items: Any,
    alarm_output: Any,
    quota_bytes: int = 8589934592,
    warn_pct: float = 0.8,
    crit_pct: float = 0.95,
    took_warn_ms: float = 50.0,
    took_crit_ms: float = 300.0,
) -> Dict[str, Any]:
    # health_items/status_items may arrive as an already-parsed list (unit
    # tests, or the playbook passing raw etcdctl JSON text straight through)
    # or as a raw "-w json" string - _safe_json_list handles both, plus the
    # "(...)" placeholder/error text used when the exec call itself failed.
    health_items = _safe_json_list(health_items)
    status_items = _safe_json_list(status_items)

    pod_rows = []
    for p in pods or []:
        phase = _get(p, "status.phase", "Unknown")
        container_statuses = _get(p, "status.containerStatuses", []) or []
        ready = bool(container_statuses) and all(c.get("ready") for c in container_statuses)
        pod_rows.append(
            {
                "name": _get(p, "metadata.name"),
                "node": _get(p, "spec.nodeName", ""),
                "phase": phase,
                "ready": ready,
                "severity": "OK" if (phase == "Running" and ready) else "CRITICAL",
            }
        )

    health_rows = []
    for h in health_items or []:
        endpoint = h.get("endpoint") or h.get("Endpoint") or ""
        healthy = h.get("health")
        took_ms = _parse_took_ms(h.get("took"))
        error = h.get("error", "") or ""
        if healthy is False or error:
            severity = "CRITICAL"
        elif took_ms is not None and took_ms >= took_crit_ms:
            severity = "CRITICAL"
        elif took_ms is not None and took_ms >= took_warn_ms:
            severity = "WARNING"
        else:
            severity = "OK"
        health_rows.append(
            {"endpoint": endpoint, "healthy": healthy, "took_ms": took_ms, "error": error, "severity": severity}
        )

    status_rows = []
    leaders, raft_terms = set(), set()
    for s in status_items or []:
        endpoint = s.get("Endpoint") or s.get("endpoint") or ""
        st = s.get("Status") or s.get("status") or s
        db_size = st.get("dbSize", 0) or 0
        db_size_in_use = st.get("dbSizeInUse", db_size) or db_size
        leader = st.get("leader")
        raft_term = st.get("raftTerm")
        is_learner = bool(st.get("isLearner", False))
        version = st.get("version", "")
        if leader is not None:
            leaders.add(leader)
        if raft_term is not None:
            raft_terms.add(raft_term)

        pct = (db_size / quota_bytes) if quota_bytes else 0.0
        if pct >= crit_pct:
            severity = "CRITICAL"
        elif pct >= warn_pct or is_learner:
            severity = "WARNING"
        else:
            severity = "OK"

        status_rows.append(
            {
                "endpoint": endpoint,
                "version": version,
                "db_size_mb": round(db_size / 1048576, 1),
                "db_size_in_use_mb": round(db_size_in_use / 1048576, 1),
                "db_quota_pct": round(pct * 100, 1),
                "leader": leader,
                "raft_term": raft_term,
                "is_learner": is_learner,
                "severity": severity,
            }
        )

    cluster_findings = []
    if len(leaders) > 1:
        cluster_findings.append(
            {
                "severity": "CRITICAL",
                "message": f"etcd members disagree on the current leader ({len(leaders)} distinct leader IDs reported) - possible split-brain or an election in progress.",
            }
        )
    elif status_rows and not leaders:
        cluster_findings.append({"severity": "CRITICAL", "message": "no etcd member reported a leader."})
    if len(raft_terms) > 1:
        cluster_findings.append(
            {
                "severity": "WARNING",
                "message": f"etcd members report different raft terms ({sorted(raft_terms)}) - a leader election may be in progress or recently completed; re-check right before upgrading.",
            }
        )

    alarms = []
    for line in str(alarm_output or "").strip().splitlines():
        line = line.strip()
        if line:
            alarms.append({"raw": line, "severity": "CRITICAL"})

    return {"pods": pod_rows, "health": health_rows, "status": status_rows, "cluster_findings": cluster_findings, "alarms": alarms}


# ----------------------------------------------------------------------------
# 2. Node <-> MachineConfigPool matrix
# ----------------------------------------------------------------------------
def node_mcp_matrix(nodes: List[dict], pools: List[dict]) -> List[dict]:
    rows = []
    for node in nodes or []:
        name = _get(node, "metadata.name")
        labels = _get(node, "metadata.labels", {}) or {}
        annotations = _get(node, "metadata.annotations", {}) or {}
        roles = sorted(
            k.split("node-role.kubernetes.io/", 1)[1]
            for k in labels
            if k.startswith("node-role.kubernetes.io/")
        ) or ["<none>"]

        ready_cond = _cond_true(_get(node, "status.conditions", []), "Ready")
        schedulable = not bool(_get(node, "spec.unschedulable", False))

        current_cfg = annotations.get("machineconfiguration.openshift.io/currentConfig", "")
        desired_cfg = annotations.get("machineconfiguration.openshift.io/desiredConfig", "")
        mc_state = annotations.get("machineconfiguration.openshift.io/state", "")
        mc_reason = annotations.get("machineconfiguration.openshift.io/reason", "")

        # match the node to the (first) pool whose nodeSelector.matchLabels it satisfies
        matched_pool = None
        for pool in pools or []:
            selector = _get(pool, "spec.nodeSelector.matchLabels", {}) or {}
            if selector and all(labels.get(k) == v for k, v in selector.items()):
                matched_pool = pool
                # prefer a non-worker pool match (e.g. master/infra) over the generic worker pool
                if _get(pool, "metadata.name") != "worker":
                    break

        pool_name = _get(matched_pool, "metadata.name", "<unmatched>") if matched_pool else "<unmatched>"
        pool_rendered = _get(matched_pool, "status.configuration.name", "") if matched_pool else ""

        if not matched_pool:
            status = "NO_POOL_MATCH"
        elif mc_state and mc_state.lower() == "degraded":
            status = "DEGRADED"
        elif current_cfg and desired_cfg and current_cfg != desired_cfg:
            status = "UPDATING"
        elif pool_rendered and current_cfg and current_cfg != pool_rendered:
            status = "STALE"
        elif ready_cond is False:
            status = "NOT_READY"
        else:
            status = "OK"

        rows.append(
            {
                "node": name,
                "roles": roles,
                "ready": ready_cond,
                "schedulable": schedulable,
                "pool": pool_name,
                "node_current_config": current_cfg,
                "node_desired_config": desired_cfg,
                "pool_rendered_config": pool_rendered,
                "mc_state": mc_state,
                "mc_reason": mc_reason,
                "status": status,
                "severity": "CRITICAL"
                if status in ("DEGRADED", "NOT_READY", "NO_POOL_MATCH")
                else ("WARNING" if status in ("UPDATING", "STALE") else "OK"),
            }
        )
    return sorted(rows, key=lambda r: (r["pool"], r["node"]))


# ----------------------------------------------------------------------------
# 3. ClusterOperators
# ----------------------------------------------------------------------------
def co_report(operators: List[dict]) -> List[dict]:
    rows = []
    for co in operators or []:
        name = _get(co, "metadata.name")
        conditions = _get(co, "status.conditions", []) or []
        available = _cond_true(conditions, "Available")
        progressing = _cond_true(conditions, "Progressing")
        degraded = _cond_true(conditions, "Degraded")
        upgradeable = _cond_true(conditions, "Upgradeable")

        versions = {v.get("name"): v.get("version") for v in _get(co, "status.versions", []) or []}
        operator_version = versions.get("operator", "")

        messages = [
            f"{c.get('type')}={c.get('status')}: {c.get('message')}"
            for c in conditions
            if c.get("type") in ("Available", "Progressing", "Degraded", "Upgradeable")
            and c.get("message")
        ]

        if available is False or degraded is True:
            severity = "CRITICAL"
        elif upgradeable is False or progressing is True:
            severity = "WARNING"
        else:
            severity = "OK"

        rows.append(
            {
                "name": name,
                "version": operator_version,
                "available": available,
                "progressing": progressing,
                "degraded": degraded,
                "upgradeable": upgradeable,
                "severity": severity,
                "messages": messages,
            }
        )
    return sorted(rows, key=lambda r: (-_severity_rank(r["severity"]), r["name"]))


# ----------------------------------------------------------------------------
# 4. MachineConfigPools
# ----------------------------------------------------------------------------
def mcp_report(pools: List[dict]) -> List[dict]:
    rows = []
    for pool in pools or []:
        name = _get(pool, "metadata.name")
        status = _get(pool, "status", {}) or {}
        conditions = status.get("conditions", []) or []
        machine_count = status.get("machineCount", 0)
        ready = status.get("readyMachineCount", 0)
        updated = status.get("updatedMachineCount", 0)
        unavailable = status.get("unavailableMachineCount", 0)
        degraded_count = status.get("degradedMachineCount", 0)

        updated_cond = _cond_true(conditions, "Updated")
        updating_cond = _cond_true(conditions, "Updating")
        degraded_cond = _cond_true(conditions, "Degraded")
        node_degraded_cond = _cond_true(conditions, "NodeDegraded")
        render_degraded_cond = _cond_true(conditions, "RenderDegraded")

        counts_match = machine_count == ready == updated and unavailable == 0 and degraded_count == 0

        if degraded_cond or node_degraded_cond or render_degraded_cond or degraded_count > 0:
            severity = "CRITICAL"
        elif not counts_match or updating_cond:
            severity = "WARNING"
        else:
            severity = "OK"

        degraded_messages = [
            f"{c.get('type')}: {c.get('message')}"
            for c in conditions
            if str(c.get("status", "")).lower() == "true" and "degraded" in c.get("type", "").lower()
        ]

        rows.append(
            {
                "name": name,
                "machine_count": machine_count,
                "ready_machine_count": ready,
                "updated_machine_count": updated,
                "unavailable_machine_count": unavailable,
                "degraded_machine_count": degraded_count,
                "updated": updated_cond,
                "updating": updating_cond,
                "degraded": bool(degraded_cond or node_degraded_cond or render_degraded_cond),
                "counts_match": counts_match,
                "severity": severity,
                "messages": degraded_messages,
                "rendered_config": status.get("configuration", {}).get("name", ""),
            }
        )
    return sorted(rows, key=lambda r: (-_severity_rank(r["severity"]), r["name"]))


# ----------------------------------------------------------------------------
# 5. MachineSets vs Machines
# ----------------------------------------------------------------------------
def machineset_report(machinesets: List[dict], machines: List[dict]) -> List[dict]:
    machines = machines or []
    rows = []
    for ms in machinesets or []:
        name = _get(ms, "metadata.name")
        namespace = _get(ms, "metadata.namespace")
        desired = _get(ms, "spec.replicas", 0) or 0
        status = _get(ms, "status", {}) or {}
        current = status.get("replicas", 0)
        ready = status.get("readyReplicas", 0)
        available = status.get("availableReplicas", 0)

        owned = [
            m
            for m in machines
            if _get(m, "metadata.namespace") == namespace
            and (
                _get(m, "metadata.labels", {}).get("machine.openshift.io/cluster-api-machineset") == name
                or any(
                    o.get("kind") == "MachineSet" and o.get("name") == name
                    for o in _get(m, "metadata.ownerReferences", []) or []
                )
            )
        ]

        phase_counts: Dict[str, int] = {}
        problem_machines = []
        for m in owned:
            phase = _get(m, "status.phase", "Unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            node_ref = _get(m, "status.nodeRef.name")
            if phase not in ("Running",) or not node_ref:
                problem_machines.append(
                    {"name": _get(m, "metadata.name"), "phase": phase, "node": node_ref}
                )

        actual_count = len(owned)
        counts_consistent = (
            desired == current == ready == available == actual_count and not problem_machines
        )

        if problem_machines or actual_count < desired:
            severity = "CRITICAL"
        elif not counts_consistent:
            severity = "WARNING"
        else:
            severity = "OK"

        rows.append(
            {
                "name": name,
                "namespace": namespace,
                "desired": desired,
                "current": current,
                "ready": ready,
                "available": available,
                "actual_machine_count": actual_count,
                "phase_counts": phase_counts,
                "problem_machines": problem_machines,
                "counts_consistent": counts_consistent,
                "severity": severity,
            }
        )
    return sorted(rows, key=lambda r: (-_severity_rank(r["severity"]), r["name"]))


# ----------------------------------------------------------------------------
# 6. OpenShift Virtualization - per-VMI node-drain readiness
# ----------------------------------------------------------------------------
# Known-bad combination that stalls `oc adm drain` (and therefore the MCP
# rollout during an upgrade): evictionStrategy asks KubeVirt to live-migrate
# the VM off the node, but the VM isn't actually migratable. KubeVirt itself
# reports this via the VMI's LiveMigratable status condition (the same one
# `oc get vmis -o wide` shows in the LIVE-MIGRATABLE column, and the same one
# the upstream VMCannotBeEvicted alert fires on). Common reasons include a
# non-RWX-backed PVC/DataVolume, hostpath-provisioner storage, SR-IOV/host
# devices, bridge networking, and - the case reported in the field for this
# playbook - a read-only CD-ROM/ISO disk attached (libvirt refuses to migrate
# read-only disks: "Cannot migrate empty or read-only disk sda"). CD-ROM
# disks are flagged explicitly below even when LiveMigratable still reports
# True, since that combination has been seen to stall mid-migration anyway.
MIGRATE_STRATEGIES = ("LiveMigrate", "LiveMigrateIfPossible")


def vmi_migration_report(vmis: List[dict]) -> List[dict]:
    rows = []
    for vmi in vmis or []:
        name = _get(vmi, "metadata.name")
        namespace = _get(vmi, "metadata.namespace")
        phase = _get(vmi, "status.phase", "Unknown")
        node = _get(vmi, "status.nodeName", "")

        conditions = _get(vmi, "status.conditions", []) or []
        live_migratable = _cond_true(conditions, "LiveMigratable")
        lm_cond = _condition(conditions, "LiveMigratable") or {}
        lm_reason = lm_cond.get("reason", "")
        lm_message = lm_cond.get("message", "")

        eviction_strategy = _get(vmi, "spec.evictionStrategy") or _get(vmi, "status.evictionStrategy") or "cluster-default"

        disks = _get(vmi, "spec.domain.devices.disks", []) or []
        cdrom_disks = [d.get("name") for d in disks if isinstance(d, dict) and "cdrom" in d]

        reasons = []
        if phase != "Running":
            severity = "INFO"
            reasons.append(f"phase={phase}")
        elif eviction_strategy in MIGRATE_STRATEGIES and live_migratable is False:
            severity = "CRITICAL"
            why = f" ({lm_reason}: {lm_message})" if lm_reason else ""
            reasons.append(
                f"evictionStrategy={eviction_strategy} but LiveMigratable=False{why} "
                "- node drain will stall waiting on this VM during the upgrade"
            )
        elif cdrom_disks and eviction_strategy in MIGRATE_STRATEGIES:
            severity = "WARNING"
            reasons.append(
                f"has CD-ROM/ISO disk(s) [{', '.join(d for d in cdrom_disks if d)}] - read-only disks are "
                "known to stall live migration (libvirt refuses to migrate them) even when LiveMigratable=True; "
                "verify a test migration succeeds or switch this VM to evictionStrategy=None/shutdown before upgrading"
            )
        elif live_migratable is False:
            severity = "WARNING"
            why = f" ({lm_reason}: {lm_message})" if lm_reason else ""
            reasons.append(
                f"LiveMigratable=False{why}, but evictionStrategy={eviction_strategy} so it will be shut down "
                "(not stuck) on drain - confirm that downtime is acceptable during the upgrade"
            )
        else:
            severity = "OK"

        rows.append(
            {
                "name": name,
                "namespace": namespace,
                "node": node,
                "phase": phase,
                "eviction_strategy": eviction_strategy,
                "live_migratable": live_migratable,
                "cdrom_disks": [d for d in cdrom_disks if d],
                "severity": severity,
                "reasons": reasons,
            }
        )
    return sorted(rows, key=lambda r: (-_severity_rank(r["severity"]), r["namespace"] or "", r["name"] or ""))


# ----------------------------------------------------------------------------
# 7. Resources stuck terminating on a finalizer (Namespaces, PVs/PVCs, and a
#    dynamic sweep across every installed CustomResourceDefinition)
# ----------------------------------------------------------------------------
def crd_scan_targets(crds: List[dict], exclude_names: Optional[List[str]] = None) -> List[dict]:
    """Reduce a CustomResourceDefinition list down to {crd_name, group, version,
    kind, scope} for the ones actually worth scanning: skip anything the user
    excluded and anything that never became Established (broken/uninstalled)."""
    exclude = set(exclude_names or [])
    targets = []
    for crd in crds or []:
        name = _get(crd, "metadata.name")
        if not name or name in exclude:
            continue
        conditions = _get(crd, "status.conditions", []) or []
        if _cond_true(conditions, "Established") is False:
            continue
        versions = _get(crd, "spec.versions", []) or []
        storage_versions = [v for v in versions if v.get("storage")]
        served_versions = [v for v in versions if v.get("served")]
        chosen = (storage_versions or served_versions or [None])[0]
        if not chosen or not chosen.get("name"):
            continue
        targets.append(
            {
                "crd_name": name,
                "group": _get(crd, "spec.group"),
                "version": chosen.get("name"),
                "kind": _get(crd, "spec.names.kind"),
                "scope": _get(crd, "spec.scope", "Namespaced"),
            }
        )
    return targets


def _ns_excluded(ns: str, patterns: Optional[List[str]]) -> bool:
    """True when namespace `ns` matches an exact name or a trailing-'*'
    prefix pattern (e.g. "openshift-*") in `patterns`."""
    for pat in patterns or []:
        if pat.endswith("*") and ns.startswith(pat[:-1]):
            return True
        if pat == ns:
            return True
    return False


def finalizer_stuck_report(
    fixed_resources: Dict[str, List[dict]],
    crd_scan_results: List[dict],
    now_iso: str,
    stuck_after_seconds: int = 600,
    exclude_namespaces: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Flag any object with both a deletionTimestamp AND finalizers set - i.e.
    deletion was requested but something (a controller/webhook that no longer
    runs or is stuck) hasn't removed its finalizer yet, so the object is stuck
    Terminating. `fixed_resources` is {kind: [objects]} for the small set of
    built-in kinds we always check (Namespace/PersistentVolume/
    PersistentVolumeClaim); `crd_scan_results` is the raw registered result of
    looping kubernetes.core.k8s_info over crd_scan_targets() (each entry has
    `.item` = the target dict and `.resources`/`.failed` from the module).

    `exclude_namespaces` (exact names or trailing-'*' patterns, e.g.
    "openshift-*") skips namespaced objects in those namespaces and the
    matching Namespace objects themselves; cluster-scoped objects (PVs,
    cluster-scoped CRs) are always checked. `excluded` counts objects that
    would have been reported but were skipped this way."""
    now = _parse_ts(now_iso)
    rows: List[dict] = []
    excluded_count = 0

    def process(kind: str, crd_name: str, items: List[dict]):
        nonlocal excluded_count
        for obj in items or []:
            deletion_ts = _get(obj, "metadata.deletionTimestamp")
            finalizers = _get(obj, "metadata.finalizers", []) or []
            if not deletion_ts or not finalizers:
                continue
            ns = _get(obj, "metadata.namespace", "") or (_get(obj, "metadata.name", "") if kind == "Namespace" else "")
            if ns and _ns_excluded(ns, exclude_namespaces):
                excluded_count += 1
                continue
            dt = _parse_ts(deletion_ts)
            age_seconds = (now - dt).total_seconds() if (dt and now) else None
            if age_seconds is None or age_seconds >= stuck_after_seconds:
                severity = "CRITICAL"
            else:
                severity = "INFO"
            rows.append(
                {
                    "kind": kind,
                    "crd": crd_name,
                    "namespace": _get(obj, "metadata.namespace", ""),
                    "name": _get(obj, "metadata.name"),
                    "finalizers": finalizers,
                    "deletion_timestamp": deletion_ts,
                    "age_seconds": age_seconds,
                    "age_human": _format_duration(age_seconds),
                    "severity": severity,
                }
            )

    for kind, items in (fixed_resources or {}).items():
        process(kind, "", items)

    crds_failed = 0
    for result in crd_scan_results or []:
        if result.get("failed"):
            crds_failed += 1
            continue
        target = result.get("item", {}) or {}
        process(target.get("kind", "Unknown"), target.get("crd_name", ""), result.get("resources", []) or [])

    rows.sort(key=lambda r: (-_severity_rank(r["severity"]), r["kind"], r["namespace"] or "", r["name"] or ""))
    return {
        "rows": rows,
        "crds_scanned": len(crd_scan_results or []),
        "crds_failed": crds_failed,
        "excluded_namespaces": list(exclude_namespaces or []),
        "excluded": excluded_count,
    }


# ----------------------------------------------------------------------------
# 8. Deprecated / removed API usage, grouped per namespace
# ----------------------------------------------------------------------------
def _k8s_minor_tuple(v: str):
    m = re.match(r"^v?(\d+)\.(\d+)", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def deprecated_api_report(
    apirequestcounts: List[dict],
    exclude_namespaces: Optional[List[str]] = None,
    min_requests: int = 1,
    target_k8s_minor: str = "",
) -> Dict[str, Any]:
    exclude_namespaces = exclude_namespaces or []
    target_tuple = _k8s_minor_tuple(target_k8s_minor) if target_k8s_minor else None

    def excluded(ns: str) -> bool:
        return _ns_excluded(ns, exclude_namespaces)

    by_namespace: Dict[str, Dict[str, dict]] = {}
    cluster_summary: Dict[str, dict] = {}

    for item in apirequestcounts or []:
        resource_name = _get(item, "metadata.name")  # e.g. cronjobs.v1beta1.batch
        removed_in = _get(item, "status.removedInRelease", "") or ""
        if not removed_in:
            continue  # only interested in resources OpenShift itself flags as deprecated/removed

        removed_tuple = _k8s_minor_tuple(removed_in)
        if target_tuple and removed_tuple:
            severity = "CRITICAL" if removed_tuple <= target_tuple else "WARNING"
        else:
            severity = "WARNING"

        cluster_summary[resource_name] = {
            "resource": resource_name,
            "removedInRelease": removed_in,
            "requestCount24h": _get(item, "status.requestCount", 0),
            "severity": severity,
        }

        # walk currentHour + each entry of last24h for byNode -> byUser -> byVerb
        buckets = []
        current_hour = _get(item, "status.currentHour", {})
        if current_hour:
            buckets.append(current_hour)
        buckets.extend(_get(item, "status.last24h", []) or [])

        for bucket in buckets:
            for node in bucket.get("byNode", []) or []:
                for user in node.get("byUser", []) or []:
                    username = user.get("username", "")
                    request_count = user.get("requestCount", 0) or sum(
                        v.get("requestCount", 0) for v in user.get("byVerb", []) or []
                    )
                    if request_count < min_requests:
                        continue
                    verbs = sorted({v.get("verb") for v in user.get("byVerb", []) or [] if v.get("verb")})

                    m = SA_USERNAME_RE.match(username)
                    namespace = m.group(1) if m else "(non-namespaced / human user)"
                    principal = m.group(2) if m else username

                    if excluded(namespace):
                        continue

                    ns_bucket = by_namespace.setdefault(namespace, {})
                    entry = ns_bucket.setdefault(
                        resource_name,
                        {
                            "resource": resource_name,
                            "removedInRelease": removed_in,
                            "severity": severity,
                            "requestCount": 0,
                            "verbs": set(),
                            "principals": set(),
                        },
                    )
                    entry["requestCount"] += request_count
                    entry["verbs"].update(verbs)
                    entry["principals"].add(principal)

    # finalize sets -> sorted lists for JSON/template friendliness
    namespace_matrix = []
    for ns, resources in by_namespace.items():
        res_list = []
        for r in resources.values():
            r["verbs"] = sorted(r["verbs"])
            r["principals"] = sorted(r["principals"])
            res_list.append(r)
        res_list.sort(key=lambda r: (-_severity_rank(r["severity"]), -r["requestCount"]))
        namespace_matrix.append({"namespace": ns, "resources": res_list})

    namespace_matrix.sort(
        key=lambda n: (
            -max((_severity_rank(r["severity"]) for r in n["resources"]), default=0),
            n["namespace"],
        )
    )

    cluster_list = sorted(
        cluster_summary.values(), key=lambda r: (-_severity_rank(r["severity"]), r["resource"])
    )

    return {
        "cluster_summary": cluster_list,
        "namespace_matrix": namespace_matrix,
        "target_k8s_minor": target_k8s_minor,
    }


# ----------------------------------------------------------------------------
# 9. Advanced Cluster Management (ACM) - hub health, managed-cluster
#    inventory, and cascade-credential resolution
# ----------------------------------------------------------------------------
def acm_hub_report(mch_list: List[dict]) -> List[dict]:
    """Health of the ACM hub operator itself (the MultiClusterHub CR).

    Uses `status.phase` as the primary signal (Running/Installing/Updating/
    Error - the field ACM's own `oc get mch` output leads with) rather than
    assuming MultiClusterHub follows the ClusterOperator-style
    Available/Progressing/Degraded condition convention it doesn't strictly
    document; the `Complete` condition's message is surfaced too, when
    present, for extra context."""
    rows = []
    for mch in mch_list or []:
        phase = _get(mch, "status.phase", "Unknown")
        conditions = _get(mch, "status.conditions", []) or []
        complete = _condition(conditions, "Complete")
        message = (complete or {}).get("message", "")
        if phase == "Running":
            severity = "OK"
        elif phase == "Error":
            severity = "CRITICAL"
        elif phase in ("Installing", "Updating", "Pending", "Unknown", None, ""):
            severity = "WARNING"
        else:
            severity = "WARNING"
        rows.append(
            {
                "name": _get(mch, "metadata.name", "multiclusterhub"),
                "namespace": _get(mch, "metadata.namespace", ""),
                "phase": phase or "Unknown",
                "message": message,
                "version": _get(mch, "status.currentVersion", ""),
                "severity": severity,
            }
        )
    return rows


def acm_managed_cluster_report(managed_clusters: List[dict]) -> List[dict]:
    """Per-cluster health from ACM's own view of each ManagedCluster.

    Three conditions the hub's registration/klusterlet agents set matter
    here: ManagedClusterConditionAvailable (can the hub reach the spoke's API
    right now), HubAcceptedManagedCluster (did an admin/auto-approver accept
    the join request), and ManagedClusterJoined (did the initial handshake
    complete). All three should be True/True/True for a cluster it's safe to
    cascade a health check against - this report is also what
    acm_resolve_cascade_targets uses to decide which clusters are even worth
    trying credentials against."""
    rows = []
    for mc in managed_clusters or []:
        name = _get(mc, "metadata.name")
        conditions = _get(mc, "status.conditions", []) or []
        available_cond = _condition(conditions, "ManagedClusterConditionAvailable")
        available_status = (available_cond or {}).get("status", "Unknown")
        available = True if available_status == "True" else (False if available_status == "False" else None)
        accepted = _cond_true(conditions, "HubAcceptedManagedCluster")
        joined = _cond_true(conditions, "ManagedClusterJoined")
        labels = _get(mc, "metadata.labels", {}) or {}
        k8s_version = _get(mc, "status.version.kubernetes", "")

        if available is False or accepted is False:
            severity = "CRITICAL"
        elif available is None or joined is not True:
            severity = "WARNING"
        else:
            severity = "OK"

        rows.append(
            {
                "name": name,
                "available": available,
                "available_status": available_status,
                "accepted": accepted,
                "joined": joined,
                "kubernetes_version": k8s_version,
                "openshift_version": labels.get("openshiftVersion", ""),
                "vendor": labels.get("vendor", ""),
                "cloud": labels.get("cloud", ""),
                "severity": severity,
            }
        )
    rows.sort(key=lambda r: (-_severity_rank(r["severity"]), r["name"] or ""))
    return rows


def _decode_secret_value(resources: List[dict], keys: List[str]) -> Optional[str]:
    """Pull the first matching base64-encoded key out of a k8s_info Secret
    lookup's `.resources` list (0 or 1 items - a name+namespace get)."""
    if not resources:
        return None
    data = resources[0].get("data", {}) or {}
    raw = None
    for k in keys:
        if k in data:
            raw = data[k]
            break
    if raw is None and data:
        raw = next(iter(data.values()))
    if raw is None:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def acm_resolve_cascade_targets(
    managed_clusters: List[dict],
    managed_cluster_rows: List[dict],
    hive_secret_results: List[dict],
    msa_secret_results: List[dict],
    exclude_names: Optional[List[str]] = None,
    prefer_hive: bool = True,
    use_cluster_proxy: bool = False,
    cluster_proxy_base_url: str = "",
) -> List[Dict[str, Any]]:
    """Resolve, per managed cluster, whether (and how) the cascade run can
    authenticate to it - never silently drops a cluster, always returns one
    row per cluster with `checked` + a human `reason`.

    Preference order: a Hive-provisioned admin-kubeconfig Secret (the
    strongest credential - it's how ACM itself installed the cluster, and
    reading it needs no write to the hub) beats a ManagedServiceAccount
    token (works for imported clusters too, but needs the add-on enabled and
    RBAC granted on the spoke - see the accompanying ACM Policy manifest).
    A cluster with neither is reported `checked: false` with a specific
    reason rather than dropped from the list."""
    exclude_names = set(exclude_names or [])
    mc_by_name = {_get(mc, "metadata.name"): mc for mc in managed_clusters or []}

    def api_url(name: str) -> str:
        configs = _get(mc_by_name.get(name, {}), "spec.managedClusterClientConfigs", []) or []
        return configs[0].get("url", "") if configs else ""

    hive_kubeconfig: Dict[str, str] = {}
    for r in hive_secret_results or []:
        name = r.get("item")
        value = _decode_secret_value(r.get("resources") or [], ["kubeconfig", "kubeconfig.yaml"])
        if name and value:
            hive_kubeconfig[name] = value

    msa_token: Dict[str, str] = {}
    for r in msa_secret_results or []:
        name = r.get("item")
        value = _decode_secret_value(r.get("resources") or [], ["token"])
        if name and value:
            msa_token[name] = value

    targets = []
    for row in managed_cluster_rows or []:
        name = row["name"]
        entry = {
            "name": name,
            "checked": False,
            "auth_method": None,
            "host": "",
            "kubeconfig_content": "",
            "token": "",
            "reason": "",
        }
        if name in exclude_names:
            entry["reason"] = "excluded via acm_exclude_clusters"
            targets.append(entry)
            continue
        if row.get("available") is not True:
            entry["reason"] = f"cluster not Available (status: {row.get('available_status', 'Unknown')})"
            targets.append(entry)
            continue

        have_hive = name in hive_kubeconfig
        have_msa = name in msa_token
        use_hive = have_hive and (prefer_hive or not have_msa)
        use_msa = have_msa and not use_hive

        if use_hive:
            entry.update(
                checked=True,
                auth_method="kubeconfig",
                kubeconfig_content=hive_kubeconfig[name],
                reason="Hive-provisioned admin-kubeconfig secret",
            )
        elif use_msa:
            host = (cluster_proxy_base_url.rstrip("/") + "/" + name) if use_cluster_proxy else api_url(name)
            if not host:
                entry["reason"] = (
                    "ManagedServiceAccount token found but no reachable API URL "
                    "(spec.managedClusterClientConfigs is empty and cluster-proxy is not enabled)"
                )
            else:
                entry.update(
                    checked=True,
                    auth_method="token",
                    host=host,
                    token=msa_token[name],
                    reason="ManagedServiceAccount token" + (" via cluster-proxy" if use_cluster_proxy else " (direct API URL)"),
                )
        else:
            entry["reason"] = (
                "no credentials available (no Hive admin-kubeconfig secret, "
                "no synced ManagedServiceAccount token)"
            )
        targets.append(entry)

    return targets


# ----------------------------------------------------------------------------
# 10. Upgrade channel resolution (EUS-aware) + Cincinnati graph path lookup
# ----------------------------------------------------------------------------
def resolve_upgrade_channel(current_version: Any, upgrade_channel: Any) -> Dict[str, Any]:
    """Resolve a user-supplied `upgrade_channel` (blank, a bare prefix like
    'eus'/'stable'/'fast'/'candidate', or an already-qualified channel like
    'eus-4.20'/'stable-4.19') into a concrete Cincinnati channel name plus the
    target major.minor it points at.

    Bare-prefix auto-derivation rule (this is the actual EUS semantics - EUS
    releases are the even minors, and an EUS-to-EUS upgrade always lands on
    the *next* even minor, not just +1):
      - 'eus' + current minor is EVEN (already an EUS version) -> target is
        current + 2 (skip the intervening odd-minor EUS boundary)
      - 'eus' + current minor is ODD                            -> target is
        current + 1 (jump straight to the next EUS/even minor)
      - any other bare prefix (stable/fast/candidate/...)        -> target is
        always current + 1 (the immediate next minor)
    An already-qualified channel (has its own '-<major>.<minor>' suffix) is
    used as given; a target that isn't actually newer than the current
    version, or an 'eus-' channel whose target minor isn't even, comes back
    with a note rather than being rejected outright - Cincinnati itself is
    the real authority (see cincinnati_shortest_path), this is just naming.

    Returns {} when upgrade_channel is blank (nothing to resolve - the
    caller's task file is skipped entirely in that case). Never raises -
    unparseable input comes back as {'error': '...'}."""
    channel = str(upgrade_channel or "").strip()
    if not channel:
        return {}

    parsed = _parse_ocp_version(current_version)
    if not parsed:
        return {"error": f"could not parse current_version '{current_version}' as major.minor.patch"}
    major, minor, _patch = parsed

    qualified = re.match(r"^([a-z]+)-(\d+)\.(\d+)$", channel)
    if qualified:
        prefix, tgt_major, tgt_minor = qualified.group(1), int(qualified.group(2)), int(qualified.group(3))
        notes = []
        if prefix == "eus" and tgt_minor % 2 != 0:
            notes.append(f"EUS channels normally target an even minor - {tgt_major}.{tgt_minor} is odd")
        if (tgt_major, tgt_minor) <= (major, minor):
            notes.append(f"target {tgt_major}.{tgt_minor} is not newer than current {major}.{minor}")
        return {
            "channel": channel,
            "prefix": prefix,
            "target_major": tgt_major,
            "target_minor": tgt_minor,
            "is_eus_jump": prefix == "eus",
            "auto_derived": False,
            "reasoning": f"explicit channel '{channel}' -> target {tgt_major}.{tgt_minor}",
            "notes": notes,
        }

    bare = re.match(r"^([a-z]+)$", channel)
    if not bare:
        return {
            "error": (
                f"could not parse upgrade_channel '{channel}' - expected a bare prefix "
                "(e.g. 'stable', 'eus') or a qualified channel (e.g. 'stable-4.19', 'eus-4.20')"
            )
        }
    prefix = bare.group(1)
    is_eus = prefix == "eus"
    if is_eus:
        if minor % 2 == 0:
            tgt_minor = minor + 2
            reasoning = f"current {major}.{minor} is already EUS (even) - next EUS target is +2 -> {major}.{tgt_minor}"
        else:
            tgt_minor = minor + 1
            reasoning = f"current {major}.{minor} is odd - next EUS target is +1 -> {major}.{tgt_minor}"
    else:
        tgt_minor = minor + 1
        reasoning = f"'{prefix}' channel - targeting the next minor -> {major}.{tgt_minor}"

    return {
        "channel": f"{prefix}-{major}.{tgt_minor}",
        "prefix": prefix,
        "target_major": major,
        "target_minor": tgt_minor,
        "is_eus_jump": is_eus,
        "auto_derived": True,
        "reasoning": reasoning,
        "notes": [],
    }


def cincinnati_shortest_path(
    graph: Any, current_version: Any, target_major: int, target_minor: int
) -> Dict[str, Any]:
    """Given a raw Cincinnati /graph API response ({'nodes': [{'version': ...}, ...],
    'edges': [[from_index, to_index], ...]}), find the shortest sequence of
    Cincinnati-endorsed upgrade hops from `current_version` to the highest
    `target_major.target_minor.z` release present in the graph.

    Using the real graph (rather than hand-computing "current + 2") matters
    specifically for EUS: an EUS channel's graph does NOT contain a direct
    edge from e.g. 4.18.z straight to 4.20.z - it requires passing through
    4.19.z first (that's why EUS-to-EUS upgrades are a two-step process even
    though they're marketed/labelled together). BFS over the actual edges
    reflects that correctly instead of assuming a single hop.

    Never raises. Returns {'found': False, 'reason': '...'} for every failure
    mode (empty/missing graph, current version not present in this channel,
    no z-stream of the target minor present yet, or genuinely no path)."""
    nodes = (graph or {}).get("nodes") or []
    edges = (graph or {}).get("edges") or []
    if not nodes:
        return {"found": False, "reason": "empty or missing graph (no nodes returned)"}

    version_by_index = {i: n.get("version", "") for i, n in enumerate(nodes)}
    current_version = str(current_version or "")

    start_idx = None
    for i, v in version_by_index.items():
        if v == current_version:
            start_idx = i
            break
    if start_idx is None:
        return {
            "found": False,
            "reason": (
                f"current version {current_version} is not present in this channel's graph - "
                "you likely need to complete an intermediate upgrade (e.g. to a version already "
                "in this channel) before switching to it"
            ),
        }

    candidates = []
    for i, v in version_by_index.items():
        parsed = _parse_ocp_version(v)
        if parsed and parsed[0] == target_major and parsed[1] == target_minor:
            candidates.append((parsed[2], i, v))
    if not candidates:
        return {
            "found": False,
            "reason": f"no {target_major}.{target_minor}.z release is present in this channel's graph yet",
        }
    candidates.sort()
    _, target_idx, target_version = candidates[-1]

    if target_idx == start_idx:
        return {
            "found": True,
            "hops": [current_version],
            "target_version": current_version,
            "reason": "already at the target version",
        }

    adjacency: Dict[int, List[int]] = {}
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            continue
        a, b = edge
        adjacency.setdefault(a, []).append(b)

    from collections import deque

    visited = {start_idx}
    parent: Dict[int, int] = {}
    queue = deque([start_idx])
    found = False
    while queue:
        cur = queue.popleft()
        if cur == target_idx:
            found = True
            break
        for nxt in adjacency.get(cur, []):
            if nxt not in visited:
                visited.add(nxt)
                parent[nxt] = cur
                queue.append(nxt)

    if not found:
        return {
            "found": False,
            "reason": (
                f"no upgrade path from {current_version} to {target_version} in this channel's "
                "graph - they may not be directly connected (an intermediate channel switch may "
                "be required first)"
            ),
        }

    path_idx = [target_idx]
    while path_idx[-1] != start_idx:
        path_idx.append(parent[path_idx[-1]])
    path_idx.reverse()
    hops = [version_by_index[i] for i in path_idx]
    return {
        "found": True,
        "hops": hops,
        "target_version": target_version,
        "reason": f"{len(hops) - 1} hop(s)",
    }


# ----------------------------------------------------------------------------
# 11. OpenShift Data Foundation (ODF/OCS) + Ceph cluster health
# ----------------------------------------------------------------------------
def cephcluster_report(cephclusters: List[dict]) -> List[dict]:
    """Health of the CephCluster CR (ceph.rook.io/v1) managed by ODF's
    rook-ceph operator. Unlike StorageCluster (ocs.openshift.io/v1, which
    reuses co_report() below - it follows the same Available/Progressing/
    Degraded/Upgradeable condition convention as HCO/KubeVirt/CDI), the
    CephCluster CR does NOT follow that convention: it reports state via a
    top-level status.phase (Ready/Progressing/Failure/Connecting/...) plus a
    nested status.ceph.health summary (HEALTH_OK/HEALTH_WARN/HEALTH_ERR) that
    Rook itself keeps in sync with the storage cluster's actual `ceph status`.
    Shaped like co_report()'s rows (name/severity/messages) so the task file
    and templates can treat every component report uniformly."""
    rows = []
    for cc in cephclusters or []:
        name = _get(cc, "metadata.name")
        phase = _get(cc, "status.phase") or _get(cc, "status.state", "Unknown")
        ceph = _get(cc, "status.ceph", {}) or {}
        ceph_health = ceph.get("health", "") or ""
        ceph_details = ceph.get("details", {}) or {}
        last_checked = ceph.get("lastChecked", "")

        messages = [
            f"{check_name}: {detail.get('message')}"
            for check_name, detail in ceph_details.items()
            if isinstance(detail, dict) and detail.get("message")
        ]
        state_message = _get(cc, "status.message", "")
        if state_message:
            messages.append(state_message)

        if phase == "Failure" or ceph_health == "HEALTH_ERR":
            severity = "CRITICAL"
        elif phase == "Ready" and ceph_health in ("HEALTH_OK", ""):
            severity = "OK"
        else:
            # Progressing/Connecting/Unknown phases, or a HEALTH_WARN ceph
            # summary, are all worth a human's attention but aren't
            # necessarily an outage on their own.
            severity = "WARNING"

        rows.append(
            {
                "name": name,
                "phase": phase or "Unknown",
                "ceph_health": ceph_health or "Unknown",
                "last_checked": last_checked,
                "severity": severity,
                "messages": messages,
            }
        )
    return sorted(rows, key=lambda r: (-_severity_rank(r["severity"]), r["name"]))


def ceph_status_report(raw_status: Any) -> Dict[str, Any]:
    """Parse a best-effort `ceph status -f json` capture (executed via the
    rook-ceph-tools pod) into a structured summary: overall health, the
    individual named health checks Ceph itself is reporting, osdmap up/in
    counts, a pgmap summary, and mon quorum membership.

    Deliberately parsed here in Python rather than via Jinja `from_json` in
    the task file - the same defensive-parsing lesson already applied to
    etcdctl output (_safe_json_list/_safe_json_dict above): `ceph status`
    returns a JSON OBJECT, and depending on Ansible/Jinja2-native settings a
    set_fact of that text can already be a native dict by the time it
    reaches a filter plugin. Never raises - a failed/empty/malformed exec
    capture (including the "(...)" placeholder text used when the exec call
    itself failed) degrades to an 'unparsed' report rather than crashing the
    play, so the raw text captured alongside it remains the source of truth
    either way."""
    data = _safe_json_dict(raw_status)
    if not data:
        return {
            "parsed": False,
            "overall_status": "Unknown",
            "overall_severity": "WARNING",
            "checks": [],
            "osdmap": {},
            "pgmap": {},
            "mon": {},
        }

    health = data.get("health", {}) or {}
    overall_status = health.get("status", "Unknown")
    if overall_status == "HEALTH_OK":
        overall_severity = "OK"
    elif overall_status == "HEALTH_WARN":
        overall_severity = "WARNING"
    elif overall_status == "HEALTH_ERR":
        overall_severity = "CRITICAL"
    else:
        overall_severity = "WARNING"

    checks = []
    for check_name, check in (health.get("checks") or {}).items():
        check = check or {}
        check_severity_raw = check.get("severity", "")
        message = (check.get("summary") or {}).get("message", "")
        if check_severity_raw == "HEALTH_ERR":
            check_severity = "CRITICAL"
        elif check_severity_raw == "HEALTH_WARN":
            check_severity = "WARNING"
        else:
            check_severity = "OK"
        checks.append(
            {
                "name": check_name,
                "severity": check_severity,
                "message": message,
                "muted": bool(check.get("muted", False)),
            }
        )
    checks.sort(key=lambda c: (-_severity_rank(c["severity"]), c["name"]))

    # osdmap: some Ceph releases nest the counts one level deeper
    # (osdmap.osdmap.num_osds) - handle both shapes defensively.
    osdmap = data.get("osdmap", {}) or {}
    if "num_osds" not in osdmap and isinstance(osdmap.get("osdmap"), dict):
        osdmap = osdmap["osdmap"]
    num_osds = osdmap.get("num_osds", 0) or 0
    num_up_osds = osdmap.get("num_up_osds", 0) or 0
    num_in_osds = osdmap.get("num_in_osds", 0) or 0
    osd_severity = "CRITICAL" if num_osds and (num_up_osds < num_osds or num_in_osds < num_osds) else "OK"
    osdmap_summary = {
        "num_osds": num_osds,
        "num_up_osds": num_up_osds,
        "num_in_osds": num_in_osds,
        "num_remapped_pgs": osdmap.get("num_remapped_pgs", 0),
        "severity": osd_severity,
    }

    pgmap = data.get("pgmap", {}) or {}
    bytes_used = pgmap.get("bytes_used", 0) or 0
    bytes_total = pgmap.get("bytes_total", 0) or 0
    pct_used = round((bytes_used / bytes_total) * 100, 1) if bytes_total else 0.0
    pgs_by_state = pgmap.get("pgs_by_state", []) or []
    state_names = {s.get("state_name") for s in pgs_by_state}
    all_active_clean = bool(pgs_by_state) and state_names <= {"active+clean"}
    pgmap_summary = {
        "num_pgs": pgmap.get("num_pgs", 0),
        "num_pools": pgmap.get("num_pools", 0),
        "bytes_used": bytes_used,
        "bytes_avail": pgmap.get("bytes_avail", 0),
        "bytes_total": bytes_total,
        "pct_used": pct_used,
        "pgs_by_state": [{"state": s.get("state_name"), "count": s.get("count", 0)} for s in pgs_by_state],
        "severity": "OK" if all_active_clean else "WARNING",
    }

    mon_summary = {
        "quorum_names": data.get("quorum_names", []) or [],
        "quorum_count": len(data.get("quorum", []) or []),
    }

    return {
        "parsed": True,
        "overall_status": overall_status,
        "overall_severity": overall_severity,
        "checks": checks,
        "osdmap": osdmap_summary,
        "pgmap": pgmap_summary,
        "mon": mon_summary,
    }


# ----------------------------------------------------------------------------
# 12. Cluster operators installed snapshot (outputs/cluster_operators_installed.json)
# ----------------------------------------------------------------------------
# A deliberately minimal, fixed-shape data dump - NOT part of the
# findings/severity report elsewhere in this playbook. Downstream tooling
# consumes this file directly, so its shape is exactly:
#   {"cluster": {"current", "target", "channel"},
#    "operators": [{"name", "channel", "version", "catalog"}, ...]}
# and nothing else - no severity, no extra keys, no nulls.
_EUS_CHANNEL_RE = re.compile(r"^eus(-\d+\.\d+)?$", re.IGNORECASE)


def _trim_channel_family(channel: Any) -> str:
    """Collapse an OCP upgrade channel to the bare family name "EUS" when
    it's any EUS-family channel (bare "eus" or a qualified "eus-4.20"),
    regardless of which minor it targets - EUS is reported as one concept.
    Every other channel family (stable/fast/candidate/...) is returned
    exactly as given, full name intact - only EUS was asked to collapse."""
    text = str(channel or "")
    return "EUS" if _EUS_CHANNEL_RE.match(text) else text


def cluster_operators_snapshot(
    subscriptions: List[dict],
    csvs: List[dict],
    catalogsources: List[dict],
    current_version: Any,
    target_version: Any,
    channel: Any,
) -> Dict[str, Any]:
    """Build the exact fixed-shape structure written to
    outputs/cluster_operators_installed.json.

    Per operator: `name`/`channel` come straight from each Subscription's
    `spec.name`/`spec.channel`; `version` comes from the matching
    ClusterServiceVersion's own `spec.version` field - a real, authoritative
    field on the CSV (NOT parsed out of its name), which is why it correctly
    carries a build/prerelease suffix as-is (e.g. '4.18.27-rhodf',
    '4.18.0-202608142236') exactly like the live cluster reports it;
    `catalog` is the `spec.image` of the CatalogSource that Subscription's
    `spec.source`/`spec.sourceNamespace` points to (falling back to the
    Subscription's own namespace when `sourceNamespace` isn't set, since
    that's a valid CatalogSource location too) - this lets a downstream
    consumer tell Red Hat's own catalog apart from certified/marketplace/
    community WITHOUT this task filtering or guessing which catalog is
    "the real one".

    A Subscription with no installed CSV yet (still installing, or stuck),
    or whose CSV has no `spec.version` at all, is skipped - it has no
    resolvable version and this output's schema has no null fields.
    Duplicate installs of the same package (same name+channel+version,
    e.g. the same operator subscribed in more than one namespace) are
    consolidated into a single entry rather than repeated - if two
    Subscriptions for the same package differ in channel or version
    they are NOT considered duplicates and both are kept.

    `channel` (the top-level cluster.channel value) is collapsed to "EUS"
    for any EUS-family channel via `_trim_channel_family` - the caller
    decides WHICH channel string to pass in (the resolved upgrade_channel
    target, or the live cluster channel), this function only applies that
    one presentation rule. Never raises."""
    csv_version_by_namespaced_name: Dict[Any, str] = {}
    for csv in csvs or []:
        ns = _get(csv, "metadata.namespace", "")
        name = _get(csv, "metadata.name", "")
        version = _get(csv, "spec.version")
        if name and version:
            csv_version_by_namespaced_name[(ns, name)] = str(version)

    catalog_image_by_source: Dict[Any, str] = {}
    for cs in catalogsources or []:
        ns = _get(cs, "metadata.namespace", "")
        name = _get(cs, "metadata.name", "")
        image = _get(cs, "spec.image", "")
        if name:
            catalog_image_by_source[(ns, name)] = image

    operators: List[dict] = []
    seen: Dict[tuple, bool] = {}
    for sub in subscriptions or []:
        ns = _get(sub, "metadata.namespace", "")
        csv_name = _get(sub, "status.installedCSV") or _get(sub, "status.currentCSV")
        if not csv_name:
            continue
        version = csv_version_by_namespaced_name.get((ns, csv_name))
        if not version:
            continue
        op_name = _get(sub, "spec.name", "")
        op_channel = _get(sub, "spec.channel", "")

        dedup_key = (op_name, op_channel, version)
        if dedup_key in seen:
            continue
        seen[dedup_key] = True

        source_name = _get(sub, "spec.source", "")
        source_ns = _get(sub, "spec.sourceNamespace") or ns
        catalog_image = catalog_image_by_source.get((source_ns, source_name), "")

        operators.append(
            {
                "name": op_name,
                "channel": op_channel,
                "version": version,
                "catalog": catalog_image,
            }
        )

    return {
        "cluster": {
            "current": str(current_version or ""),
            "target": str(target_version or ""),
            "channel": _trim_channel_family(channel),
        },
        "operators": operators,
    }


# ----------------------------------------------------------------------------
# 13. Catalog opm render targeting (outputs/<catalog>_<tag>.json)
# ----------------------------------------------------------------------------
# Follow-on to the cluster operators installed snapshot (section 12): for
# every CatalogSource actually referenced by an installed Subscription, we
# want to `opm render` that exact catalog and keep only the olm.channel
# entries for packages this cluster has installed - so a downstream
# consumer gets real per-channel bundle-entry metadata (for computing
# replaces/skipRange upgrade paths itself) without this playbook rendering
# every package in a possibly huge catalog, or walking the OLM graph here.
# See tasks/89_catalog_opm_render.yml.

def catalog_render_targets(operators: List[dict], catalogsources: List[dict]) -> List[dict]:
    """Group cluster_operators_snapshot()'s own `operators` list (the same
    list written to cluster_operators_installed.json - deliberately NOT
    re-derived from raw Subscriptions here, so a stuck/no-version
    Subscription that snapshot already excluded can't sneak back in and
    the two output files always agree on which packages "count") by the
    catalog image each entry came from, then resolves that image back to
    the CatalogSource's own name+namespace (needed to find its Pod).

    Each entry: {"catalog_name", "catalog_namespace", "image",
    "packages": [sorted, deduplicated operator names]}. An operator whose
    catalog image doesn't match any known CatalogSource still gets a
    target (packages need the image to fall back to a raw-image opm
    render if the caller wants that later), just with an empty
    catalog_name/catalog_namespace - the task decides what to do with
    that. Never raises."""
    name_ns_by_image: Dict[str, tuple] = {}
    for cs in catalogsources or []:
        image = _get(cs, "spec.image", "")
        if image and image not in name_ns_by_image:
            name_ns_by_image[image] = (_get(cs, "metadata.name", ""), _get(cs, "metadata.namespace", ""))

    packages_by_image: Dict[str, set] = {}
    for op in operators or []:
        image = op.get("catalog", "")
        name = op.get("name", "")
        if not image or not name:
            continue
        packages_by_image.setdefault(image, set()).add(name)

    targets = []
    for image, packages in packages_by_image.items():
        catalog_name, catalog_ns = name_ns_by_image.get(image, ("", ""))
        targets.append(
            {
                "catalog_name": catalog_name,
                "catalog_namespace": catalog_ns,
                "image": image,
                "packages": sorted(packages),
            }
        )
    return sorted(targets, key=lambda t: (t["catalog_namespace"], t["catalog_name"], t["image"]))


def opm_source_path(pod: dict) -> str:
    """Extract the <source_path> argument `opm serve` was started with,
    from a CatalogSource Pod's own spec - so opm_render can be run against
    that exact local path inside the already-running pod (no image
    re-pull needed, and it works whether the catalog is a file-based
    config directory like /configs or a legacy sqlite DB).

    Looks at the first container's command+args combined (OLM has shipped
    both `command: [opm], args: [serve, /configs]` and
    `command: [/bin/opm, serve, /configs, ...]` across versions): finds
    the literal 'serve' token and returns the first following argument
    that isn't itself a flag (doesn't start with '-'). Returns '' if no
    such path is found - the caller treats that as "skip this catalog".
    Never raises."""
    containers = _get(pod, "spec.containers", []) or []
    if not containers:
        return ""
    argv = list(_get(containers[0], "command", []) or []) + list(_get(containers[0], "args", []) or [])
    for i, tok in enumerate(argv):
        if str(tok) == "serve":
            for nxt in argv[i + 1:]:
                if not str(nxt).startswith("-"):
                    return str(nxt)
            break
    return ""


_IMAGE_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def catalog_render_filename(image: Any) -> str:
    """Turn a catalog image ref into a filesystem-safe '<name>_<tag>' stem
    (no extension) for outputs/<name>_<tag>.json, e.g.
    'registry.redhat.io/redhat/redhat-operator-index:v4.20' ->
    'redhat-operator-index_v4.20', and equally for an airgapped mirror
    like 'private.registry.local:5000/mirror/redhat-operator-index:v4.20'
    - the registry host[:port] never ends up in the filename, only the
    repo's last path segment and its tag/short-digest.

    Falls back to 'catalog' (or 'catalog_<n>' - left to the caller to
    dedupe) if the image string doesn't parse at all. Never raises."""
    text = str(image or "")
    if not text:
        return "catalog"
    # Split the LAST path segment off first, then split ITS tag/digest -
    # so a registry's own 'host:port' is never mistaken for an image tag.
    _, _, ref = text.rpartition("/")
    if "@" in ref:
        name_part, _, digest = ref.partition("@")
        tag = digest.rsplit(":", 1)[-1][:12]  # short digest, not the whole sha256:...
    elif ":" in ref:
        name_part, _, tag = ref.partition(":")
    else:
        name_part, tag = ref, ""
    name_part = _IMAGE_NAME_SANITIZE_RE.sub("-", name_part).strip("-") or "catalog"
    tag = _IMAGE_NAME_SANITIZE_RE.sub("-", tag).strip("-")
    return f"{name_part}_{tag}" if tag else name_part


def opm_render_filter(raw_stdout: Any, packages: List[str]) -> List[dict]:
    """Parse `opm render <path> -o json` output - newline-delimited JSON
    declarative-config objects, NOT a single JSON array or a JSON stream
    that json.loads can eat in one call - and keep only the olm.channel
    entries for the given packages, in exactly the shape a downstream
    consumer needs to walk replaces/skipRange itself:
      [{"package": "...", "channel": "...", "entries": ["pkg.v1.0.0", ...]}]

    Other schemas (olm.package, olm.bundle, ...) are dropped here - only
    olm.channel carries the entries list a channel-graph walk needs, and
    keeping everything would make this file as large as the catalog
    itself for no benefit to that use case. Malformed/non-JSON lines are
    skipped rather than failing the whole parse (some opm versions can
    interleave log lines with -o json). Never raises - returns [] on
    total failure, same defensive-parsing posture as ceph_status_report()."""
    wanted = set(packages or [])
    out: List[dict] = []
    for line in str(raw_stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("schema") != "olm.channel":
            continue
        package = obj.get("package")
        if not package or package not in wanted:
            continue
        entries = obj.get("entries") or []
        entry_names = [e.get("name") for e in entries if isinstance(e, dict) and e.get("name")]
        out.append({"package": package, "channel": obj.get("name", ""), "entries": entry_names})
    return out


# ----------------------------------------------------------------------------
# 13b. Catalog mirror (IDMS/ICSP/ITMS) check
# ----------------------------------------------------------------------------
# How OpenShift mirror configuration actually redirects pulls (CRI-O's
# registries.conf, rendered by the MCO from these CRs):
#   - ImageDigestMirrorSet (IDMS) and its deprecated predecessor
#     ImageContentSourcePolicy (ICSP) only redirect pulls BY DIGEST
#     (image@sha256:...) - operator bundle/operand images and release
#     payloads.
#   - ImageTagMirrorSet (ITMS) redirects pulls BY TAG (image:v4.20). Catalog
#     index images are normally referenced by tag, so without an ITMS
#     covering it a default CatalogSource like redhat-operators keeps
#     pulling its index straight from registry.redhat.io - even on a
#     cluster that has an IDMS.
#   - mirrorSourcePolicy (IDMS/ITMS only, per entry): AllowContactingSource
#     (the default) tries the mirrors first and FALLS BACK to the source
#     registry when the mirror misses; NeverContactSource never falls back,
#     so an image missing from the mirror fails to pull. ICSP has no such
#     field and always falls back.
# So with any IDMS/ICSP/ITMS the cluster is treated as mirrored even if it
# can still reach the internet: a default catalog next to the mirror lets
# OLM pick bundles that were never mirrored, which today only work through
# the fallback and break once the cluster is disconnected (or immediately
# with NeverContactSource). See tasks/89b_catalog_mirror_check.yml.

DEFAULT_CATALOG_SOURCES = ["redhat-operators", "certified-operators", "community-operators", "redhat-marketplace"]


def _image_host(image: Any) -> str:
    """Registry host[:port] of an image ref or mirror location - the first
    path segment, but only when it looks like a host (has a '.' or ':', or
    is 'localhost'); a bare 'repo/name' has no explicit registry."""
    first, sep, _ = str(image or "").partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return ""


def _split_image_ref(image: Any) -> tuple:
    """('registry/repo/name', 'digest'|'tag') for an image ref. A ref with no
    tag or digest is pulled as :latest, so it counts as 'tag'."""
    text = str(image or "")
    if "@" in text:
        return text.split("@", 1)[0], "digest"
    head, sep, last = text.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    return (head + sep + last), "tag"


def _mirror_source_covers(source: str, repo: str) -> bool:
    """True when a mirror-set `source` applies to `repo`: an exact match, a
    path prefix ending on a '/' boundary, or a '*.example.com' wildcard
    matching the repo's host (IDMS/ITMS allow wildcard hosts)."""
    source = str(source or "").rstrip("/")
    if not source or not repo:
        return False
    if source.startswith("*."):
        host = _image_host(repo) or repo.split("/", 1)[0]
        return host.endswith(source[1:])
    return repo == source or repo.startswith(source + "/")


def _mirror_rewrite(image: str, source: str, mirror: str) -> str:
    """The image ref CRI-O actually pulls when mirror-set `source` redirects
    `image` to `mirror`: the matched prefix (or wildcard host) is replaced,
    the rest of the path and the tag/digest kept."""
    source = source.rstrip("/")
    if source.startswith("*."):
        host = image.split("/", 1)[0]
        return mirror.rstrip("/") + image[len(host):]
    return mirror.rstrip("/") + image[len(source):]


def catalog_mirror_report(
    idms: List[dict],
    icsp: List[dict],
    operatorhub: List[dict],
    catalogsources: List[dict],
    subscriptions: List[dict],
    installplans: List[dict],
    default_sources: Optional[List[str]] = None,
    marketplace_namespace: str = "openshift-marketplace",
    itms: Optional[List[dict]] = None,
    csvs: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Cross-check mirror configuration against OLM catalog usage.

    - mirror_configured: any ImageDigestMirrorSet, ImageContentSourcePolicy
      or ImageTagMirrorSet exists - each redirects pulls to the mirror, so
      any one makes this a mirrored cluster even if it can still reach the
      internet; none means a connected cluster. mirror_sets lists every
      entry (source, mirrors, mirrorSourcePolicy - ICSP always
      'AllowContactingSource'); mirror_hosts are the registry hosts of every
      mirror location.
    - pull_policy: digest_mirroring/tag_mirroring (any IDMS/ICSP resp. ITMS
      entry), and per kind whether any entry falls back to the source
      (AllowContactingSource) or all are NeverContactSource.
    - catalog_pull_paths: per CatalogSource, how its index image is really
      pulled - 'mirror-host' (image already points at a mirror host),
      'IDMS'/'ICSP'/'ITMS' (redirected by that mirror set, with its policy)
      or 'source' (not redirected: pulled straight from its own registry).
    - default_sources: one row per default OperatorHub source - present
      (CatalogSource still exists in marketplace_namespace) and disabled
      (OperatorHub/cluster spec.disableAllDefaultSources or a per-source
      `disabled: true`).
    - operators: one row per Subscription. The catalog that installed the
      current CSV is read from the InstallPlan the Subscription references
      (status.installPlanRef): the bundleLookups entry for that CSV, else
      any bundleLookup, else the legacy spec.catalogSource. Each catalog is
      classified 'mirrored' (pulled from the mirror - see catalog_pull_paths),
      'default' (a default OperatorHub source), 'missing' (CatalogSource no
      longer exists), 'other' (custom catalog pulled from its own registry)
      or 'unknown' (no InstallPlan/catalog found).
    - catalogs: installed operators grouped by catalog image, so a consumer
      can `opm render` each image once for all its operators. An operator
      belongs to the catalog its InstallPlan installed the current CSV from
      (the Subscription's source when no InstallPlan is found). Per image:
      the CatalogSource(s), pull_image (the ref actually pulled - the mirror
      location when an IDMS/ICSP/ITMS entry redirects it, else the image
      itself), pulled_from, and packages: one {name (package name),
      channel (Subscription), version (installed CSV's spec.version when
      `csvs` is given)} per operator - the same package+channel+version in
      several namespaces is one entry. Operators whose catalog can't be
      resolved to an image go to unresolved_operators.
    - notes: plain-language explanations of what the mirror config means
      for this cluster, for the report.
    - findings (mirrored clusters only): CRITICAL when any default
      CatalogSource is still present, and per operator whose Subscription
      or InstallPlan is bound to a default/missing catalog - OLM resolves
      its updates outside the mirror, which blocks the upgrade. INFO for
      'other'/'unknown' operators, for digest/tag mirrors that fall back to
      the source registry, and for any (deprecated) ICSP still in use.
    Never raises."""
    defaults = list(default_sources if default_sources is not None else DEFAULT_CATALOG_SOURCES)

    mirror_sets: List[dict] = []
    for kind, items, list_key in (
        ("ImageDigestMirrorSet", idms, "imageDigestMirrors"),
        ("ImageContentSourcePolicy", icsp, "repositoryDigestMirrors"),
        ("ImageTagMirrorSet", itms, "imageTagMirrors"),
    ):
        for item in items or []:
            entries = []
            for entry in _get(item, "spec." + list_key, []) or []:
                if not isinstance(entry, dict):
                    continue
                entries.append({
                    "source": str(entry.get("source", "")),
                    "mirrors": [str(m) for m in entry.get("mirrors", []) or [] if m],
                    # ICSP has no mirrorSourcePolicy and always falls back.
                    "policy": "AllowContactingSource" if kind == "ImageContentSourcePolicy"
                    else (entry.get("mirrorSourcePolicy") or "AllowContactingSource"),
                })
            mirror_sets.append({
                "kind": kind,
                "short": {"ImageDigestMirrorSet": "IDMS", "ImageContentSourcePolicy": "ICSP", "ImageTagMirrorSet": "ITMS"}[kind],
                "name": _get(item, "metadata.name", ""),
                "entries": entries,
            })
    mirror_hosts = sorted({h for ms in mirror_sets for e in ms["entries"] for h in (_image_host(m) for m in e["mirrors"]) if h})
    mirror_configured = len(mirror_sets) > 0

    def entries_of(*shorts):
        return [(ms, e) for ms in mirror_sets if ms["short"] in shorts for e in ms["entries"]]

    digest_entries = entries_of("IDMS", "ICSP")
    tag_entries = entries_of("ITMS")
    pull_policy = {
        "digest_mirroring": bool(digest_entries),
        "digest_fallback": [f"{ms['short']}/{ms['name']} {e['source']}" for ms, e in digest_entries if e["policy"] != "NeverContactSource"],
        "digest_never_contact": [f"{ms['short']}/{ms['name']} {e['source']}" for ms, e in digest_entries if e["policy"] == "NeverContactSource"],
        "tag_mirroring": bool(tag_entries),
        "tag_fallback": [f"ITMS/{ms['name']} {e['source']}" for ms, e in tag_entries if e["policy"] != "NeverContactSource"],
        "tag_never_contact": [f"ITMS/{ms['name']} {e['source']}" for ms, e in tag_entries if e["policy"] == "NeverContactSource"],
    }

    def pull_path(image: str) -> Dict[str, Any]:
        """How this image is really pulled, given the mirror sets."""
        repo, ref_type = _split_image_ref(image)
        if mirror_hosts and _image_host(image) in mirror_hosts:
            return {"ref_type": ref_type, "path": "mirror-host", "via": "", "policy": ""}
        for ms, e in (digest_entries if ref_type == "digest" else tag_entries):
            if e["mirrors"] and _mirror_source_covers(e["source"], repo):
                return {"ref_type": ref_type, "path": ms["short"], "via": f"{ms['short']}/{ms['name']}", "policy": e["policy"],
                        "pull_image": _mirror_rewrite(image, e["source"], e["mirrors"][0])}
        return {"ref_type": ref_type, "path": "source", "via": "", "policy": ""}

    hub = next((h for h in operatorhub or [] if _get(h, "metadata.name") == "cluster"), {})
    disable_all = bool(_get(hub, "spec.disableAllDefaultSources", False))
    hub_disabled = {s.get("name"): bool(s.get("disabled")) for s in (_get(hub, "spec.sources", []) or []) if isinstance(s, dict)}

    cs_by_key: Dict[tuple, dict] = {}
    catalog_pull_paths = []
    for cs in catalogsources or []:
        ns, name, image = _get(cs, "metadata.namespace", ""), _get(cs, "metadata.name", ""), _get(cs, "spec.image", "")
        cs_by_key[(ns, name)] = cs
        row = {"name": name, "namespace": ns, "image": image,
               "default": ns == marketplace_namespace and name in defaults}
        row.update(pull_path(image))
        catalog_pull_paths.append(row)
    catalog_pull_paths.sort(key=lambda r: (not r["default"], r["namespace"], r["name"]))
    path_by_key = {(r["namespace"], r["name"]): r for r in catalog_pull_paths}

    default_rows = []
    for name in defaults:
        cs = cs_by_key.get((marketplace_namespace, name))
        default_rows.append({
            "name": name,
            "present": cs is not None,
            "disabled": disable_all or hub_disabled.get(name, False),
            "image": _get(cs, "spec.image", "") if cs else "",
        })
    defaults_present = [d["name"] for d in default_rows if d["present"]]

    def classify(ns: str, name: str) -> tuple:
        if not name:
            return "unknown", ""
        cs = cs_by_key.get((ns, name))
        image = _get(cs, "spec.image", "") if cs else ""
        if ns == marketplace_namespace and name in defaults:
            return "default", image
        if cs is None:
            return "missing", ""
        if path_by_key[(ns, name)]["path"] != "source":
            return "mirrored", image
        return "other", image

    # What happens to a bundle that was never mirrored - the consequence an
    # old-catalog InstallPlan actually runs into.
    if pull_policy["digest_mirroring"] and not pull_policy["digest_fallback"]:
        unmirrored_bundle = "with NeverContactSource on every digest mirror, a bundle missing from the mirror fails to pull (ImagePullBackOff)"
    else:
        unmirrored_bundle = "a bundle missing from the mirror only pulls through the fallback to the source registry, and fails once the cluster is disconnected"

    ip_by_key = {(_get(ip, "metadata.namespace", ""), _get(ip, "metadata.name", "")): ip for ip in installplans or []}
    csv_version = {(_get(c, "metadata.namespace", ""), _get(c, "metadata.name", "")): str(_get(c, "spec.version", "") or "")
                   for c in csvs or []}

    catalogs_by_image: Dict[str, dict] = {}
    unresolved_operators: List[dict] = []
    operators = []
    for sub in subscriptions or []:
        ns = _get(sub, "metadata.namespace", "")
        csv = _get(sub, "status.installedCSV") or _get(sub, "status.currentCSV") or ""
        sub_source = _get(sub, "spec.source", "")
        sub_source_ns = _get(sub, "spec.sourceNamespace") or ns
        sub_kind, _ = classify(sub_source_ns, sub_source)

        ip_ref = _get(sub, "status.installPlanRef") or {}
        ip_name = ip_ref.get("name") or _get(sub, "status.installplan.name", "")
        ip = ip_by_key.get((ip_ref.get("namespace") or ns, ip_name)) if ip_name else None
        ip_cat_name, ip_cat_ns = "", ""
        if ip:
            lookups = _get(ip, "status.bundleLookups", []) or []
            match = [b for b in lookups if b.get("identifier") == csv] or lookups
            ref = (match[0].get("catalogSourceRef") or {}) if match else {}
            ip_cat_name = ref.get("name") or _get(ip, "spec.catalogSource", "")
            ip_cat_ns = ref.get("namespace") or _get(ip, "spec.catalogSourceNamespace") or ns
        ip_kind, ip_image = classify(ip_cat_ns, ip_cat_name)

        severity, message = "OK", ""
        if mirror_configured:
            if ip_kind in ("default", "missing") or sub_kind in ("default", "missing"):
                severity = "CRITICAL"
                if ip_kind in ("default", "missing") and sub_kind == "mirrored":
                    message = (f"installed from {ip_kind} catalog {ip_cat_name} (InstallPlan {ip_name}); Subscription already "
                               f"points to mirrored catalog {sub_source} - the next update resolves from the mirror")
                elif ip_kind in ("default", "missing"):
                    message = (f"installed from {ip_kind} catalog {ip_cat_name} (InstallPlan {ip_name}) and Subscription still "
                               f"points to {sub_source} - re-point it to the mirrored CatalogSource; {unmirrored_bundle}")
                else:
                    message = (f"Subscription points to {sub_kind} catalog {sub_source} - re-point it to the mirrored CatalogSource; "
                               f"{unmirrored_bundle}")
            elif ip_kind in ("other", "unknown"):
                severity = "INFO"
                message = (f"no InstallPlan catalog found for {csv or 'this Subscription'}" if ip_kind == "unknown"
                           else f"installed from catalog {ip_cat_name} whose image {ip_image} is not pulled from the mirror "
                                f"(not on a mirror host, and no IDMS/ICSP/ITMS entry covers it)")

        # The catalog an operator belongs to is the one its InstallPlan
        # installed the current CSV from; the Subscription's source is the
        # fallback when no InstallPlan is found (garbage-collected, or never
        # created). The version is the installed CSV's spec.version.
        if ip_cat_name and (ip_cat_ns, ip_cat_name) in cs_by_key:
            cat_name, cat_ns = ip_cat_name, ip_cat_ns
        else:
            cat_name, cat_ns = sub_source, sub_source_ns
        cat_cs = cs_by_key.get((cat_ns, cat_name))
        cat_image = _get(cat_cs, "spec.image", "") if cat_cs else ""
        pkg_entry = {
            "name": _get(sub, "spec.name", ""),
            "channel": _get(sub, "spec.channel", "") or "",
            "version": csv_version.get((ns, csv), ""),
        }
        if not cat_image:
            unresolved_operators.append(dict(pkg_entry, catalog_source=cat_name, catalog_source_namespace=cat_ns))
        else:
            path = path_by_key[(cat_ns, cat_name)]
            cat = catalogs_by_image.setdefault(cat_image, {
                "image": cat_image,
                "pull_image": path.get("pull_image", cat_image),
                "pulled_from": path["path"],
                "catalog_sources": [],
                "default": False,
                "packages": [],
            })
            ref = {"name": cat_name, "namespace": cat_ns}
            if ref not in cat["catalog_sources"]:
                cat["catalog_sources"].append(ref)
            cat["default"] = cat["default"] or path["default"]
            # Same package+channel+version installed in several namespaces
            # is one entry - the catalog lookup is identical.
            if pkg_entry not in cat["packages"]:
                cat["packages"].append(pkg_entry)

        operators.append({
            "package": _get(sub, "spec.name", ""),
            "namespace": ns,
            "csv": csv,
            "subscription_source": sub_source,
            "subscription_source_kind": sub_kind,
            "installplan": ip_name,
            "installplan_catalog": ip_cat_name,
            "installplan_catalog_kind": ip_kind,
            "installplan_catalog_image": ip_image,
            "severity": severity,
            "message": message,
        })
    operators.sort(key=lambda o: (-_severity_rank(o["severity"]), o["namespace"], o["package"]))

    catalogs = []
    for image in sorted(catalogs_by_image):
        cat = catalogs_by_image[image]
        cat["packages"].sort(key=lambda p: (p["name"], p["channel"], p["version"]))
        catalogs.append(cat)
    unresolved_operators.sort(key=lambda o: (o["name"], o["channel"]))

    notes: List[str] = []
    if not mirror_configured:
        notes.append("Connected cluster: no ImageDigestMirrorSet, ImageContentSourcePolicy or ImageTagMirrorSet, "
                     "so every image is pulled from its own registry and the default catalogs are expected.")
    else:
        notes.append("Mirrored cluster: any IDMS/ICSP/ITMS makes CRI-O try the mirror first, even when the cluster can "
                     "still reach the internet, so operators must come from mirrored catalogs.")
        if pull_policy["digest_mirroring"]:
            if pull_policy["digest_fallback"]:
                notes.append("Digest pulls (operator bundles and operands, release payload) fall back to the source registry when "
                             "the mirror misses (AllowContactingSource, and always for ICSP): "
                             + "; ".join(pull_policy["digest_fallback"])
                             + ". An image that was never mirrored still works today and breaks once the cluster is disconnected.")
            if pull_policy["digest_never_contact"]:
                notes.append("Digest pulls never contact the source (NeverContactSource): "
                             + "; ".join(pull_policy["digest_never_contact"])
                             + ". Any image missing from the mirror fails to pull.")
        else:
            notes.append("No IDMS/ICSP: digest pulls (operator bundles, release payload) are not redirected to the mirror.")
        if pull_policy["tag_mirroring"]:
            notes.append("Tag pulls are redirected by ITMS: " + "; ".join(pull_policy["tag_fallback"] + pull_policy["tag_never_contact"])
                         + ("." if not pull_policy["tag_never_contact"] else " (NeverContactSource entries never fall back)."))
        else:
            notes.append("No ImageTagMirrorSet: images referenced by tag - which is how catalog index images are normally "
                         "referenced - are NOT redirected. IDMS/ICSP only apply to digest pulls.")
        for r in catalog_pull_paths:
            if r["default"] and r["path"] == "source":
                notes.append(f"Default catalog {r['name']} ({r['image']}) is pulled straight from {_image_host(r['image']) or 'its registry'} "
                             f"(by {r['ref_type']}, no mirror set covers it), so it lists bundles that may never have been mirrored.")
        if any(ms["short"] == "ICSP" for ms in mirror_sets):
            notes.append("ImageContentSourcePolicy is deprecated: convert it with `oc adm migrate icsp` to IDMS/ITMS, which also "
                         "support mirrorSourcePolicy.")

    findings = []
    if mirror_configured and defaults_present:
        direct = [r["name"] for r in catalog_pull_paths if r["default"] and r["path"] == "source"]
        findings.append({
            "severity": "CRITICAL",
            "summary": (f"image mirroring is configured ({', '.join(ms['short'] + '/' + ms['name'] for ms in mirror_sets)}) but default "
                        f"CatalogSource(s) {', '.join(defaults_present)} are still present in {marketplace_namespace}"
                        f"{' (OperatorHub disableAllDefaultSources=true but they still exist)' if disable_all else ''}"
                        f"{'; ' + ', '.join(direct) + ' index pulled straight from the source registry (no ITMS covers it)' if direct else ''}"
                        f" - OLM can resolve bundles that were never mirrored ({unmirrored_bundle}). Disable them via "
                        "OperatorHub/cluster spec.disableAllDefaultSources so OLM only resolves from mirrored catalogs."),
        })
    for op in operators:
        if op["severity"] != "OK":
            findings.append({"severity": op["severity"], "summary": f"{op['package']} ({op['namespace']}): {op['message']}"})
    if mirror_configured and pull_policy["digest_fallback"]:
        findings.append({"severity": "INFO", "summary": (
            "digest mirrors fall back to the source registry (AllowContactingSource/ICSP): " + "; ".join(pull_policy["digest_fallback"])
            + " - an image missing from the mirror is pulled from the internet without notice; set mirrorSourcePolicy: "
            "NeverContactSource on the IDMS to prove the mirror is complete.")})
    if any(ms["short"] == "ICSP" for ms in mirror_sets):
        findings.append({"severity": "INFO", "summary": (
            "deprecated ImageContentSourcePolicy in use (" + ", ".join(ms["name"] for ms in mirror_sets if ms["short"] == "ICSP")
            + ") - migrate with `oc adm migrate icsp` to ImageDigestMirrorSet/ImageTagMirrorSet.")})

    return {
        "mirror_configured": mirror_configured,
        "mirror_sets": mirror_sets,
        "mirror_hosts": mirror_hosts,
        "pull_policy": pull_policy,
        "catalog_pull_paths": catalog_pull_paths,
        "disable_all_default_sources": disable_all,
        "default_sources": default_rows,
        "operators": operators,
        "catalogs": catalogs,
        "unresolved_operators": unresolved_operators,
        "notes": notes,
        "findings": findings,
    }


# ----------------------------------------------------------------------------
# 14. Markdown table cell escaping
# ----------------------------------------------------------------------------
def md_cell(value: Any) -> str:
    """Escape a value for safe use inside a GFM/CommonMark pipe-table cell:
    escape literal pipes (which would otherwise split the cell) and collapse
    newlines so multi-line messages don't break the row."""
    text = str(value)
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


class FilterModule(object):
    def filters(self):
        return {
            "etcd_health_report": etcd_health_report,
            "node_mcp_matrix": node_mcp_matrix,
            "co_report": co_report,
            "mcp_report": mcp_report,
            "machineset_report": machineset_report,
            "vmi_migration_report": vmi_migration_report,
            "crd_scan_targets": crd_scan_targets,
            "finalizer_stuck_report": finalizer_stuck_report,
            "deprecated_api_report": deprecated_api_report,
            "acm_hub_report": acm_hub_report,
            "acm_managed_cluster_report": acm_managed_cluster_report,
            "acm_resolve_cascade_targets": acm_resolve_cascade_targets,
            "resolve_upgrade_channel": resolve_upgrade_channel,
            "cincinnati_shortest_path": cincinnati_shortest_path,
            "cephcluster_report": cephcluster_report,
            "ceph_status_report": ceph_status_report,
            "cluster_operators_snapshot": cluster_operators_snapshot,
            "catalog_render_targets": catalog_render_targets,
            "opm_source_path": opm_source_path,
            "catalog_render_filename": catalog_render_filename,
            "opm_render_filter": opm_render_filter,
            "catalog_mirror_report": catalog_mirror_report,
            "md_cell": md_cell,
        }
