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


def finalizer_stuck_report(
    fixed_resources: Dict[str, List[dict]],
    crd_scan_results: List[dict],
    now_iso: str,
    stuck_after_seconds: int = 600,
) -> Dict[str, Any]:
    """Flag any object with both a deletionTimestamp AND finalizers set - i.e.
    deletion was requested but something (a controller/webhook that no longer
    runs or is stuck) hasn't removed its finalizer yet, so the object is stuck
    Terminating. `fixed_resources` is {kind: [objects]} for the small set of
    built-in kinds we always check (Namespace/PersistentVolume/
    PersistentVolumeClaim); `crd_scan_results` is the raw registered result of
    looping kubernetes.core.k8s_info over crd_scan_targets() (each entry has
    `.item` = the target dict and `.resources`/`.failed` from the module)."""
    now = _parse_ts(now_iso)
    rows: List[dict] = []

    def process(kind: str, crd_name: str, items: List[dict]):
        for obj in items or []:
            deletion_ts = _get(obj, "metadata.deletionTimestamp")
            finalizers = _get(obj, "metadata.finalizers", []) or []
            if not deletion_ts or not finalizers:
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
        for pat in exclude_namespaces:
            if pat.endswith("*") and ns.startswith(pat[:-1]):
                return True
            if pat == ns:
                return True
        return False

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
# 12. Markdown table cell escaping
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
            "md_cell": md_cell,
        }
