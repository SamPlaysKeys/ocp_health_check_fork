#!/usr/bin/env python3
"""Lightweight assertion-based tests for filter_plugins/ocp_health_filters.py.

Run with: python3 tests/test_filters.py
No external test framework required, so this also works in minimal CI images.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "filter_plugins"))
sys.path.insert(0, os.path.dirname(__file__))

import json  # noqa: E402

import ocp_health_filters as f  # noqa: E402
import fixtures as fx  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---- etcd_health_report -------------------------------------------------------
etcd_ok = f.etcd_health_report(fx.ETCD_PODS, fx.ETCD_HEALTH_OK, fx.ETCD_STATUS_OK, fx.ETCD_ALARM_NONE)
check("etcd pod report flags the not-ready etcd container as CRITICAL", any(p["severity"] == "CRITICAL" for p in etcd_ok["pods"]))
check("all 3 etcd endpoints healthy and fast -> OK", all(h["severity"] == "OK" for h in etcd_ok["health"]))
check("etcd status rows all OK when db size is nowhere near quota", all(s["severity"] == "OK" for s in etcd_ok["status"]))
check("no cluster-level findings when leader/term agree", len(etcd_ok["cluster_findings"]) == 0)
check("no alarms parsed from empty alarm output", len(etcd_ok["alarms"]) == 0)

etcd_bad = f.etcd_health_report(fx.ETCD_PODS, fx.ETCD_HEALTH_SLOW_AND_DOWN, fx.ETCD_STATUS_SPLIT_LEADER, fx.ETCD_ALARM_NOSPACE)
etcd_health_by_ep = {h["endpoint"]: h for h in etcd_bad["health"]}
check("120ms round trip is flagged WARNING (>=50ms default threshold)", etcd_health_by_ep["https://10.0.0.2:2379"]["severity"] == "WARNING")
check("an unhealthy endpoint with an error is CRITICAL", etcd_health_by_ep["https://10.0.0.3:2379"]["severity"] == "CRITICAL")
check("took_ms is parsed as a number, not the raw string", etcd_health_by_ep["https://10.0.0.1:2379"]["took_ms"] == 12.34)
check("disagreeing leaders across members produces a CRITICAL cluster finding", any(cf["severity"] == "CRITICAL" and "leader" in cf["message"] for cf in etcd_bad["cluster_findings"]))
check("a NOSPACE alarm line is parsed and flagged CRITICAL", len(etcd_bad["alarms"]) == 1 and etcd_bad["alarms"][0]["severity"] == "CRITICAL")

etcd_quota = f.etcd_health_report(fx.ETCD_PODS, fx.ETCD_HEALTH_OK, fx.ETCD_STATUS_DB_NEAR_QUOTA, fx.ETCD_ALARM_NONE)
check("db size at ~97% of the 8GiB default quota is CRITICAL", etcd_quota["status"][0]["severity"] == "CRITICAL")
check("db_quota_pct is computed correctly (~95.5%)", 95 <= etcd_quota["status"][0]["db_quota_pct"] <= 96)

# health_items/status_items must also work as RAW etcdctl "-w json" strings,
# not just already-parsed lists - this is what the playbook actually hands in
# (see tasks/15_etcd_health.yml). Regression coverage for the bug where
# Ansible/Jinja2 had already turned a JSON-looking set_fact string into a
# native list, and a second `from_json` pass on that blew up with "the JSON
# object must be str, bytes or bytearray, not list".
etcd_from_raw_json = f.etcd_health_report(
    fx.ETCD_PODS, json.dumps(fx.ETCD_HEALTH_OK), json.dumps(fx.ETCD_STATUS_OK), fx.ETCD_ALARM_NONE
)
check("etcd_health_report parses a raw JSON *string* for health_items the same as a pre-parsed list",
      all(h["severity"] == "OK" for h in etcd_from_raw_json["health"]) and len(etcd_from_raw_json["health"]) == 3)
check("etcd_health_report parses a raw JSON *string* for status_items the same as a pre-parsed list",
      all(s["severity"] == "OK" for s in etcd_from_raw_json["status"]) and len(etcd_from_raw_json["status"]) == 3)

etcd_already_list = f.etcd_health_report(fx.ETCD_PODS, fx.ETCD_HEALTH_OK, fx.ETCD_STATUS_OK, fx.ETCD_ALARM_NONE)
check("a pre-parsed list still works unchanged (no double-parsing regression)",
      etcd_already_list["health"] == etcd_from_raw_json["health"])

etcd_bad_input = f.etcd_health_report(
    fx.ETCD_PODS,
    "(etcdctl endpoint health exec failed or was skipped - review manually)",
    "not json at all {{{",
    "",
)
check("an exec-failed placeholder string for health_items degrades to an empty list, not a crash",
      etcd_bad_input["health"] == [])
check("genuinely malformed JSON for status_items degrades to an empty list, not a crash",
      etcd_bad_input["status"] == [])

# ---- node_mcp_matrix --------------------------------------------------------
matrix = f.node_mcp_matrix(fx.NODES, fx.MACHINECONFIGPOOLS)
by_name = {r["node"]: r for r in matrix}
check("node_mcp_matrix returns 3 rows", len(matrix) == 3)
check("master-0 is OK", by_name["master-0"]["status"] == "OK")
check("worker-0 is UPDATING (current != desired)", by_name["worker-0"]["status"] == "UPDATING")
check("worker-1 is OK (in sync)", by_name["worker-1"]["status"] == "OK")
check("worker-0 matched to worker pool", by_name["worker-0"]["pool"] == "worker")

# ---- co_report ---------------------------------------------------------------
co = f.co_report(fx.CLUSTEROPERATORS)
co_by_name = {r["name"]: r for r in co}
check("co_report returns 2 rows", len(co) == 2)
check("authentication operator is OK", co_by_name["authentication"]["severity"] == "OK")
check("storage operator is CRITICAL (Available=False, Degraded=True)", co_by_name["storage"]["severity"] == "CRITICAL")
check("storage operator captured its message", any("waiting for deployment" in m for m in co_by_name["storage"]["messages"]))

# ---- mcp_report ----------------------------------------------------------
mcp = f.mcp_report(fx.MACHINECONFIGPOOLS)
mcp_by_name = {r["name"]: r for r in mcp}
check("mcp_report returns 2 rows", len(mcp) == 2)
check("master pool OK", mcp_by_name["master"]["severity"] == "OK")
check("worker pool WARNING (updating, unavailable=1)", mcp_by_name["worker"]["severity"] == "WARNING")

# ---- machineset_report -----------------------------------------------------
ms = f.machineset_report(fx.MACHINESETS, fx.MACHINES)
ms_by_name = {r["name"]: r for r in ms}
check("machineset_report returns 2 rows", len(ms) == 2)
check("1a machineset OK (2/2 running, matches status)", ms_by_name["cluster-worker-us-east-1a"]["severity"] == "OK")
check(
    "1b machineset CRITICAL (machine stuck Provisioning, status says ready=0)",
    ms_by_name["cluster-worker-us-east-1b"]["severity"] == "CRITICAL",
)
check(
    "1b machineset flags the provisioning machine as a problem machine",
    len(ms_by_name["cluster-worker-us-east-1b"]["problem_machines"]) == 1,
)

# ---- vmi_migration_report -----------------------------------------------
vmi = f.vmi_migration_report(fx.VMIS)
vmi_by_name = {r["name"]: r for r in vmi}
check("vmi_migration_report returns 4 rows", len(vmi) == 4)
check("web-vm-1 (plain disk, migratable, LiveMigrate) is OK", vmi_by_name["web-vm-1"]["severity"] == "OK")
check(
    "installer-vm (CD-ROM + LiveMigrate, still reported migratable) is WARNING",
    vmi_by_name["installer-vm"]["severity"] == "WARNING",
)
check(
    "installer-vm reason mentions the CD-ROM disk name",
    any("cdrom-iso" in r for r in vmi_by_name["installer-vm"]["reasons"]),
)
check(
    "hostpath-vm (LiveMigrate + not migratable) is CRITICAL - this is exactly what stalls drain",
    vmi_by_name["hostpath-vm"]["severity"] == "CRITICAL",
)
check(
    "shutdown-ok-vm (evictionStrategy=None + not migratable) is only WARNING, not CRITICAL",
    vmi_by_name["shutdown-ok-vm"]["severity"] == "WARNING",
)

# ---- crd_scan_targets --------------------------------------------------------
targets = f.crd_scan_targets(fx.CRDS, [])
check("crd_scan_targets keeps only the Established CRD", len(targets) == 1 and targets[0]["kind"] == "Widget")
targets_excluded = f.crd_scan_targets(fx.CRDS, ["widgets.example.com"])
check("crd_scan_targets honors exclude_names", len(targets_excluded) == 0)

# ---- finalizer_stuck_report ---------------------------------------------------
fin = f.finalizer_stuck_report(
    {"Namespace": fx.NAMESPACES, "PersistentVolume": fx.PERSISTENTVOLUMES, "PersistentVolumeClaim": fx.PERSISTENTVOLUMECLAIMS},
    fx.CRD_SCAN_RESULTS,
    fx.NOW_ISO,
    stuck_after_seconds=600,
)
fin_by_name = {r["name"]: r for r in fin["rows"]}
check("finalizer_stuck_report ignores objects with no deletionTimestamp", "pv-ok" not in fin_by_name and "widget-fine" not in fin_by_name)
check("stuck-ns (40min old) is CRITICAL", fin_by_name["stuck-ns"]["severity"] == "CRITICAL")
check("just-deleting-ns (10s old) is only INFO, not a false positive", fin_by_name["just-deleting-ns"]["severity"] == "INFO")
check("stuck-pvc (90min old) is CRITICAL", fin_by_name["stuck-pvc"]["severity"] == "CRITICAL")
check("widget-stuck (30min old, from the dynamic CRD scan) is CRITICAL", fin_by_name["widget-stuck"]["severity"] == "CRITICAL")
check("widget-stuck carries its owning CRD name", fin_by_name["widget-stuck"]["crd"] == "widgets.example.com")
check("the failed CRD listing is counted, not silently dropped", fin["crds_failed"] == 1)
check("age_human is a short human string, not raw seconds", fin_by_name["stuck-ns"]["age_human"] == "40m0s")

# ---- deprecated_api_report --------------------------------------------------
dep = f.deprecated_api_report(fx.APIREQUESTCOUNTS, [], 1, "1.29")  # target = OCP 4.16 -> k8s 1.29
check("cluster_summary excludes resources with no removedInRelease", len(dep["cluster_summary"]) == 2)
ns_by_name = {n["namespace"]: n for n in dep["namespace_matrix"]}
check("legacy-app namespace recovered from service account username", "legacy-app" in ns_by_name)
check("openshift-monitoring namespace recovered from service account username", "openshift-monitoring" in ns_by_name)
check(
    "cronjobs flagged CRITICAL for legacy-app (removed 1.25 <= target 1.29)",
    ns_by_name["legacy-app"]["resources"][0]["severity"] == "CRITICAL",
)
check(
    "namespace_matrix sorted with most severe namespace first",
    dep["namespace_matrix"][0]["namespace"] in ("legacy-app", "openshift-monitoring"),
)

dep_no_target = f.deprecated_api_report(fx.APIREQUESTCOUNTS, [], 1, "")
check(
    "without a target version, deprecated APIs are WARNING not CRITICAL",
    all(r["severity"] == "WARNING" for ns in dep_no_target["namespace_matrix"] for r in ns["resources"]),
)

dep_excluded = f.deprecated_api_report(fx.APIREQUESTCOUNTS, ["openshift-*"], 1, "1.29")
check(
    "api_report_exclude_namespaces filters out matching namespaces",
    "openshift-monitoring" not in {n["namespace"] for n in dep_excluded["namespace_matrix"]},
)

# ---- acm_hub_report ----------------------------------------------------------
mch_ok = f.acm_hub_report(fx.MCH_RUNNING)
check("acm_hub_report returns 1 row", len(mch_ok) == 1)
check("MultiClusterHub phase=Running -> OK", mch_ok[0]["severity"] == "OK")

mch_err = f.acm_hub_report(fx.MCH_ERROR)
check("MultiClusterHub phase=Error -> CRITICAL", mch_err[0]["severity"] == "CRITICAL")
check("MultiClusterHub CRITICAL row still carries the Complete condition's message", "grc component" in mch_err[0]["message"])

check("acm_hub_report on an empty list (no MCH found) returns no rows, doesn't crash", f.acm_hub_report([]) == [])

# ---- acm_managed_cluster_report -----------------------------------------------
mc_rows = f.acm_managed_cluster_report(fx.MANAGED_CLUSTERS)
mc_by_name = {r["name"]: r for r in mc_rows}
check("acm_managed_cluster_report returns 4 rows", len(mc_rows) == 4)
check("local-cluster (available/accepted/joined all True) is OK", mc_by_name["local-cluster"]["severity"] == "OK")
check("spoke-hive is OK", mc_by_name["spoke-hive"]["severity"] == "OK")
check("spoke-down (Available=False) is CRITICAL", mc_by_name["spoke-down"]["severity"] == "CRITICAL")
check("spoke-down carries available=False (real bool, not the string)", mc_by_name["spoke-down"]["available"] is False)
check("openshift_version label is surfaced", mc_by_name["spoke-msa"]["openshift_version"] == "4.15.30")
check("rows are sorted most-severe first", mc_rows[0]["severity"] == "CRITICAL")

# ---- acm_resolve_cascade_targets ----------------------------------------------
targets = f.acm_resolve_cascade_targets(
    fx.MANAGED_CLUSTERS, mc_rows, fx.ACM_HIVE_SECRET_RESULTS, fx.ACM_MSA_SECRET_RESULTS,
    exclude_names=[], prefer_hive=True, use_cluster_proxy=False, cluster_proxy_base_url="",
)
targets_by_name = {t["name"]: t for t in targets}
check("acm_resolve_cascade_targets returns one row per managed cluster (4)", len(targets) == 4)
check("spoke-hive resolves via the Hive admin-kubeconfig secret", targets_by_name["spoke-hive"]["auth_method"] == "kubeconfig")
check("spoke-hive's kubeconfig content is base64-decoded, not left encoded", "apiVersion: v1" in targets_by_name["spoke-hive"]["kubeconfig_content"])
check("spoke-msa resolves via the ManagedServiceAccount token (no Hive secret found for it)", targets_by_name["spoke-msa"]["auth_method"] == "token")
check("spoke-msa's token is base64-decoded", targets_by_name["spoke-msa"]["token"] == "fake-msa-token-xyz")
check("spoke-msa's host comes from spec.managedClusterClientConfigs (direct URL, cluster-proxy off)", targets_by_name["spoke-msa"]["host"] == "https://api.spoke-msa.example.com:6443")
check("local-cluster has neither credential source -> not checked", targets_by_name["local-cluster"]["checked"] is False)
check("local-cluster's reason explains why (no credentials)", "no credentials" in targets_by_name["local-cluster"]["reason"])
check("spoke-down (not Available) is not checked regardless of credentials", targets_by_name["spoke-down"]["checked"] is False)
check("spoke-down's reason cites Available status, not credentials", "not Available" in targets_by_name["spoke-down"]["reason"])

targets_excl = f.acm_resolve_cascade_targets(
    fx.MANAGED_CLUSTERS, mc_rows, fx.ACM_HIVE_SECRET_RESULTS, fx.ACM_MSA_SECRET_RESULTS,
    exclude_names=["local-cluster"], prefer_hive=True, use_cluster_proxy=False, cluster_proxy_base_url="",
)
check("acm_exclude_clusters excludes the named cluster with its own reason",
      {t["name"]: t for t in targets_excl}["local-cluster"]["reason"] == "excluded via acm_exclude_clusters")

targets_proxy = f.acm_resolve_cascade_targets(
    fx.MANAGED_CLUSTERS, mc_rows, fx.ACM_HIVE_SECRET_RESULTS, fx.ACM_MSA_SECRET_RESULTS,
    exclude_names=[], prefer_hive=True, use_cluster_proxy=True, cluster_proxy_base_url="https://cluster-proxy-addon-user.multicluster-engine.svc:9092",
)
check("cluster-proxy mode builds a proxied host URL instead of the direct API URL",
      {t["name"]: t for t in targets_proxy}["spoke-msa"]["host"] == "https://cluster-proxy-addon-user.multicluster-engine.svc:9092/spoke-msa")

targets_no_creds = f.acm_resolve_cascade_targets([], [], [], [])
check("acm_resolve_cascade_targets on no managed clusters returns an empty list, doesn't crash", targets_no_creds == [])

# ---- resolve_upgrade_channel ---------------------------------------------------
r = f.resolve_upgrade_channel("4.18.14", "")
check("resolve_upgrade_channel with a blank channel returns {} (nothing to resolve)", r == {})

r = f.resolve_upgrade_channel("4.18.14", "eus")
check("bare 'eus' with EVEN current minor (4.18) jumps +2 -> 4.20", r["target_minor"] == 20 and r["target_major"] == 4)
check("bare 'eus' resolves to channel 'eus-4.20'", r["channel"] == "eus-4.20")
check("bare 'eus' is flagged as an EUS jump", r["is_eus_jump"] is True)
check("bare 'eus' is marked auto_derived", r["auto_derived"] is True)

r = f.resolve_upgrade_channel("4.17.20", "eus")
check("bare 'eus' with ODD current minor (4.17) jumps +1 -> 4.18 (not +2)", r["target_minor"] == 18)
check("bare 'eus' from odd minor resolves to channel 'eus-4.18'", r["channel"] == "eus-4.18")

r = f.resolve_upgrade_channel("4.18.14", "stable")
check("bare 'stable' always targets current + 1 minor regardless of parity", r["target_minor"] == 19 and r["channel"] == "stable-4.19")
check("bare 'stable' is NOT flagged as an EUS jump", r["is_eus_jump"] is False)

r = f.resolve_upgrade_channel("4.18.14", "eus-4.20")
check("fully-qualified 'eus-4.20' is used as-is, not re-derived", r["channel"] == "eus-4.20" and r["auto_derived"] is False)
check("fully-qualified channel carries no warning notes when the target is sane", r["notes"] == [])

r = f.resolve_upgrade_channel("4.18.14", "eus-4.19")
check("fully-qualified 'eus-4.19' (odd target) is accepted but flagged with a warning note",
      r["target_minor"] == 19 and any("even minor" in n for n in r["notes"]))

r = f.resolve_upgrade_channel("4.20.5", "eus-4.18")
check("a target not newer than current is accepted but flagged with a warning note",
      any("not newer than current" in n for n in r["notes"]))

r = f.resolve_upgrade_channel("not-a-version", "eus")
check("an unparseable current_version returns an 'error' key, doesn't crash", "error" in r)

r = f.resolve_upgrade_channel("4.18.14", "not valid!!")
check("an unparseable upgrade_channel returns an 'error' key, doesn't crash", "error" in r)

# ---- cincinnati_shortest_path ---------------------------------------------------
p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_EUS_4_20, "4.18.14", 4, 20)
check("EUS path is found across the real graph (4.18 -> 4.19 -> 4.20)", p["found"] is True)
check("EUS path passes through a 4.19.x node - it does NOT jump directly 4.18 -> 4.20",
      any(h.startswith("4.19.") for h in p["hops"]))
check("EUS path starts at current_version", p["hops"][0] == "4.18.14")
check("EUS path resolves to the highest 4.20.z node (4.20.1, not 4.20.0)", p["target_version"] == "4.20.1")
check("EUS path's last hop matches target_version", p["hops"][-1] == "4.20.1")

p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_MISSING_CURRENT, "4.18.14", 4, 20)
check("current version absent from the graph -> not found, with a clear reason",
      p["found"] is False and "not present in this channel" in p["reason"])

p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_NO_TARGET_YET, "4.18.14", 4, 20)
check("target minor has no z-stream in the graph yet -> not found, with a clear reason",
      p["found"] is False and "no 4.20.z release" in p["reason"])

p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_DISCONNECTED, "4.18.14", 4, 20)
check("current and target both present but disconnected -> not found (no path), not a crash",
      p["found"] is False and "no upgrade path" in p["reason"])

p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_STABLE_4_19, "4.18.14", 4, 19)
check("plain next-minor (stable) path is found", p["found"] is True and p["hops"][-1] == "4.19.5")

p = f.cincinnati_shortest_path({}, "4.18.14", 4, 20)
check("an empty/missing graph response -> not found, doesn't crash", p["found"] is False)

p = f.cincinnati_shortest_path(fx.CINCINNATI_GRAPH_EUS_4_20, "4.20.1", 4, 20)
check("already at the target version -> found, single-element path, no BFS needed",
      p["found"] is True and p["hops"] == ["4.20.1"] and p["target_version"] == "4.20.1")

# ---- cephcluster_report -----------------------------------------------------
cc_ready = f.cephcluster_report(fx.CEPHCLUSTER_READY)
check("cephcluster_report returns 1 row", len(cc_ready) == 1)
check("Ready phase + HEALTH_OK -> OK", cc_ready[0]["severity"] == "OK")

cc_warn = f.cephcluster_report(fx.CEPHCLUSTER_WARN)
check("Ready phase + HEALTH_WARN -> WARNING (not OK, not CRITICAL)", cc_warn[0]["severity"] == "WARNING")
check("CephCluster WARNING messages surface the ceph.details entry", any("1 osds down" in m for m in cc_warn[0]["messages"]))

cc_fail = f.cephcluster_report(fx.CEPHCLUSTER_FAILURE)
check("Failure phase -> CRITICAL regardless of ceph.health", cc_fail[0]["severity"] == "CRITICAL")
check("CephCluster CRITICAL messages include the top-level status.message", any("failed to configure" in m for m in cc_fail[0]["messages"]))

check("cephcluster_report on an empty list doesn't crash", f.cephcluster_report([]) == [])

# ---- ceph_status_report -------------------------------------------------------
cs_ok = f.ceph_status_report(fx.CEPH_STATUS_JSON_OK)
check("ceph_status_report marks a HEALTH_OK payload parsed=True", cs_ok["parsed"] is True)
check("HEALTH_OK -> overall_severity OK", cs_ok["overall_severity"] == "OK")
check("osdmap reflects all 3 OSDs up and in", cs_ok["osdmap"] == {"num_osds": 3, "num_up_osds": 3, "num_in_osds": 3, "num_remapped_pgs": 0, "severity": "OK"})
check("pgmap marks all-active+clean as OK", cs_ok["pgmap"]["severity"] == "OK")
check("pgmap computes pct_used correctly (~25%)", 24 <= cs_ok["pgmap"]["pct_used"] <= 26)
check("mon quorum count matches the quorum list length", cs_ok["mon"]["quorum_count"] == 3)
check("no health checks when HEALTH_OK", cs_ok["checks"] == [])

cs_warn = f.ceph_status_report(fx.CEPH_STATUS_JSON_WARN_OSD_DOWN)
check("HEALTH_WARN -> overall_severity WARNING", cs_warn["overall_severity"] == "WARNING")
check("osdmap flags CRITICAL when an OSD is down (1/3 up) even though ceph's own overall status is only WARN",
      cs_warn["osdmap"]["severity"] == "CRITICAL")
check("ceph_status_report surfaces both named health checks", len(cs_warn["checks"]) == 2)
cs_warn_checks_by_name = {c["name"]: c for c in cs_warn["checks"]}
check("OSD_DOWN check parsed with its summary message", cs_warn_checks_by_name["OSD_DOWN"]["message"] == "1 osds down")
check("checks are sorted most-severe first", cs_warn["checks"][0]["severity"] in ("CRITICAL", "WARNING"))
check("pgmap is WARNING when not all PGs are active+clean", cs_warn["pgmap"]["severity"] == "WARNING")

cs_err = f.ceph_status_report(fx.CEPH_STATUS_JSON_ERR)
check("HEALTH_ERR -> overall_severity CRITICAL", cs_err["overall_severity"] == "CRITICAL")
check("osdmap severity CRITICAL when OSDs are both down and out", cs_err["osdmap"]["severity"] == "CRITICAL")

cs_unparsed = f.ceph_status_report(fx.CEPH_STATUS_EXEC_FAILED_PLACEHOLDER)
check("an exec-failed placeholder string degrades to parsed=False, not a crash", cs_unparsed["parsed"] is False)
check("an unparsed report still has a sane (non-crashing) shape", cs_unparsed["osdmap"] == {} and cs_unparsed["checks"] == [])

cs_from_raw_json_string = f.ceph_status_report(json.dumps(fx.CEPH_STATUS_JSON_OK))
check("ceph_status_report parses a raw JSON *string* the same as an already-native dict",
      cs_from_raw_json_string["overall_status"] == cs_ok["overall_status"] and cs_from_raw_json_string["osdmap"] == cs_ok["osdmap"])

cs_empty = f.ceph_status_report({})
check("an empty dict input degrades to parsed=False, not a crash", cs_empty["parsed"] is False)

cs_malformed = f.ceph_status_report("not json at all {{{")
check("genuinely malformed JSON text degrades to parsed=False, not a crash", cs_malformed["parsed"] is False)

# ---- cluster_operators_snapshot -----------------------------------------------
check("_trim_channel_family collapses a bare 'eus' to 'EUS'", f._trim_channel_family("eus") == "EUS")
check("_trim_channel_family collapses a qualified 'eus-4.20' to 'EUS' regardless of minor", f._trim_channel_family("eus-4.20") == "EUS")
check("_trim_channel_family is case-insensitive", f._trim_channel_family("EUS-4.18") == "EUS")
check("_trim_channel_family leaves a non-EUS channel exactly as given", f._trim_channel_family("stable-4.19") == "stable-4.19")
check("_trim_channel_family leaves a blank channel as an empty string, not a crash", f._trim_channel_family("") == "")

snap = f.cluster_operators_snapshot(fx.COSNAP_SUBSCRIPTIONS, fx.COSNAP_CSVS, fx.COSNAP_CATALOGSOURCES, "4.18.14", "4.20.32", "eus-4.20")
check("cluster.current/target are carried through as given", snap["cluster"]["current"] == "4.18.14" and snap["cluster"]["target"] == "4.20.32")
check("cluster.channel is trimmed to EUS", snap["cluster"]["channel"] == "EUS")
by_name = {o["name"]: o for o in snap["operators"]}
check("cluster-logging is present with its CSV's real spec.version", by_name["cluster-logging"] == {"name": "cluster-logging", "channel": "stable-6.2", "version": "6.2.0", "catalog": "registry.redhat.io/redhat/redhat-operator-index:v4.20"})
check("community-thing's catalog resolves to the community CatalogSource image, not Red Hat's", by_name["community-thing"]["catalog"] == "registry.redhat.io/redhat/community-operator-index:v4.20")
check("multicluster-engine subscribed twice (same name/channel/version) is consolidated into ONE entry", len([o for o in snap["operators"] if o["name"] == "multicluster-engine"]) == 1)
check("a stuck Subscription with no installedCSV is skipped entirely", "stuck-op" not in by_name)
check("a CSV that exists but has no spec.version is skipped entirely", "no-version-csv-op" not in by_name)
check("exactly 3 operators survive (cluster-logging, multicluster-engine x1, community-thing)", len(snap["operators"]) == 3)
check("every operator row has exactly the 4 documented keys, nothing extra", all(set(o.keys()) == {"name", "channel", "version", "catalog"} for o in snap["operators"]))

snap_stable = f.cluster_operators_snapshot(fx.COSNAP_SUBSCRIPTIONS, fx.COSNAP_CSVS, fx.COSNAP_CATALOGSOURCES, "4.18.14", "4.19.5", "stable-4.19")
check("a non-EUS cluster.channel is kept as its full name, not trimmed", snap_stable["cluster"]["channel"] == "stable-4.19")

check("cluster_operators_snapshot on no subscriptions/csvs/catalogsources returns an empty operators list, doesn't crash",
      f.cluster_operators_snapshot([], [], [], "4.18.14", "", "") == {"cluster": {"current": "4.18.14", "target": "", "channel": ""}, "operators": []})

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
else:
    print("All checks passed.")
