# OpenShift pre-upgrade health check

A read-only Ansible playbook that checks an OpenShift Container Platform
cluster's readiness for an upgrade and produces three report artifacts:

| File | Purpose |
|---|---|
| `reports/*.md` | Markdown, meant to be committed alongside the change ticket in git |
| `reports/*.html` | Full standalone report, open in any browser |
| `reports/*.summary.html` | Compact HTML fragment/widget, embed via `<iframe>` in a dashboard/ticket |

It never modifies the cluster - every task is a `k8s_info` read or (optionally)
a `pxctl status`/`pxctl license list` exec into an existing Portworx pod.

## What it checks

1. **ClusterVersion** - cluster ID, current version/channel, update conditions,
   whether your intended target version is a recommended/conditional update.
   The report header also shows the cluster's human-readable **name**
   (`Infrastructure/cluster` `status.infrastructureName`), **API URL**
   (`status.apiServerURL`), and **console URL** (`Console/cluster`
   `status.consoleURL`) - the raw `clusterID` alone is a UUID and hard to
   recognize at a glance across multiple reports/clusters. These are display
   fields only (not health checks); if either object can't be read they fall
   back to "unknown" rather than failing the play. If you'd rather target a
   **channel** than a fixed version - including an EUS jump like 4.18 -> 4.20 -
   set `upgrade_channel` instead of (or alongside) `upgrade_target_version`;
   see [Upgrade channel resolution (EUS-aware)](#upgrade-channel-resolution-eus-aware)
   below.
2. **etcd cluster health** - per-pod readiness, `etcdctl endpoint health`
   round-trip latency, `endpoint status` (dbSize vs quota, leader/raft-term
   agreement across members, learner status), and active alarms (e.g.
   `NOSPACE`). Deliberately **read-only** - see
   [etcd notes](#etcd-notes) below for what is *not* run automatically and why.
3. **Node <-> MachineConfigPool render matrix** - for every node: its pool,
   current vs desired MachineConfig, and whether that matches the pool's
   currently rendered config (catches nodes stuck mid-rollout or degraded).
4. **ClusterOperators** - flags anything not `Available=True/Progressing=False/
   Degraded=False/Upgradeable=True`, with the operator's own status message.
5. **MachineConfigPools** - `Updated`/`Updating`/`Degraded` conditions and
   machine-count consistency (ready/updated/unavailable/degraded).
6. **MachineSets vs Machines** - compares each MachineSet's
   `DESIRED/CURRENT/READY/AVAILABLE` against the actual `Machine` objects it
   owns (phase, whether each has a bound Node).
7. **Deprecated/removed API usage, per namespace** - reads
   `APIRequestCount` (`apiserver.openshift.io/v1`), keeps only resources
   OpenShift itself has flagged via `status.removedInRelease`, and recovers
   the calling **namespace** by parsing
   `system:serviceaccount:<namespace>:<name>` usernames out of
   `status.currentHour`/`status.last24h`. If you set `upgrade_target_version`,
   entries removed at or before your target's Kubernetes minor are marked
   CRITICAL; everything else flagged is WARNING.
8. **Resources stuck Terminating on a finalizer** - Namespaces, PVs, PVCs, and
   (optionally) every installed, Established CustomResourceDefinition kind are
   scanned for objects with `metadata.deletionTimestamp` set but
   `metadata.finalizers` still non-empty. Only flagged CRITICAL once the
   deletion has been stuck for `finalizer_scan_stuck_after_seconds` (default
   10 minutes) - a brand-new deletion in flight is normal and only INFO. The
   dynamic CRD sweep (`finalizer_scan_include_crs: true`, the default) is the
   expensive part - one list call per CRD kind - so set it `false` or use
   `finalizer_scan_exclude_crds` on very large clusters.
9. **Portworx** - StorageCluster/StorageNode CR health, Portworx + Stork pod
   health, nodes still carrying `px/service=stop|disabled` labels, and a
   best-effort `pxctl status` / `pxctl license list` capture. See
   [Portworx section](#portworx-notes) below for what is *not* automated.
10. **OpenShift Virtualization (CNV/KubeVirt)** - HyperConverged/KubeVirt/CDI
    component health, KubeVirt's outdated-workload counter, and a **per-VM
    node-drain-readiness matrix**: every `Running` VirtualMachineInstance is
    checked against its `LiveMigratable` condition, its `evictionStrategy`, and
    whether it has a CD-ROM/ISO disk attached. A VM with `evictionStrategy:
    LiveMigrate` that isn't actually migratable is marked CRITICAL - that
    combination is exactly what stalls `oc adm drain` during the upgrade. A
    CD-ROM/ISO disk is flagged as WARNING even when KubeVirt currently reports
    the VM as migratable, because libvirt refuses to migrate read-only disks
    and this has been seen to stall mid-migration in the field. See
    [OpenShift Virtualization section](#openshift-virtualization-notes) below.
11. **Advanced Cluster Management (ACM)** (flag: `acm_enabled`, default false -
    only relevant when you're running this against an ACM hub) -
    MultiClusterHub operator health, and a **managed-cluster inventory**
    (Available/Accepted/Joined status, versions) for every cluster ACM knows
    about. Optionally (`acm_cascade_enabled: true`) **cascades the entire
    check to every managed cluster** with resolvable credentials and writes
    each one its own separate `.md`/`.html`/`.summary.html` report, plus a
    per-cluster results table on the hub's own report. See
    [ACM notes](#acm-notes) below - this is the one place the project can
    write to a cluster (an opt-in ManagedServiceAccount), so read that
    section before turning the cascade on.

Every non-OK result becomes a `finding` with a severity
(`CRITICAL`/`WARNING`/`INFO`); the play fails at the end if any `CRITICAL`
finding exists and `fail_on_critical: true` (the default) - handy for gating
a CI/pipeline job before it starts the real upgrade.

## Requirements

```bash
ansible-galaxy collection install -r requirements.yml
pip install -r requirements.txt --break-system-packages   # kubernetes python client
```

Needs `ansible-core >= 2.15` and `kubernetes.core >= 3.0`. The account you
connect with needs at least `cluster-reader` (read access to nodes,
clusteroperators, machineconfigpools, machinesets/machines, apirequestcounts,
and the Portworx CRs/pods if `portworx_enabled: true`) **plus** `pods/exec`
in the `openshift-etcd` namespace for the etcd checks - `cluster-reader`
alone does not grant exec. The `Infrastructure` and `Console` singletons read
for the report header's cluster name/API/console URLs are cluster-scoped
`config.openshift.io` resources, readable under `cluster-reader` like the
rest of the checks. In stock OpenShift only `cluster-admin` (and the
etcd operator itself) can exec into the etcd static pods. If the account
running this playbook lacks that permission, the etcd section still lists
the pods it found, but every `etcdctl` capture is reported as "exec failed
or was skipped - review manually" instead of failing the whole play - the
rest of the report renders normally either way.

## Connecting to the cluster

Set `ocp_auth_method` in `group_vars/all.yml` (or with `-e`) to one of:

```bash
# 1. kubeconfig (default) - uses your current context unless ocp_context is set
ansible-playbook playbook.yml -e ocp_auth_method=kubeconfig -e ocp_kubeconfig=~/.kube/config

# 2. token (e.g. `oc whoami -t`, or a ServiceAccount token)
ansible-playbook playbook.yml -e ocp_auth_method=token \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_api_token="$(oc whoami -t)"

# 3. username/password (basic auth / htpasswd identity provider)
ansible-playbook playbook.yml -e ocp_auth_method=password \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_username=admin -e ocp_password="$MY_PASSWORD"
```

Never commit real tokens/passwords - pass them with `-e`, `--vault-id`, or an
environment-backed lookup. All three methods are handled by the
`kubernetes.core` collection directly (see `tasks/00_facts.yml`); this
playbook doesn't shell out to `oc login`.

## Running it

```bash
ansible-playbook playbook.yml \
  -e upgrade_target_version=4.17.14 \
  -e report_output_dir=./reports
```

Useful flags:

- `-e fail_on_critical=false` - always exit 0, just read the report yourself.
- `-e portworx_enabled=false` - skip Portworx checks entirely on clusters that
  don't run it.
- `-e cnv_enabled=false` - skip OpenShift Virtualization checks entirely on
  clusters that don't run it.
- `-e finalizer_scan_include_crs=false` - skip the dynamic per-CRD finalizer
  sweep on very large clusters and keep only the cheap Namespace/PV/PVC checks.
- `--tags portworx,clusteroperators` - run a subset of checks (see the tag on
  each task in `playbook.yml`; the CNV section is tagged `virtualization,cnv`,
  etcd is tagged `etcd`, stuck-finalizer scanning is tagged `finalizers`, and
  ACM is tagged `acm`).
- `-e api_report_exclude_namespaces='["openshift-*","kube-*"]"` - hide
  platform namespaces from the deprecated-API matrix and focus on your own
  workloads.
- `-e acm_enabled=true` - turn on the ACM hub/managed-cluster inventory
  section (only meaningful when you point this playbook at an ACM hub).
- `-e acm_enabled=true -e acm_cascade_enabled=true` - also cascade the full
  check to every managed cluster with resolvable credentials, one separate
  report per cluster. Read [ACM notes](#acm-notes) before turning this on.
- `-e upgrade_channel=eus` - resolve the next EUS target from the cluster's
  current version instead of typing in a fixed `upgrade_target_version`. See
  [Upgrade channel resolution (EUS-aware)](#upgrade-channel-resolution-eus-aware).

All tunables, with comments, live in `group_vars/all.yml`.

## Upgrade channel resolution (EUS-aware)

`upgrade_target_version` (a fixed version like `4.17.14`) still works exactly
as before and always wins if you set it. `upgrade_channel` is the
alternative: point this at a **channel** instead, and the playbook resolves
the actual target version (and, best-effort, the hop-by-hop path to get
there) for you - correctly handling the EUS case, where the target isn't
simply "+1 minor."

```bash
# Auto-derive the next EUS target from wherever the cluster currently is:
#   current minor EVEN (already EUS)  -> +2  (e.g. 4.18 -> 4.20)
#   current minor ODD                 -> +1  (e.g. 4.17 -> 4.18)
ansible-playbook playbook.yml -e upgrade_channel=eus

# Or a bare non-EUS prefix - always the next minor, regardless of parity:
ansible-playbook playbook.yml -e upgrade_channel=stable
ansible-playbook playbook.yml -e upgrade_channel=fast

# Or skip auto-derivation and name the channel outright:
ansible-playbook playbook.yml -e upgrade_channel=eus-4.20
ansible-playbook playbook.yml -e upgrade_channel=stable-4.19

# upgrade_target_version, if also set, overrides whatever upgrade_channel
# resolves to - the resolved channel/path still appears in the report,
# just marked as overridden:
ansible-playbook playbook.yml -e upgrade_channel=eus -e upgrade_target_version=4.20.3
```

**Why the actual hop-by-hop path matters for EUS specifically**: there is no
direct edge from one EUS (even) minor straight to the next in Cincinnati's
update graph - e.g. going from 4.18 to 4.20 genuinely requires passing
through a 4.19.z release first, which is exactly why an "EUS-to-EUS upgrade"
is a two-step process even though it's marketed/labelled as one. This
playbook doesn't hand-wave that: it queries the real Cincinnati update graph
(the same data source `oc adm upgrade`, the OpenShift web console, the [Red
Hat upgrade-graph lab](https://access.redhat.com/labs/ocpupgradegraph/update_path/),
and community tools like
[ctron.github.io/openshift-update-graph](https://ctron.github.io/openshift-update-graph/)
all ultimately read from) via its public JSON API
(`upgrade_graph_url`, default `https://api.openshift.com/api/upgrades_info/v1/graph`)
and computes the real shortest path with a graph search - so the reported
path always reflects an actual Cincinnati-endorsed route, intermediate hops
included, not an assumption.

**Disconnected/firewalled clusters**: the graph fetch is best-effort and
*never fails the play*. If `upgrade_graph_url` isn't reachable from wherever
this playbook runs (common when the cluster - or your bastion - has no
direct internet egress), the report still shows the resolved channel name
and target minor, just without a hop-by-hop path, plus a note to verify
manually with `oc adm upgrade channel <channel>` + `oc adm upgrade` once
that channel is set on the cluster. Point `upgrade_graph_url` at an internal
OpenShift Update Service (OSUS) mirror if you run one - that's exactly what
that variable is for.

**What ends up in the report**: the resolved channel, whether it was
auto-derived or given explicitly, the reasoning ("current 4.18 is already
EUS - next EUS target is +2 -> 4.20"), the hop-by-hop path when the graph
was reachable, and - if `upgrade_target_version` was also set - a note that
it overrode the channel's resolved path. When `upgrade_channel` isn't set at
all, none of this appears and behavior is identical to before this feature
existed.

Other related vars (`group_vars/all.yml`): `upgrade_graph_arch` (default
`amd64`), `upgrade_path_validate_certs`, `upgrade_path_timeout` (seconds,
default 15).

## etcd notes

etcd health (report section 2) uses only commands that don't put write load
on the cluster: `etcdctl endpoint health --cluster -w json` (whose own
round-trip timer, the `took` field, is used as the "IO health" signal),
`etcdctl endpoint status --cluster -w json` (dbSize/quota, leader, raft term,
learner status), and `etcdctl alarm list`. All three run inside the
`etcdctl` sidecar container that OpenShift's own etcd static pod ships,
which already has the right certs/endpoints wired up via environment
variables - no credentials to pass in.

Two things are **deliberately not run automatically**, and are left as manual
checklist items in the report instead:

- `etcdctl check perf` - it generates real write load against etcd, and
  upstream issues report it can grow the etcd DB size
  ([etcd-io/etcd#9326](https://github.com/etcd-io/etcd/issues/9326)) and
  produce false FAILs depending on load profile
  ([#10609](https://github.com/etcd-io/etcd/issues/10609),
  [#13455](https://github.com/etcd-io/etcd/issues/13455)) - not something to
  fire automatically against a production control plane right before an
  upgrade.
- An `fio`-based disk write/fdatasync benchmark - Red Hat documents this for
  genuine disk-hardware validation (99th-percentile fdatasync well under
  10ms), but it sustains real write load against the etcd data disk and
  should be run deliberately in a maintenance window, not as a side effect of
  a routine pre-upgrade check: <https://access.redhat.com/solutions/4885641>

Thresholds (`etcd_took_warn_ms`/`etcd_took_crit_ms`, `etcd_db_warn_pct`/
`etcd_db_crit_pct`) default to Red Hat's own published guidance (network
round-trip p99 < 50ms; dbSize warn/crit at 80%/95% of the 8GiB default
`quota-backend-bytes`) and can be overridden in `group_vars/all.yml` if your
cluster runs a different quota.

## Portworx notes

This automates what's visible through the Kubernetes/OpenShift API
(StorageCluster/StorageNode CR status, pod health, node labels) plus a raw
capture of `pxctl status` / `pxctl license list` for a human to read. It does
**not** parse or validate KVDB quorum detail, storage-pool
rebalance/resync-in-progress state, or license expiry - Portworx's own CLI
output is the source of truth for those and is included verbatim in the
report (section 8) rather than re-implemented here. The report also prints a
manual checklist covering:

- Confirming your Portworx Enterprise/Operator version supports the OCP
  version you're upgrading to, via Portworx's own compatibility matrix
  (Portworx recommends upgrading Portworx itself *before* upgrading
  OpenShift): <https://docs.portworx.com/portworx-enterprise/support-matrix/operator-openshift-upgrade-path>
- No node or storage pool left in maintenance mode.
- Pre-staging kernel module dependencies for the new node kernel.
- Taking a fresh backup/cloudsnap of critical volumes before starting.

If your Portworx install uses a different namespace or pod labels than the
defaults (`portworx_namespace: portworx`, `portworx_pod_selector: name=portworx`),
set those in `group_vars/all.yml`.

## OpenShift Virtualization notes

The VM node-drain-readiness matrix (section 9 of the report) is built entirely
from what KubeVirt itself already reports - the `LiveMigratable` status
condition (the same one `oc get vmis -o wide` shows in the LIVE-MIGRATABLE
column, and the same one the upstream `VMCannotBeEvicted` alert fires on),
each VMI's `evictionStrategy`, and whether it has a CD-ROM/ISO disk attached.
Severity logic, in order:

1. **CRITICAL** - `evictionStrategy` is `LiveMigrate`/`LiveMigrateIfPossible`
   but `LiveMigratable=False`. This is the exact condition that stalls
   `oc adm drain` (and therefore the MachineConfigPool rollout) - the drain
   waits for a migration that will never succeed. Common root causes reported
   by KubeVirt: non-RWX-backed storage, hostpath-provisioner volumes, SR-IOV
   or host-device passthrough, bridge networking.
2. **WARNING** - has a CD-ROM/ISO disk *and* `evictionStrategy` is set to
   migrate, even if `LiveMigratable` currently reports `True`. Read-only disks
   are libvirt's own limitation ("Cannot migrate empty or read-only disk"),
   and this has been reported to stall mid-migration in practice even when
   KubeVirt's own migratability check doesn't catch it upfront - worth a test
   migration or switching the VM to `evictionStrategy: None` before the
   upgrade.
3. **WARNING** - `LiveMigratable=False` but `evictionStrategy` is `None`/unset:
   the VM will be shut down (not stuck) on drain, which is safe for the
   upgrade to proceed but is still worth flagging as expected downtime.
4. **OK** - everything else.

What it does **not** try to infer from here: whether your installed HCO/CNV
version is compatible with the OCP release you're upgrading to. Red Hat has
shipped HCO versions that explicitly block cluster upgrades past certain
OpenShift minors, so that's called out in the manual checklist rather than
guessed at. If your install uses a non-default namespace or CR name, set
`cnv_namespace`, `cnv_hyperconverged_name`, and `cnv_kubevirt_name` in
`group_vars/all.yml`.

## ACM notes

Advanced Cluster Management (ACM) support (report section 12) is three
layers, each a bigger commitment than the last - `acm_enabled: false` by
default, so none of this runs unless you're actually pointing this playbook
at an ACM hub.

**Layer 1-2: hub health + managed-cluster inventory** (`acm_enabled: true`).
Fully read-only, no extra credentials: the MultiClusterHub CR's own
`status.phase`, and every `ManagedCluster`'s `ManagedClusterConditionAvailable`
/ `HubAcceptedManagedCluster` / `ManagedClusterJoined` conditions. This alone
tells you whether ACM itself is healthy and which managed clusters the hub
can currently reach - useful even if you never turn on the cascade.

**Layer 3: the cascade** (`acm_cascade_enabled: true`). Re-runs this entire
playbook against every managed cluster with resolvable credentials, as a
separate `ansible-playbook` subprocess per cluster (not a nested Ansible
loop - full state isolation for free, and the well-tested single-cluster
logic is reused completely unchanged). Each cluster gets its own
`.md`/`.html`/`.summary.html` report under `acm_cascade_report_dir` (default
`<report_output_dir>/acm-managed-clusters/`), and the hub's own report gets a
credential-resolution table plus a per-cluster results summary. A cluster
whose child run fails before producing its report is shown as `UNKNOWN`, not
silently dropped.

**Credentials, in order of preference:**

1. **Hive-provisioned admin-kubeconfig secret** - if ACM itself installed the
   cluster (via Hive), a `<cluster-name>-admin-kubeconfig` Secret already
   exists in that cluster's own namespace on the hub. Reading it is a plain
   read, no writes, the strongest credential available, and used first
   whenever present (`acm_prefer_hive_kubeconfig: true`, the default).
2. **ManagedServiceAccount token** - for imported clusters (no Hive secret),
   ACM's `managed-serviceaccount` add-on can provision a real ServiceAccount
   on the spoke and sync its token back to the hub as a Secret. By default
   (`acm_create_managed_service_accounts: false`) the playbook only *reads*
   a `ManagedServiceAccount` + token Secret that already exists - it won't
   create one. Set `acm_create_managed_service_accounts: true` to let it
   create the CR (named `acm_msa_name`, default `ocp-preupgrade-healthcheck`,
   so it's obviously attributable if you go looking for it later) for any
   candidate cluster that doesn't already have one - **this is the one
   place in this otherwise read-only project that writes to a cluster** (the
   hub, specifically - the CR it creates there is what causes the add-on to
   provision the actual ServiceAccount on the spoke). The token alone
   doesn't grant anything, though - see the Policy manifest below.
3. **Neither** - the cluster is reported `checked: false` in the
   credential-resolution table with a specific reason (no credentials, not
   Available, or excluded), never silently skipped.

**RBAC for the ManagedServiceAccount token**: a bare token has zero
permissions until something binds its ServiceAccount to a role on the spoke.
`extras/acm-policy-managed-serviceaccount-rbac.yaml` is a reference ACM
Policy (Policy + Placement + PlacementBinding) that grants it `cluster-reader`
plus `pods/exec` in `openshift-etcd`, self-healing via ACM's governance
framework if the bindings ever drift. It is **not applied automatically** -
review and adapt the namespace/ManagedClusterSet targeting for your
environment, then `oc apply -f` it on the hub yourself. This is a deliberate
choice: Policy is the right tool for "make sure this RBAC exists on every
managed cluster, forever" (a continuous enforcement loop), but not for
running the health check itself - running the health check stays an
Ansible/subprocess job, same as the rest of this project.

**Reaching the spoke's API**: by default (`acm_use_cluster_proxy: false`)
the cascade connects directly to each spoke's own API server, using the URL
ACM already has in `ManagedCluster.spec.managedClusterClientConfigs` - this
needs whatever's running this playbook to have network-level reachability to
every managed cluster's API endpoint, which is the common case for most OCP
fleets. If your spokes are network-isolated and only reachable through ACM's
`cluster-proxy` add-on, set `acm_use_cluster_proxy: true` and
`acm_cluster_proxy_base_url` - but note the proxy's internal service is only
reachable from inside/near the hub cluster itself, so you'd need to run this
playbook from there (e.g. as an in-cluster Job) rather than from an external
workstation. This path is implemented but not something this project has
been able to validate against a real cluster-proxy setup - treat it as a
starting point, not a guarantee.

**Other things worth knowing:**

- `acm_exclude_clusters` defaults to `["local-cluster"]` - ACM lists the hub
  itself as a ManagedCluster named `local-cluster`; it's excluded from the
  cascade by default since you'd normally just run this playbook against the
  hub directly (its own report already covers it) - still shown in the
  inventory table either way, just skipped by the cascade specifically.
- `acm_cascade_fail_on_critical` defaults to `false` and is passed to each
  child run - a spoke's CRITICAL findings still show up in the cascade
  summary either way; this only controls whether that child process itself
  exits non-zero, kept off so one bad spoke can't abort the others.
- `acm_cascade_upgrade_target_version` is usually left blank - spokes are
  often on different versions/rollout timelines than the hub, so the
  update-path check isn't run identically everywhere by default. Set it if
  you actually want that check applied uniformly.
- Credential material (kubeconfig files, extra-vars files with tokens in
  them) is written to a scratch directory under `report_output_dir` for the
  duration of the cascade and deleted immediately after - if a run is killed
  mid-cascade, check `<report_output_dir>/.acm-cascade-scratch/` before
  assuming it's gone.
- **The cascade's child processes do NOT inherit your outer command line.**
  Each cascaded cluster is a brand-new `ansible-playbook` process (see Layer 3
  above) with its own argv - `-e` flags, environment variables, and anything
  else you passed to the outer run are invisible to it unless this playbook
  explicitly writes them into that child's extra-vars file. The one exception
  is `ansible_python_interpreter`, which is forwarded automatically: whatever
  value the outer run resolved (your own `-e ansible_python_interpreter=...`
  override, or the inventory's `{{ ansible_playbook_python }}` default) is
  captured and passed to every child, so if you're running with a pyenv/
  virtualenv Python that has `kubernetes`/`openshift` pip-installed (and
  `ansible-playbook` itself isn't launched from inside that venv, so Ansible
  can't discover it on its own), you only need `-e
  ansible_python_interpreter=/path/to/venv/bin/python` on the *outer* command
  - it now reaches every cascaded cluster too. If you need to forward
  anything else into the child runs (a different var, a proxy setting), set
  `acm_cascade_extra_vars` in `group_vars/all.yml` (or `-e
  acm_cascade_extra_vars='{"key":"value"}'`) - it's merged on top of
  everything else per-cluster, so it can also override the auto-forwarded
  interpreter if you ever need a *different* one for the child runs than for
  the outer/hub one.

## Deprecated-API-per-namespace caveats

- `APIRequestCount` aggregates the **last 24h** (plus the current hour); it
  won't catch something that only ran last week. Run this close to when you
  actually plan to upgrade.
- Requests from real users (not service accounts) are grouped under
  `(non-namespaced / human user)` since there's no namespace to recover.
- `ocp_to_k8s_minor_map` in `group_vars/all.yml` is a best-effort OCP-minor to
  Kubernetes-minor lookup table used only to decide CRITICAL vs WARNING for
  your specific `upgrade_target_version`. Verify it against the release notes
  for your target version before trusting it blindly, and add new rows as new
  OCP releases ship.

## Testing without a live cluster

```bash
python3 tests/test_filters.py            # unit tests for the report-building logic
python3 tests/render_report_preview.py   # renders all 3 templates with synthetic data -> tests/preview_out/
```

Both use the fixtures in `tests/fixtures.py` (synthetic but realistic
`oc get -o json`-shaped objects) so you can sanity-check any change to
`filter_plugins/ocp_health_filters.py` or the templates before pointing this
at a real cluster.

## Project layout

```
playbook.yml                    entry point
group_vars/all.yml               every tunable, with comments
inventory/hosts.yml               localhost - this talks to the API, not SSH
tasks/00_facts.yml                 connection setup, findings collector
tasks/10_clusterversion.yml
tasks/11_upgrade_path.yml          upgrade_channel resolution (EUS-aware) + Cincinnati graph path
tasks/15_etcd_health.yml
tasks/20_nodes_mcp_matrix.yml
tasks/30_clusteroperators.yml
tasks/40_machineconfigpools.yml
tasks/50_machinesets.yml
tasks/60_deprecated_apis.yml
tasks/65_stuck_finalizers.yml
tasks/70_portworx.yml
tasks/80_openshift_virtualization.yml
tasks/85_acm.yml                   ACM hub health, managed-cluster inventory, cascade
tasks/85a_acm_wait_msa_secret.yml    included per-cluster from 85_acm.yml
tasks/90_render_report.yml         renders templates, fails on CRITICAL
filter_plugins/ocp_health_filters.py   all the report-building logic (unit tested)
templates/report.md.j2 / report.html.j2 / report_summary.html.j2
extras/acm-policy-managed-serviceaccount-rbac.yaml   reference ACM Policy (see ACM notes) - not auto-applied
tests/                             fixtures + offline unit/render tests
```

## Version control

This project is a git repository (`git log` to see its history). Every
change from the point git was initialized onward gets its own commit with a
real diff - `git log -p`, `git diff <rev>..<rev>`, or `git blame` on any file
show exactly what changed and (in the commit message) why. `.gitignore`
excludes everything generated by a run (`reports/`, the ACM cascade's
`.acm-cascade-scratch/` credential scratch dir, `tests/preview_out/`,
`__pycache__/`, `*.pyc`) so only the actual project source is tracked.

If you're pulling this project into your own team's repo, either add it as a
subtree/subdirectory of your existing repo (dropping its own `.git/` history,
or preserving it with `git subtree add`/`git remote add + fetch` if you want
both histories linked) or push this repo's history to a remote of your own
(`git remote add origin <url> && git push -u origin main`) to keep it
independent. Either way, treat this repo's own history (from initialization
onward) as the record of how the playbook evolved - it's more precise than
any changelog kept alongside it.
