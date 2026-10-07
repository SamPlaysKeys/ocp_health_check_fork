# OpenShift Pre-Upgrade Health Check

A read-only Ansible playbook that validates an OpenShift Container Platform (OCP) cluster's readiness for an upgrade. It queries API resources and performs non-disruptive pod executions to assess cluster health, returning actionable reports without modifying cluster state.

All tasks are executed via `k8s_info` lookups or read-only `exec` commands into existing pods (e.g., `pxctl`, `ceph status`, `etcdctl`).

## Output Artifacts

Execution results are isolated per cluster under `outputs/<cluster>/`. The `<cluster>` directory name relies on the `status.infrastructureName` (stripping the randomized installer suffix) or the short cluster ID.

| File | Purpose |
| --- | --- |
| `outputs/<cluster>/reports/*.md` | Markdown report, designed to be committed alongside change tickets. |
| `outputs/<cluster>/reports/*.html` | Standalone HTML report for browser viewing. |
| `outputs/<cluster>/reports/*.summary.html` | Compact HTML fragment intended for `<iframe>` embedding in dashboards. |

**AAP Integration:** At the end of every run, the summary (overall status, counts, CRITICAL findings) is published via `set_stats` under `ocp_preupgrade_health.<cluster>`. In Ansible Automation Platform (AAP), this surfaces as a job artifact accessible to subsequent workflow nodes.

## Health Check Coverage

The playbook evaluates the following components. Any non-OK result generates a finding (`INFO`, `WARNING`, or `CRITICAL`). By default, the play fails if any `CRITICAL` finding is detected (`fail_on_critical: true`), making it suitable for CI/CD gating.

1. **ClusterVersion:** Evaluates cluster ID, current version/channel, and update conditions. Verifies if the target version is a recommended update.
2. **etcd Health:** Checks per-pod readiness, `etcdctl` round-trip latency, DB size versus quota, leader/raft-term agreement, and active alarms.
3. **MachineConfigPool Render Matrix:** Compares current versus desired MachineConfigs per node to identify degraded nodes or stalled rollouts.
4. **ClusterOperators:** Flags operators not reporting available, non-progressing, non-degraded, and upgradeable states.
5. **MachineConfigPools & MachineSets:** Validates status conditions and node consistency (`DESIRED/CURRENT/READY/AVAILABLE`).
6. **Deprecated API Usage:** Scans `APIRequestCount` for resources flagged via `status.removedInRelease`. Usage of APIs removed in the target Kubernetes minor release are flagged as `CRITICAL`.
7. **Stuck Finalizers:** Scans Namespaces, PVs, PVCs, and established CRDs for objects stuck in a `Terminating` state past `finalizer_scan_stuck_after_seconds` (default: 10m).
8. **Portworx:** Evaluates StorageCluster/StorageNode CR health, daemonset pod health, and captures `pxctl status`.
9. **OpenShift Virtualization (CNV):** Checks HyperConverged components and generates a **per-VM node-drain-readiness matrix** identifying VMs that will stall `oc adm drain` during upgrade.
10. **Advanced Cluster Management (ACM):** Evaluates MultiClusterHub health and managed-cluster inventory. Can optionally cascade checks to all managed clusters.
11. **OpenShift Data Foundation (ODF):** Validates StorageCluster CR health, CephCluster phase, OSD pod health, and captures structured Ceph health metrics via `rook-ceph-tools`.
12. **Catalog Mirror (IDMS/ICSP/ITMS):** Validates image digest/tag mirror sets. Flags `CRITICAL` if default OperatorHub sources are present on a mirrored cluster.

## Prerequisites

Ansible requires **Python 3.10+**, `kubernetes >= 27.2.0`, and `websocket-client >= 1.6.0`. Using a Python virtual environment is strongly recommended to prevent system package conflicts during pod executions.

```bash
python3 -m venv ~/venv-ocp && source ~/venv-ocp/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
ansible --version # Ensure core >= 2.16 and Jinja >= 3.1

```

**Permissions:** The executing account requires `cluster-reader` access, plus `pods/exec` permissions in `openshift-etcd`, `openshift-storage` (if using ODF), and namespaces hosting CatalogSource pods.

## Usage & Authentication

Set the authentication method via the `ocp_auth_method` variable in `group_vars/all.yml` or via the CLI:

### 1. Kubeconfig (Default)

Relies on your active context unless `ocp_context` is explicitly set.

```bash
ansible-playbook playbook.yml -e ocp_auth_method=kubeconfig

```

### 2. Token (ServiceAccount or interactive session)

```bash
ansible-playbook playbook.yml -e ocp_auth_method=token \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_api_token="$(oc whoami -t)"

```

### 3. Username/Password (LDAP, htpasswd, etc.)

The OpenShift API does not accept passwords directly. The playbook trades the credentials for an OAuth token (`tasks/01_oauth_login.yml`), executes the checks, and revokes the token upon completion (`tasks/99_oauth_logout.yml`).

To keep credentials out of your shell history and process list (`ps`), use the `OCP_PASSWORD` environment variable (or a Password-type survey field in AAP):

```bash
read -rsp 'OpenShift password: ' OCP_PASSWORD; echo; export OCP_PASSWORD
ansible-playbook playbook.yml -e ocp_auth_method=password \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_username=jdoe
unset OCP_PASSWORD

```

### Execution Parameters

Execute the playbook by specifying your target version or channel:

```bash
ansible-playbook playbook.yml -e upgrade_target_version=4.17.14

```

**Common Flags:**

* `-e fail_on_critical=false`: Complete the run and generate reports even if critical findings are discovered.
* `-e upgrade_channel=eus`: Auto-resolve the next Extended Update Support (EUS) target based on the current cluster version and the Cincinnati graph.
* `-e portworx_enabled=false` / `-e cnv_enabled=false` / `-e odf_enabled=false`: Disable subsystem checks for clusters not running these workloads.
* `-e acm_enabled=true -e acm_cascade_enabled=true`: Enable ACM hub inventory and cascade checks to all reachable managed clusters in isolated subprocesses.

## Output Data Schemas

The playbook generates structured JSON dumps for downstream tooling. These files are plain data models, not severity findings.

**`cluster_operators_installed.json`**

```json
{
  "cluster_name": "example-01-abcde",
  "cluster": { "current": "4.18.14", "target": "4.20.32", "channel": "EUS" },
  "operators": [
    { 
      "name": "cluster-logging", 
      "channel": "stable-6.2", 
      "version": "6.2.0",
      "catalog": "registry.redhat.io/redhat/redhat-operator-index:v4.20" 
    }
  ]
}

```

**`catalog_mirror_check.json`**

```json
{
  "cluster_name": "example-01-abcde",
  "cluster": {
    "current": "4.18.14", 
    "target": "4.20.34", 
    "channel": "eus",
    "ocp_path": ["4.18", "4.19", "4.20"],
    "upgrade_path": ["4.18.14", "4.18.30", "4.19.33", "4.20.34"]
  },
  "operators": [
    {
      "pull_image": "mirror.local:5000/olm/redhat/redhat-operator-index:v4.18",
      "packages": [
        {
          "name": "web-terminal", 
          "channel": "fast", 
          "version": "1.13.1",
          "max_ocp_version": "", 
          "main": true, 
          "required_by": []
        }
      ]
    }
  ]
}

```

## Project Structure

```text
├── playbook.yml                        # Entry point
├── group_vars/all.yml                  # Configurable tunables & thresholds
├── filter_plugins/ocp_health_filters.py # Core report-building logic (Python)
├── tasks/
│   ├── 00_facts.yml                    # Connection setup & findings collector
│   ├── 01_oauth_login.yml              # OAuth token exchange for basic auth
│   ├── 11_upgrade_path.yml             # EUS channel resolution & Cincinnati graph
│   ├── 85_acm.yml                      # ACM hub health & cascade execution
│   ├── 89_catalog_opm_render.yml       # In-pod opm render execution
│   └── 90_render_report.yml            # Template rendering
├── templates/                          # Jinja2 templates (MD, HTML, Summary)
└── tests/                              # Pytest suite & synthetic API fixtures

```

## Development & Testing

Unit tests and template renders can be run locally using synthetic API objects located in `tests/fixtures.py`.

```bash
python3 tests/test_filters.py            # Test report logic and data parsing
python3 tests/render_report_preview.py   # Generate HTML/MD from test fixtures

```

## License

Licensed under the Apache License, Version 2.0
