#!/usr/bin/env python3
"""Render the three report templates with synthetic data (no live cluster
needed) so template typos/undefined-var bugs surface before the playbook
ever runs against a real cluster. Writes previews to tests/preview_out/.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "filter_plugins"))
sys.path.insert(0, os.path.dirname(__file__))

import jinja2  # noqa: E402
import ocp_health_filters as f  # noqa: E402
import fixtures as fx  # noqa: E402

HERE = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(HERE, "..", "templates")
OUT_DIR = os.path.join(HERE, "preview_out")
os.makedirs(OUT_DIR, exist_ok=True)

node_mcp_matrix_report = f.node_mcp_matrix(fx.NODES, fx.MACHINECONFIGPOOLS)
co_report_data = f.co_report(fx.CLUSTEROPERATORS)
mcp_report_data = f.mcp_report(fx.MACHINECONFIGPOOLS)
machineset_report_data = f.machineset_report(fx.MACHINESETS, fx.MACHINES)
deprecated_api_data = f.deprecated_api_report(fx.APIREQUESTCOUNTS, [], 1, "1.29")
cnv_component_report = f.co_report(fx.HYPERCONVERGED + fx.KUBEVIRT)
vmi_migration_report_data = f.vmi_migration_report(fx.VMIS)
finalizer_stuck_data = f.finalizer_stuck_report(
    {"Namespace": fx.NAMESPACES, "PersistentVolume": fx.PERSISTENTVOLUMES, "PersistentVolumeClaim": fx.PERSISTENTVOLUMECLAIMS},
    fx.CRD_SCAN_RESULTS,
    fx.NOW_ISO,
    600,
)
# Mixed-severity scenario (slow/down endpoint, db near quota, active alarm) so the
# preview exercises every severity color in all three etcd tables at once.
etcd_health_data = f.etcd_health_report(
    fx.ETCD_PODS, fx.ETCD_HEALTH_SLOW_AND_DOWN, fx.ETCD_STATUS_DB_NEAR_QUOTA, fx.ETCD_ALARM_NOSPACE,
    8589934592, 0.8, 0.95, 50, 300,
)
odf_component_report = f.co_report(fx.ODF_STORAGECLUSTER_DEGRADED)
odf_cephcluster_report = f.cephcluster_report(fx.CEPHCLUSTER_WARN)
ceph_status_report_data = f.ceph_status_report(fx.CEPH_STATUS_JSON_WARN_OSD_DOWN)
acm_mch_report = f.acm_hub_report(fx.MCH_RUNNING)
acm_managed_clusters_report = f.acm_managed_cluster_report(fx.MANAGED_CLUSTERS)
acm_cascade_targets = f.acm_resolve_cascade_targets(
    fx.MANAGED_CLUSTERS, acm_managed_clusters_report, fx.ACM_HIVE_SECRET_RESULTS, fx.ACM_MSA_SECRET_RESULTS,
    ["local-cluster"], True, False, "",
)
# Mixed-severity cascade results: one clean pass, one with real CRITICAL
# findings, one whose child run never produced a status file - exercises
# every branch the ACM section's templates render.
acm_cascade_results = [
    {"cluster_name": "spoke-hive", "overall_status": "OK", "critical_count": 0, "warning_count": 1, "info_count": 2,
     "report_basename": "ocp-preupgrade-health-spoke-hive-20260824T000000Z"},
    {"cluster_name": "spoke-msa", "overall_status": "CRITICAL", "critical_count": 2, "warning_count": 3, "info_count": 1,
     "report_basename": "ocp-preupgrade-health-spoke-msa-20260824T000000Z"},
    {"cluster_name": "spoke-flaky", "overall_status": "UNKNOWN", "critical_count": 0, "warning_count": 0, "info_count": 0,
     "report_basename": "ocp-preupgrade-health-spoke-flaky-20260824T000000Z",
     "note": "this cluster did not produce a status file - check the \"Run the full health check...\" task output above for the real error"},
]

findings = []
for row in node_mcp_matrix_report:
    if row["severity"] != "OK":
        findings.append({"section": f"Node/MachineConfig render ({row['node']})", "severity": row["severity"],
                          "summary": f"{row['node']} status={row['status']}"})
for row in co_report_data:
    if row["severity"] != "OK":
        findings.append({"section": f"ClusterOperator ({row['name']})", "severity": row["severity"],
                          "summary": f"{row['name']} degraded={row['degraded']}"})
for row in mcp_report_data:
    if row["severity"] != "OK":
        findings.append({"section": f"MachineConfigPool ({row['name']})", "severity": row["severity"],
                          "summary": f"{row['name']} unavailable={row['unavailable_machine_count']}"})
for row in machineset_report_data:
    if row["severity"] != "OK":
        findings.append({"section": f"MachineSet ({row['name']})", "severity": row["severity"],
                          "summary": f"{row['name']} problem machines={len(row['problem_machines'])}"})
for row in deprecated_api_data["cluster_summary"]:
    findings.append({"section": f"Deprecated API ({row['resource']})", "severity": row["severity"],
                      "summary": f"removed in {row['removedInRelease']}"})
findings.append({"section": "Portworx node labels", "severity": "WARNING",
                  "summary": "Nodes with px/service set to stop or disabled: worker-1"})
findings.append({"section": "Pipe-escaping check", "severity": "WARNING",
                  "summary": "message containing a | pipe character must not break the table"})
for row in vmi_migration_report_data:
    if row["severity"] in ("CRITICAL", "WARNING"):
        findings.append({"section": f"VM drain readiness ({row['namespace']}/{row['name']})", "severity": row["severity"],
                          "summary": "; ".join(row["reasons"])})
for row in finalizer_stuck_data["rows"]:
    if row["severity"] == "CRITICAL":
        findings.append({"section": f"Stuck finalizer ({row['kind']}/{row['name']})", "severity": "CRITICAL",
                          "summary": f"Terminating for {row['age_human']}"})
for row in etcd_health_data["pods"] + etcd_health_data["health"] + etcd_health_data["status"]:
    if row["severity"] != "OK":
        label = row.get("name") or row.get("endpoint")
        findings.append({"section": f"etcd ({label})", "severity": row["severity"], "summary": "see etcd section"})
for row in etcd_health_data["cluster_findings"]:
    findings.append({"section": "etcd cluster consistency", "severity": row["severity"], "summary": row["message"]})
for row in etcd_health_data["alarms"]:
    findings.append({"section": "etcd alarm", "severity": row["severity"], "summary": row["raw"]})
for row in acm_managed_clusters_report:
    if row["severity"] != "OK":
        findings.append({"section": f"ACM managed cluster ({row['name']})", "severity": row["severity"],
                          "summary": f"available={row['available_status']}"})
for row in acm_cascade_results:
    if row["overall_status"] == "CRITICAL":
        findings.append({"section": f"ACM cascade result ({row['cluster_name']})", "severity": "CRITICAL",
                          "summary": f"{row['critical_count']} CRITICAL finding(s)"})
for row in odf_cephcluster_report:
    if row["severity"] != "OK":
        findings.append({"section": f"ODF CephCluster ({row['name']})", "severity": row["severity"],
                          "summary": f"phase={row['phase']} ceph_health={row['ceph_health']}"})
for chk in ceph_status_report_data["checks"]:
    if chk["severity"] != "OK":
        findings.append({"section": f"Ceph health check ({chk['name']})", "severity": chk["severity"],
                          "summary": chk["message"]})

context = dict(
    cluster_id=fx.CLUSTERVERSION["spec"]["clusterID"],
    cluster_id_short=fx.CLUSTERVERSION["spec"]["clusterID"][:8],
    cluster_name="ocp5-preprod-a1b2c",
    cluster_api_url="https://api.ocp5.example.com:6443",
    cluster_console_url="https://console-openshift-console.apps.ocp5.example.com",
    current_version=fx.CLUSTERVERSION["status"]["desired"]["version"],
    current_channel=fx.CLUSTERVERSION["spec"]["channel"],
    upgrade_target_version="4.17.14",
    report_generated_at="2026-08-23T20:00:00Z",
    target_is_recommended=True,
    target_is_conditional=False,
    # Upgrade channel/path resolution (EUS-aware) - exercises the "found a
    # hop-by-hop path, not overridden" branch, the richest one. The other
    # branches (no path found, unparseable channel, blank channel) are plain
    # Jinja `{% if %}` alternatives with no Ansible-only filters involved, so
    # they don't need separate coverage here - validated via real
    # ansible-playbook throwaways instead (see the project doc).
    upgrade_channel="eus",
    upgrade_channel_resolution={
        "channel": "eus-4.18", "prefix": "eus", "target_major": 4, "target_minor": 18,
        "is_eus_jump": True, "auto_derived": True,
        "reasoning": "current 4.16 is already EUS (even) - next EUS target is +2 -> 4.18", "notes": [],
    },
    upgrade_path_result={"found": True, "hops": ["4.16.20", "4.16.22", "4.17.0", "4.17.14", "4.18.0", "4.18.14"], "target_version": "4.18.14", "reason": "5 hop(s)"},
    _upgrade_target_was_explicit=False,
    cv_available="True", cv_progressing="False", cv_degraded="False", cv_retrieved_updates="True",
    cv_last_history_state="Completed",
    cv_history=fx.CLUSTERVERSION["status"]["history"],
    node_mcp_matrix_report=node_mcp_matrix_report,
    co_report_data=co_report_data,
    mcp_report_data=mcp_report_data,
    machineset_report_data=machineset_report_data,
    deprecated_api_data=deprecated_api_data,
    portworx_enabled=True,
    px_storagecluster_list=[{"metadata": {"name": "px-cluster"}}],
    px_sc_name="px-cluster",
    px_sc_phase="Online",
    px_image="portworx/oci-monitor:3.1.2",
    px_storagenodes=[{"metadata": {"name": "worker-0"}, "status": {"phase": "Online"}}],
    pxctl_status_output="Status: PX is operational\nLicense: Trial\nNodes: 3 total, 3 online",
    pxctl_license_output="License ID: XXXX\nExpires: 2027-01-01",
    portworx_manual_checklist=[
        "Confirm the installed Portworx version supports 4.17 in the compatibility matrix.",
        "Review pxctl status for KVDB health and in-progress rebalance/resync operations.",
    ],
    cnv_enabled=True,
    cnv_namespace="openshift-cnv",
    cnv_hco_list=fx.HYPERCONVERGED,
    cnv_component_report=cnv_component_report,
    kv_outdated_count=1,
    kv_update_methods=["LiveMigrate"],
    vmi_migration_report_data=vmi_migration_report_data,
    cnv_manual_checklist=[
        "Confirm the installed HCO version supports upgrading to 4.17 in Red Hat's OpenShift Virtualization support matrix.",
        "hostpath-vm/legacy-app is backed by hostpath-provisioner storage and cannot be live-migrated.",
    ],
    finalizer_scan_include_crs=True,
    finalizer_scan_stuck_after_seconds=600,
    finalizer_stuck_data=finalizer_stuck_data,
    etcd_health_data=etcd_health_data,
    etcdctl_health_raw='[{"Endpoint":"https://10.0.0.1:2379","Health":true,"Took":"12.3ms"},{"Endpoint":"https://10.0.0.2:2379","Health":false,"Took":"","Error":"context deadline exceeded"}]',
    etcdctl_status_raw='[{"Endpoint":"https://10.0.0.1:2379","Status":{"version":"3.5.9","dbSize":8200000000,"dbSizeInUse":8100000000,"leader":1,"raftTerm":5}}]',
    etcd_manual_checklist=[
        "For a real disk-hardware validation, Red Hat documents an fio-based write/fdatasync benchmark against the etcd data disk - run it deliberately in a maintenance window, not automatically.",
        "Deliberately not run here: `etcdctl check perf` - it's a write-load generator with known false-FAIL reports under some load profiles.",
        "If dbSize is flagged, consider `etcdctl defrag` (per-member, one at a time, during a maintenance window) before upgrading.",
        "Cross-check against the etcdHighFsyncDurations / etcdHighCommitDurations / etcdMembersDown alerts in OpenShift's own monitoring for a longer time-window view.",
    ],
    acm_enabled=True,
    acm_crds_present=True,
    acm_mch_report=acm_mch_report,
    acm_managed_clusters_report=acm_managed_clusters_report,
    acm_cascade_enabled=True,
    acm_cascade_targets=acm_cascade_targets,
    acm_cascade_results=acm_cascade_results,
    acm_cascade_report_dir="/path/to/reports/acm-managed-clusters",
    odf_enabled=True,
    odf_namespace="openshift-storage",
    odf_storagecluster_list=fx.ODF_STORAGECLUSTER_DEGRADED,
    odf_sc_name="ocs-storagecluster",
    odf_component_report=odf_component_report,
    odf_cephcluster_list=fx.CEPHCLUSTER_WARN,
    odf_cephcluster_report=odf_cephcluster_report,
    odf_osd_pods=fx.ODF_OSD_PODS_ONE_DOWN,
    ceph_status_report_data=ceph_status_report_data,
    ceph_status_output='{"health":{"status":"HEALTH_WARN"}, ...}',
    ceph_osd_status_output="ID  HOST      USED  AVAIL  WR OPS  WR DATA  RD OPS  RD DATA  STATE\n 0  worker-0  10G   90G    0       0        0       0        exists,up\n 1  worker-1  10G   90G    0       0        0       0        exists\n 2  worker-2  10G   90G    0       0        0       0        exists,up",
    ceph_df_output="--- RAW STORAGE ---\nCLASS   SIZE   AVAIL   USED  RAW USED  %RAW USED\nssd    120GiB  90GiB   30GiB   30GiB      25.00",
    ceph_health_detail_output="HEALTH_WARN 1 osds down; Degraded data redundancy: 320/15000 objects degraded (2.1%), 4 pgs degraded\n[WRN] OSD_DOWN: 1 osds down\n    osd.1 (root=default,host=worker-1) is down",
    odf_manual_checklist=[
        "Confirm the installed ODF/OCS Operator version supports 4.17 in Red Hat's ODF-OCP interoperability matrix.",
        "Review the captured ceph df output for pool/cluster capacity headroom before upgrading.",
    ],
)

cluster_operators_snapshot_data = f.cluster_operators_snapshot(
    fx.COSNAP_SUBSCRIPTIONS, fx.COSNAP_CSVS, fx.COSNAP_CATALOGSOURCES, "4.18.14", "4.20.32", "eus-4.20",
)

critical_findings = [x for x in findings if x["severity"] == "CRITICAL"]
warning_findings = [x for x in findings if x["severity"] == "WARNING"]
info_findings = [x for x in findings if x["severity"] == "INFO"]
context.update(
    critical_findings=critical_findings,
    warning_findings=warning_findings,
    info_findings=info_findings,
    overall_status="CRITICAL" if critical_findings else ("WARNING" if warning_findings else "OK"),
    cluster_operators_snapshot_data=cluster_operators_snapshot_data,
)

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
    undefined=jinja2.StrictUndefined,  # fail loudly on any undefined var, exactly what we want to catch
)
# Ansible's template module auto-registers filter_plugins/*.py; plain jinja2 doesn't, so wire it up here.
env.filters["md_cell"] = f.md_cell

errors = []
for tpl_name, out_name in [
    ("report.md.j2", "preview.md"),
    ("report.html.j2", "preview.html"),
    ("report_summary.html.j2", "preview.summary.html"),
    ("cluster_operators_installed.md.j2", "preview.cluster_operators_installed.md"),
]:
    try:
        rendered = env.get_template(tpl_name).render(**context)
        with open(os.path.join(OUT_DIR, out_name), "w") as fh:
            fh.write(rendered)
        print(f"[OK] rendered {tpl_name} -> tests/preview_out/{out_name} ({len(rendered)} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {tpl_name}: {exc}")
        errors.append(tpl_name)

if errors:
    sys.exit(1)
print("All templates rendered cleanly.")
