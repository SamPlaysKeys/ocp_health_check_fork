# OpenShift Pre-Upgrade Health Check

This repository provides a strictly read-only Ansible playbook that validates an OpenShift Container Platform (OCP) cluster's readiness for an upgrade. It queries API resources and performs non-disruptive pod executions to assess cluster health, returning actionable reports without modifying cluster state.

All tasks are executed via `k8s_info` lookups or read-only `exec` commands into existing pods (e.g., `pxctl`, `ceph status`, `etcdctl`).

## Output Artifacts

Execution results are isolated per cluster under `outputs/<cluster>/`. The `<cluster>` directory name relies on the `status.infrastructureName` (stripping the randomized installer suffix) or the short cluster ID.

| File | Purpose |
| --- | --- |
| `outputs/<cluster>/reports/*.md` | Markdown report, designed to be committed alongside change tickets. |
| `outputs/<cluster>/reports/*.html` | Standalone HTML report for browser viewing. |
| `outputs/<cluster>/reports/*.summary.html` | Compact HTML fragment intended for `<iframe>` embedding in dashboards. |

Additional structured JSON outputs (`cluster_operators_installed.json`, `catalog_mirror_check.json`, `*.status.json`) are written to `outputs/<cluster>/reports/` and `outputs/<cluster>/operators/`. Output locations can be overridden via `report_output_dir` and `cluster_operators_output_dir`.

## Health Check Coverage

The playbook evaluates the following components. Any non-OK result generates a finding (`INFO`, `WARNING`, or `CRITICAL`). By default, the play fails if any `CRITICAL` finding is detected (`fail_on_critical: true`), making it suitable for CI/CD gating.

1. **ClusterVersion:** Evaluates cluster ID, current version/channel, and update conditions. Verifies if the target version is a recommended or conditional update. (Provides human-readable API/Console URLs).
2. **etcd Health:** Checks per-pod readiness, `etcdctl` round-trip latency, DB size versus quota, leader/raft-term agreement, and active alarms.
3. **MachineConfigPool Render Matrix:** Compares current versus desired MachineConfigs per node to identify degraded nodes or stalled rollouts.
4. **ClusterOperators:** Flags operators not reporting `Available=True`, `Progressing=False`, `Degraded=False`, or `Upgradeable=True`.
5. **MachineConfigPools & MachineSets:** Validates status conditions (`Updated`, `Updating`, `Degraded`) and node consistency (`DESIRED/CURRENT/READY/AVAILABLE`).
6. **Deprecated API Usage:** Scans `APIRequestCount` for resources flagged via `status.removedInRelease`. Attributes usage to specific namespaces. Usage of APIs removed in the target Kubernetes minor release are flagged as `CRITICAL`.
7. **Stuck Finalizers:** Scans Namespaces, PVs, PVCs, and (by default) all established CRDs for objects stuck in a `Terminating` state past `finalizer_scan_stuck_after_seconds` (default: 10m).
8. **Portworx:** Evaluates StorageCluster/StorageNode CR health, daemonset pod health, nodes with `px/service=stop|disabled` labels, and captures `pxctl status`.
9. **OpenShift Virtualization (CNV):** Checks HyperConverged, KubeVirt, and CDI component health. Generates a **per-VM node-drain-readiness matrix** identifying VMs that will stall `oc adm drain` during upgrade (e.g., non-migratable VMs with `LiveMigrate` eviction strategies or mounted CD-ROMs).
10. **Advanced Cluster Management (ACM):** Evaluates MultiClusterHub health and managed-cluster inventory. Can optionally cascade checks to all managed clusters.
11. **OpenShift Data Foundation (ODF):** Validates StorageCluster CR health, CephCluster phase, OSD pod health, and captures structured Ceph health metrics via `rook-ceph-tools`.
12. **Cluster Operators Snapshot:** Generates a standalone JSON dump (`cluster_operators_installed.json`) of all installed operators, subscribed channels, exact CSV versions, and CatalogSource images.
13. **Catalog OPM Render:** Executes `opm render` per CatalogSource to map `olm.channel` entries for installed packages, enabling dependency graph resolution.
14. **Catalog Mirror (IDMS/ICSP/ITMS):** Validates image digest/tag mirror sets. Flags `CRITICAL` if default OperatorHub sources are present on a mirrored cluster, or if subscriptions are bound to unreachable catalogs.

---

## Prerequisites

Ansible requires **Python 3.10+**, `kubernetes >= 27.2.0`, and `websocket-client >= 1.6.0`. Using a Python virtual environment is strongly recommended to prevent system package conflicts during pod executions.

```bash
python3 -m venv ~/venv-ocp && source ~/venv-ocp/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
ansible --version # Ensure core >= 2.16 and Jinja >= 3.1

```

**Permissions:** The executing account requires `cluster-reader` access, plus `pods/exec` permissions in `openshift-etcd`, `openshift-storage` (if using ODF), and namespaces hosting CatalogSource pods (typically `openshift-marketplace`). If etcd exec privileges are denied, the playbook degrades gracefully and skips the latency/DB size checks without failing the run.

## Usage

### Authentication

Set the authentication method via the `ocp_auth_method` variable in `group_vars/all.yml` or via the CLI:

```bash
# 1. Kubeconfig (Default - relies on active context)
ansible-playbook playbook.yml -e ocp_auth_method=kubeconfig

# 2. Token (ServiceAccount or interactive session)
ansible-playbook playbook.yml -e ocp_auth_method=token \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_api_token="$(oc whoami -t)"

# 3. Basic Auth (htpasswd)
ansible-playbook playbook.yml -e ocp_auth_method=password \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_username=admin -e ocp_password="$MY_PASSWORD"

```

### Execution Parameters

Execute the playbook by specifying your target version or channel:

```bash
ansible-playbook playbook.yml -e upgrade_target_version=4.17.14

```

**Common Flags:**

* `-e fail_on_critical=false`: Complete the run and generate reports even if critical findings are discovered.
* `-e upgrade_channel=eus`: Auto-resolve the next Extended Update Support (EUS) target based on the current cluster version.
* `-e portworx_enabled=false` / `-e cnv_enabled=false` / `-e odf_enabled=false`: Disable subsystem checks for clusters not running these workloads.
* `-e finalizer_scan_include_crs=false`: Bypass the dynamic CRD finalizer sweep for faster execution on exceptionally large clusters.
* `-e api_report_exclude_namespaces='["openshift-*","kube-*"]'`: Filter out platform namespaces from the deprecated API report.

---

## Technical Deep Dives

### Upgrade Channel Resolution (EUS-Aware)

Setting `upgrade_channel` directs the playbook to query the Cincinnati update graph (`upgrade_graph_url`) to dynamically determine the correct target version and hop-by-hop update path.

* **EUS Resolution:** Passing `upgrade_channel=eus` from an even minor release (e.g., 4.18) targets the next EUS release (+2 minors, 4.20). From an odd minor release (e.g., 4.17), it targets +1 minor (4.18).
* **Graph Traversal:** The playbook calculates the shortest endorsed upgrade path, acknowledging that EUS-to-EUS upgrades require pausing at an intermediate z-stream (e.g., 4.18 -> 4.19.z -> 4.20).
* **Airgapped Support:** If the graph URL is unreachable, the playbook reports the resolved channel target but skips the explicit path calculation without failing the play.

### etcd Safety Constraints

To ensure zero impact on production control planes, the playbook explicitly **does not** run `etcdctl check perf` or `fio`-based datasync benchmarks. These commands generate heavy write loads that can temporarily degrade cluster performance or trigger false failures. Manual verification guidelines for these metrics are included in the generated report output.

### Storage Validations (Portworx & ODF)

**Portworx:** Evaluates `StorageCluster` components via API and captures `pxctl status`. It does not programmatically parse KVDB quorum or license expiry; this raw output is appended to the report for operator review.

**OpenShift Data Foundation (ODF):** Connects to `rook-ceph-tools` to execute `ceph status -f json`. The playbook parses OSD up/in counts, PG summaries, and mon quorum. It does not validate pool replication profiles or backfill completion times; these are listed as manual pre-flight checks.

### Advanced Cluster Management (ACM) Cascade

When executed against an ACM Hub (`acm_enabled: true`), the playbook maps MultiClusterHub health and managed-cluster inventory.

Setting `acm_cascade_enabled: true` triggers parallel `ansible-playbook` subprocesses for every reachable managed cluster.

* **Credentials:** Prefers existing Hive-provisioned admin kubeconfigs. Alternatively, it can utilize `ManagedServiceAccount` tokens (requires `acm_create_managed_service_accounts: true`).
* **Isolation:** Cascaded runs are completely isolated. Failures in one spoke cluster do not abort the cascade (`acm_cascade_fail_on_critical: false`).
* **Environment Forwarding:** Subprocesses do not inherit the parent CLI arguments, except for the Python interpreter path and `upgrade_channel`. Pass custom parameters to spokes via `acm_cascade_extra_vars`.

### Catalog Mirrors & OPM Rendering

* **Mirror Validation:** Identifies IDMS, ICSP, or ITMS configurations. Flags default catalog sources (`redhat-operators`) as `CRITICAL` if active on a mirrored cluster, as this risks pulling non-mirrored bundles during the upgrade.
* **OPM Render:** Connects directly into active `CatalogSource` pods to execute `opm render <path> -o json`. This avoids redundant image pulls and functions seamlessly on airgapped clusters serving file-based-config images or legacy SQLite DBs.

## Development & Testing

Unit tests and template renders can be run locally using synthetic API objects located in `tests/fixtures.py`.

```bash
python3 tests/test_filters.py            # Test report logic and data parsing
python3 tests/render_report_preview.py   # Generate HTML/MD from test fixtures

```

## License

Licensed under the Apache License, Version 2.0.
