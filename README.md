# OpenShift pre-upgrade health check

A read-only Ansible playbook that checks an OpenShift Container Platform
cluster's readiness for an upgrade and produces three report artifacts:

| File | Purpose |
|---|---|
| `outputs/<cluster>/reports/*.md` | Markdown, meant to be committed alongside the change ticket in git |
| `outputs/<cluster>/reports/*.html` | Full standalone report, open in any browser |
| `outputs/<cluster>/reports/*.summary.html` | Compact HTML fragment/widget, embed via `<iframe>` in a dashboard/ticket |

Everything a run writes goes into one folder per cluster,
`outputs/<cluster>/`, so runs against many clusters don't mix. Inside it,
`reports/` holds these reports plus `*.status.json`, and `operators/` holds
the operator and catalog outputs described below. `<cluster>`
is the cluster's `status.infrastructureName` without the random suffix the
installer appends (`example-01-abcde-x7k2p` -> `example-01-abcde`), or the
short cluster ID when the name can't be read. The JSON outputs
(`cluster_operators_installed.json`, `catalog_mirror_check.json`,
`*.status.json`) carry the same value as a top-level `"cluster_name"`.
Set `report_output_dir` / `cluster_operators_output_dir` to write elsewhere.

It never modifies the cluster - every task is a `k8s_info` read or (optionally)
a `pxctl status`/`pxctl license list` exec into an existing Portworx pod, or a
read-only `ceph status`/`ceph osd status`/`ceph df`/`ceph health detail` exec
into an existing ODF `rook-ceph-tools` pod.

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
12. **OpenShift Data Foundation (ODF)** - validates the operator is actually
    installed (StorageCluster CR presence, same pattern as Portworx above),
    then checks StorageCluster component health (reuses the ClusterOperator-
    style `co_report`), the CephCluster CR's own phase + `ceph.health`
    summary, `rook-ceph-osd` pod health, and - best-effort, via `oc exec` into
    the existing `rook-ceph-tools` pod - a structured `ceph status -f json`
    parse (overall health, individual named health checks, OSD up/in counts,
    PG summary, mon quorum) plus raw `ceph osd status`/`ceph df`/`ceph health
    detail` captures for a human to read directly. See
    [ODF notes](#odf-notes) below for what is *not* automated.
13. **Cluster operators installed snapshot** (flag:
    `cluster_operators_snapshot_enabled`, default true) - a standalone data
    dump, not part of the findings/severity report above: scans every OLM
    Subscription/ClusterServiceVersion/CatalogSource and writes a
    fixed-shape `outputs/<cluster>/operators/cluster_operators_installed.json` (plus a
    companion `.md`) listing every installed operator's package name,
    subscribed channel, exact CSV version, and the image of whichever
    CatalogSource it came from (Red Hat, certified, marketplace, or
    community - no filtering). See
    [Cluster operators snapshot notes](#cluster-operators-snapshot-notes)
    below.
14. **Per-catalog opm render** (flag: `catalog_render_enabled`, default true,
    requires `cluster_operators_snapshot_enabled: true`) - a follow-on to
    #13: for every CatalogSource an installed operator actually came from,
    execs `opm render <source-path> -o json` inside that CatalogSource's own
    already-running pod and writes the `olm.channel` entries for just this
    cluster's installed packages to `outputs/<cluster>/operators/<catalog-image-name>_<tag>.json`
    - one file per unique catalog image, so a downstream consumer can walk
    each package's `replaces`/`skipRange` graph itself. Best-effort per
    catalog (a missing pod or failed exec becomes a WARNING finding, not a
    failed play). See
    [Catalog opm render notes](#catalog-opm-render-notes) below.
15. **Catalog mirror (IDMS/ICSP/ITMS) check** (always runs) - any
    `ImageDigestMirrorSet`, `ImageContentSourcePolicy` or `ImageTagMirrorSet`
    makes this a mirrored cluster; none makes it a connected cluster, where nothing is flagged. It
    also reports each mirror entry's `mirrorSourcePolicy` and how each
    catalog index image is actually pulled. On a
    mirrored cluster it flags **CRITICAL** if a default OperatorHub
    CatalogSource (`catalog_default_sources`) is still present, and
    **CRITICAL** for each operator whose InstallPlan or Subscription is
    still bound to the old default catalog instead of a mirrored one. Writes `outputs/<cluster>/operators/catalog_mirror_check.json` and
    report section 14. See
    [Catalog mirror (IDMS/ICSP/ITMS) notes](#catalog-mirror-idmsicspitms-notes) below.

Every non-OK result becomes a `finding` with a severity
(`CRITICAL`/`WARNING`/`INFO`); the play fails at the end if any `CRITICAL`
finding exists and `fail_on_critical: true` (the default) - handy for gating
a CI/pipeline job before it starts the real upgrade.

## Requirements

```bash
python3 -m venv ~/venv-ocp && source ~/venv-ocp/bin/activate   # Python 3.10+
pip install -r requirements.txt          # ansible-core>=2.16, jinja2>=3.1, kubernetes, websocket-client
ansible-galaxy collection install -r requirements.yml
ansible --version                        # should show core 2.16+, jinja 3.1+ and the venv's Python
```

Use **`ansible-core >= 2.16`** (needs **Python 3.10+**), the version current
`kubernetes.core` releases support. 2.12 is the oldest the playbook accepts;
2.12 to 2.15 work but print a note. The Python running Ansible also needs
`kubernetes >= 27.2.0` and `websocket-client >= 1.6.0`. The playbook's first
tasks check all of this and stop with a clear message otherwise.

Use a virtualenv rather than the OS-packaged `ansible`: an old distro
Ansible (e.g. 2.10) and its old `kubernetes`/`websocket-client` packages
make every pod exec (etcd, ODF/Ceph, `opm render`) fail with errors like
`'NoneType' object has no attribute 'decode'`. The account you
connect with needs at least `cluster-reader` (read access to nodes,
clusteroperators, machineconfigpools, machinesets/machines, apirequestcounts,
and the Portworx/ODF CRs and pods if `portworx_enabled`/`odf_enabled: true`)
**plus** `pods/exec` in the `openshift-etcd` namespace for the etcd checks,
in the `openshift-storage` namespace for the ODF/Ceph checks, and in every
namespace that hosts a CatalogSource pod (`openshift-marketplace` by default)
for the per-catalog `opm render` step (task 89) -
`cluster-reader` alone does not grant exec. The `Infrastructure` and `Console` singletons read
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

# 3. username/password (any identity provider: LDAP, htpasswd, ...)
read -rsp 'OpenShift password: ' OCP_PASSWORD; echo; export OCP_PASSWORD
ansible-playbook playbook.yml -e ocp_auth_method=password \
  -e ocp_api_host=https://api.mycluster.example.com:6443 \
  -e ocp_username=jdoe
unset OCP_PASSWORD
```

Never commit real tokens/passwords - pass them with `-e`, `--vault-id`, or an
environment-backed lookup. kubeconfig and token are handed to the
`kubernetes.core` collection directly (see `tasks/00_facts.yml`). The
password method logs in like `oc login` does: `tasks/01_oauth_login.yml`
trades the username/password for an OAuth token once (the OpenShift API
server itself doesn't accept passwords), every task then uses the token, and
`tasks/99_oauth_logout.yml` revokes it at the end, even when the run fails.
`ocp_password` defaults to the `OCP_PASSWORD` environment variable, so the
password stays out of the command line (`ps`) and shell history; in AAP,
pass it from a Password-type survey field.

At the end of every run the summary (overall status, counts, CRITICAL
findings) is published with `set_stats` under `ocp_preupgrade_health.<cluster>`:
in AAP it shows as the job's artifacts and reaches later workflow nodes.

## Running it

```bash
ansible-playbook playbook.yml \
  -e upgrade_target_version=4.17.14
```

Useful flags:

- `-e fail_on_critical=false` - always exit 0, just read the report yourself.
- `-e portworx_enabled=false` - skip Portworx checks entirely on clusters that
  don't run it.
- `-e cnv_enabled=false` - skip OpenShift Virtualization checks entirely on
  clusters that don't run it.
- `-e odf_enabled=false` - skip OpenShift Data Foundation checks entirely on
  clusters that don't run it.
- `-e odf_exec_enabled=false` - keep the ODF CR/pod checks but skip the `ceph`
  exec calls (e.g. no `rook-ceph-tools` pod available, or you'd rather not
  exec at all).
- `-e finalizer_scan_include_crs=false` - skip the dynamic per-CRD finalizer
  sweep on very large clusters and keep only the cheap Namespace/PV/PVC checks.
- `--tags portworx,clusteroperators` - run a subset of checks (see the tag on
  each task in `playbook.yml`; the CNV section is tagged `virtualization,cnv`,
  etcd is tagged `etcd`, stuck-finalizer scanning is tagged `finalizers`, ACM
  is tagged `acm`, and ODF is tagged `odf,storage`).
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
report (section 10) rather than re-implemented here. The report also prints a
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

## ODF notes

OpenShift Data Foundation (ODF/OCS) support (report section 13) mirrors the
Portworx module's shape exactly, per how this section was requested: first
confirm the operator is actually installed (a StorageCluster CR present in
`odf_namespace`, default `openshift-storage`), and only run the detail checks
when it is.

Three CRs/objects are involved, each reported differently because they don't
all speak the same status "language":

- **StorageCluster** (`ocs.openshift.io/v1`) follows the same
  `Available`/`Progressing`/`Degraded`/`Upgradeable` condition convention
  ClusterOperators use, so it reuses the existing `co_report()` filter -
  exactly like CNV's HyperConverged/KubeVirt/CDI CRs do.
- **CephCluster** (`ceph.rook.io/v1`) does **not** follow that convention - it
  reports via a top-level `status.phase` (`Ready`/`Progressing`/`Failure`/...)
  plus a nested `status.ceph.health` summary (`HEALTH_OK`/`HEALTH_WARN`/
  `HEALTH_ERR`) that Rook keeps in sync with the storage cluster's actual
  `ceph status`. A separate filter (`cephcluster_report()`) handles this shape,
  but produces output in the same `name`/`severity`/`messages` form so the
  task file and templates can treat every component report uniformly.
- **`rook-ceph-osd` pods** - one pod per OSD, checked for `Running` the same
  way Portworx's daemonset pods are.

**Ceph cluster health, from the horse's mouth**: when `odf_exec_enabled: true`
(the default) and a `Running` `rook-ceph-tools` pod is found, the playbook
runs `ceph status -f json` and parses it into a structured report - overall
health, every individual named health check Ceph itself is reporting (e.g.
`OSD_DOWN`, `PG_DEGRADED`) with its own severity and message, OSD up/in
counts from `osdmap`, a PG/capacity summary from `pgmap`, and mon quorum
membership. That parse happens in Python (`ceph_status_report()` in
`filter_plugins/ocp_health_filters.py`), never via Jinja's `from_json` filter
in the task file - the same defensive-parsing lesson already applied to
etcdctl's JSON output (Jinja2's native environment can hand a filter plugin an
already-converted native dict, and a second JSON-decode pass on that blows
up). A failed or missing exec capture degrades to an "unparsed" report rather
than crashing the play; the raw captured text is always shown in the report
regardless of whether the parse succeeded. Three more raw, unparsed captures
are included alongside it for a human to read directly: `ceph osd status`,
`ceph df`, and `ceph health detail`.

The report also cross-checks Ceph's own `osdmap.num_osds` against the actual
number of `rook-ceph-osd` pods found - a mismatch (e.g. an OSD pod that never
scheduled, or a stale OSD entry Ceph hasn't forgotten) is flagged WARNING even
when Ceph's overall health looks fine.

What this does **not** try to infer from here: pool replication/erasure-coding
profile correctness, MDS (CephFS)/RGW (object) subsystem health beyond what
`ceph status` itself surfaces, in-flight backfill/recovery completion time, or
ODF<->OCP version compatibility. These are called out as manual checklist
items in the report instead, alongside the raw `ceph` output needed to check
them:

- Confirming your installed ODF/OCS Operator version supports the OCP version
  you're upgrading to, via Red Hat's ODF-OCP interoperability matrix (and
  upgrading ODF itself first, same ordering guidance as Portworx):
  <https://access.redhat.com/articles/5001441>
- Pool/cluster capacity headroom from the captured `ceph df` output - don't
  start the upgrade with a pool at or near full.
- Any OSD reported up-but-not-in, or reweighted to 0, in `ceph osd status` -
  silent capacity/redundancy loss even when the pod itself is `Running`.
- No scrub/backfill/recovery operation expected to still be running when node
  drains begin.
- MDS/RGW-specific health, if those subsystems are in use - checked
  separately, outside this module's scope.

If your ODF install uses a different namespace or CR/pod-selector names than
the defaults (`odf_namespace: openshift-storage`,
`odf_osd_pod_selector: app=rook-ceph-osd`,
`odf_tools_pod_selector: app=rook-ceph-tools`), set those in
`group_vars/all.yml`.

## Cluster operators snapshot notes

Unlike every other section, `outputs/<cluster>/operators/cluster_operators_installed.json` (and
its companion `.md`) is **not** a findings/severity report - it's a plain
data dump for downstream tooling, in exactly this shape and no other keys:

```json
{
  "cluster_name": "example-01-abcde",
  "cluster": { "current": "4.18.14", "target": "4.20.32", "channel": "EUS" },
  "operators": [
    { "name": "cluster-logging", "channel": "stable-6.2", "version": "6.2.0",
      "catalog": "registry.redhat.io/redhat/redhat-operator-index:v4.20" }
  ]
}
```

- **name**/**channel** come straight from each `Subscription`'s
  `spec.name`/`spec.channel`.
- **version** comes from the matching `ClusterServiceVersion`'s own
  `spec.version` field - a real, authoritative field on the CSV, not parsed
  out of its name, so it correctly carries a build/prerelease suffix as-is
  (e.g. `4.18.27-rhodf`, `4.18.0-202608142236`) exactly like the cluster
  itself reports it.
- **catalog** is the `spec.image` of the `CatalogSource` that Subscription's
  `spec.source`/`spec.sourceNamespace` points to - reported for every
  operator regardless of which catalog it came from (Red Hat, certified,
  marketplace, community, or a custom one). There's no catalog filtering at
  all here - unlike a more targeted per-catalog check might do, this section
  is intentionally just "what's installed and where did it come from."
- **cluster.channel** prefers the resolved `upgrade_channel` target (e.g.
  `upgrade_channel: eus` resolves to `eus-4.20`) when `upgrade_channel` was
  set, falling back to the cluster's own live `ClusterVersion` channel
  otherwise. Any EUS-family channel is collapsed to the literal `"EUS"`
  regardless of target minor; every other channel family (`stable-4.19`,
  `fast-4.18`, ...) is kept as its full name.
- A `Subscription` with no installed CSV yet (stuck, or still installing) is
  **skipped** - there's no resolvable version for it, and this schema has no
  null fields.
- If the same package is subscribed more than once with an **identical**
  channel and version (e.g. installed in two different namespaces), the
  duplicate is **consolidated** into a single entry. If the channel or
  version differs between them, both are kept - they're not really
  duplicates at that point.

Set `cluster_operators_snapshot_enabled: false` to skip this section
entirely, or `cluster_operators_output_dir` to write somewhere other than
the default per-cluster `outputs/<cluster>/operators/` folder.

## Catalog opm render notes

A follow-on to the snapshot above (task 89,
`tasks/89_catalog_opm_render.yml`): for every `CatalogSource` an installed
operator actually came from, write that catalog's own `opm render` output -
filtered to just this cluster's installed packages - to its own
`outputs/<cluster>/operators/<catalog-image-name>_<tag>.json`, e.g.
`outputs/<cluster>/operators/redhat-operator-index_v4.20.json`:

```json
[
  { "package": "cluster-logging", "channel": "stable-6.2",
    "entries": ["cluster-logging.v6.2.0", "cluster-logging.v6.1.0"] }
]
```

**Why exec into the CatalogSource's own pod instead of pulling the index
image again**: that pod already *is* the catalog image - it's running
`opm serve <source-path>` from it right now, serving OLM's own grpc queries
on port 50051. Re-pulling the image separately (or querying it over grpc,
which speaks a different protobuf-based protocol, not the FBC JSON below)
would be redundant and, on an airgapped cluster, might not even be possible
from wherever this playbook runs. Instead this execs `opm render
<source-path> -o json` **inside that pod**, reading its own local
filesystem - works identically for a Red Hat catalog, a certified/
marketplace one, or a mirrored `private.registry.local/...` catalog on an
airgapped install, and for both a file-based-config image (`<source-path>`
is a directory, typically `/configs`) and a legacy sqlite-DB image
(`<source-path>` is a `.db` file) - the exact path is read off that pod's
own `opm serve <source-path>` container args rather than assumed.

**Filtering**: `opm render`'s `-o json` output is a *stream* of
newline-delimited declarative-config objects (`olm.package`, `olm.channel`,
`olm.bundle`, ...), not one JSON array - parsed defensively in Python
(`opm_render_filter()` in `filter_plugins/ocp_health_filters.py`, same
line-by-line/never-via-Jinja posture as `ceph_status_report()`). Only
`olm.channel` objects are kept, and only for packages this cluster actually
has installed **from that specific catalog** (the same package list already
in `cluster_operators_installed.json` for that catalog - see #13 above,
never re-derived from raw Subscriptions, so the two files can't drift
apart). Every other schema, and every channel for a package this cluster
doesn't have from that catalog, is dropped - keeping the output scoped to
what a downstream consumer actually needs to walk `replaces`/`skipRange`
for its own installed operators, rather than rendering (and shipping) the
entire catalog.

**Best-effort, per catalog** - a catalog whose Pod can't be found (wrong
`olm.catalogSource` label, pod not `Running`), whose `opm serve` source path
can't be parsed from its container args, or whose `opm render` exec fails
(no `opm` binary in that particular catalog image, or `pods/exec` not
granted in its namespace) becomes a single WARNING finding for that catalog
and is skipped - it never fails the whole play, and every other resolvable
catalog is still rendered.

Requires `cluster_operators_snapshot_enabled: true` (task 88 must run first
in the same play - task 89 asserts this explicitly rather than silently
producing nothing). Set `catalog_render_enabled: false` to skip this section
entirely, or `catalog_render_output_dir` to write somewhere other than
`cluster_operators_output_dir` (which itself defaults to `outputs/<cluster>/operators/`).

## Catalog mirror (IDMS/ICSP/ITMS) notes

Task 89b (`tasks/89b_catalog_mirror_check.yml`, filter
`catalog_mirror_report()`). It runs on its own, so `--tags catalog_mirror` works without the other tasks.
There is no enable flag: skipping this check would let a mirrored cluster
with leftover default catalogs report as healthy.

**How mirror configuration redirects pulls.** The MCO renders these
resources into CRI-O's `registries.conf`:

| Resource | Redirects | Falls back to the source registry when the mirror misses? |
|---|---|---|
| `ImageDigestMirrorSet` (`config.openshift.io/v1`) | pulls by digest (`@sha256:...`): operator bundles and operands, release payload | Per entry `mirrorSourcePolicy`: `AllowContactingSource` (default) yes, `NeverContactSource` no |
| `ImageContentSourcePolicy` (`operator.openshift.io/v1alpha1`, deprecated) | pulls by digest | Always (no policy field) |
| `ImageTagMirrorSet` (`config.openshift.io/v1`) | pulls by tag (`:v4.20`), which is how catalog index images are normally referenced | Per entry `mirrorSourcePolicy`, same as IDMS |

Consequences:

- Mirrors are tried first even if the cluster can still reach the internet,
  so any IDMS, ICSP or ITMS makes the cluster **mirrored**. With none of them,
  it is a **connected** cluster.
- IDMS and ICSP do **not** redirect tag pulls. Without an ITMS, a default
  catalog such as `redhat-operators` keeps pulling its index straight from
  `registry.redhat.io`. It then lists every bundle Red Hat publishes,
  including bundles that were never mirrored.
- With fallback allowed (`AllowContactingSource` or ICSP), an operator
  installed from such a bundle works today through the internet and breaks
  once the cluster is disconnected. With `NeverContactSource`, it fails
  immediately with `ImagePullBackOff`.

What the check reports:

- **Mirror sets**: every IDMS, ICSP and ITMS entry, with its source, mirrors and
  effective `mirrorSourcePolicy`. Each mirror location's registry host
  (e.g. `mirror.local:5000`) is a *mirror host*.
- **Catalog index pull paths**: for each CatalogSource, whether its image
  already points at a mirror host, is redirected by an IDMS/ICSP (digest
  ref) or ITMS (tag ref) entry (with that entry's policy), or is pulled
  straight from the **source registry**. Sources match on a path prefix
  (`registry.redhat.io/redhat` covers `registry.redhat.io/redhat/...`) or a
  `*.example.com` wildcard host.
- **Notes**: plain-language explanations of the above for this cluster, shown in report section 14.
- **INFO findings**: digest mirrors that fall back to the source, so an image
  missing from the mirror is pulled from the internet without notice (set
  `NeverContactSource` to prove the mirror is complete). Also any
  deprecated ICSP still in use (`oc adm migrate icsp` converts it to IDMS/ITMS).
- **Default sources**: each name in `catalog_default_sources` is
  checked for a live CatalogSource in `openshift-marketplace`, and
  `OperatorHub/cluster` is read to see whether it is disabled
  (`spec.disableAllDefaultSources` or a per-source `disabled: true`). With a
  mirror configured, any default CatalogSource that still exists is
  **CRITICAL**.
- **InstallPlan catalog**: for each Subscription, the check follows
  `status.installPlanRef` and reads the catalog that resolved the installed
  CSV from `status.bundleLookups[].catalogSourceRef`. When that is missing,
  it falls back to the InstallPlan's legacy `spec.catalogSource`. The catalog is
  classified as one of:
  - `mirrored` - the CatalogSource index is pulled from the mirror (it is on a mirror host,
    or an IDMS/ICSP/ITMS entry covers it). **OK**.
  - `default` - one of the default sources, even when an ITMS redirects
    its index. **CRITICAL**: re-point the
    Subscription to the mirrored CatalogSource. If the Subscription
    already points to the mirror, the message notes that the next update
    will resolve from it.
  - `missing` - the CatalogSource no longer exists. **CRITICAL**.
  - `other` - a custom catalog pulled from its own registry. **INFO**.
  - `unknown` - no InstallPlan or catalog was found. **INFO**.

  If the Subscription itself points to a default or missing source, it is
  also flagged **CRITICAL**.

  These cases are CRITICAL because they block the upgrade. OLM resolves
  those operators' updates from a catalog outside the mirror, so the bundles it
  picks may not be mirrored.
- **`outputs/<cluster>/operators/catalog_mirror_check.json`** contains only what an external
  `opm` script needs to look up upgrade metadata: the installed operators,
  grouped by the catalog image to pull. The script can run `opm render`
  once per image:

  ```json
  {
    "cluster_name": "example-01-abcde",
    "cluster": {"current": "4.18.14", "target": "4.20.34", "channel": "eus",
                "ocp_path": ["4.18", "4.19", "4.20"],
                "upgrade_path": ["4.18.14", "4.18.30", "4.19.33", "4.20.34"]},
    "operators": [
      {
        "pull_image": "mirror.local:5000/olm/redhat/redhat-operator-index:v4.18",
        "packages": [
          {"name": "devworkspace-operator", "channel": "fast", "version": "0.43.0",
           "max_ocp_version": "", "main": false, "required_by": ["web-terminal"]},
          {"name": "web-terminal", "channel": "fast", "version": "1.13.1",
           "max_ocp_version": "", "main": true, "required_by": []}
        ]
      }
    ]
  }
  ```

  - `cluster.ocp_path` lists the OCP releases the upgrade passes through,
    and so the catalog versions to fetch, e.g. `pull_image` retagged `:v4.19`,
    `:v4.20`. The target is `upgrade_target_version` when set. Otherwise it is
    resolved from `upgrade_channel`, which defaults to EUS: an even current
    minor goes +2, an odd one +1. `channel` is `eus` for a two-release path
    and `stable` (or the given channel prefix) for a one-release path.
  - `cluster.upgrade_path` is the same route at z-stream level: the hops
    the Cincinnati graph endorses from the current version to the target,
    to the exact `upgrade_target_version` when one is set. It needs
    `upgrade_channel` and a reachable `upgrade_graph_url`; otherwise it is
    `[]` and `target` is only `major.minor` unless `upgrade_target_version`
    gives the z.
  - `pull_image` is the catalog ref actually pulled. When an IDMS, ICSP or
    ITMS entry redirects the CatalogSource image, it is the mirror location;
    otherwise it is the image itself.
  - An operator belongs to the catalog that its InstallPlan installed the
    current CSV from. When no InstallPlan is found, it falls back to the
    Subscription's source.
  - `name` is the package name, `channel` is the Subscription's channel, and
    `version` is the installed CSV's `spec.version`. These match `opm`'s
    `package`, channel `name` and bundle `version`. The same package,
    channel and version installed in several namespaces is listed once.
  - `max_ocp_version` is the `olm.maxOpenShiftVersion` the installed CSV
    declares (from its `olm.properties` annotation, the same one OLM reads),
    or `""` when it declares none. If it is lower than a release on
    `ocp_path`, OLM blocks the cluster upgrade to that release until the
    operator is upgraded. The playbook reports that as a CRITICAL
    "Operator max OpenShift version" finding.
  - `main: false` marks a sub-operator that another operator pulled in.
    Either OLM created its Subscription to satisfy a dependency (label
    `olm.managed: "true"`), or a parent listed for it in
    `catalog_suboperator_parents` is subscribed. That map covers operators
    whose own controller creates the Subscription, such as ACM creating
    `multicluster-engine`; add others as needed.
  - `required_by` lists the packages that declare `olm.package.required` on
    this one, from the InstallPlans, plus the configured parents.
  - Subscriptions with nothing installed yet, or whose CatalogSource no
    longer exists, are left out because they have no version or no image.
    The mirror findings are in the report (section 14).
- On a connected cluster (no IDMS, ICSP or ITMS), the tables are still built but
  nothing is flagged, because default catalogs are expected there.

## OpenShift Virtualization notes

The VM node-drain-readiness matrix (section 11 of the report) is built entirely
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
- `upgrade_channel`, unlike `upgrade_target_version`, **is** auto-forwarded to
  every cascaded child by default (see "child processes do NOT inherit your
  outer command line" below) - a channel like `eus` is relative and each
  child resolves it against its own `current_version`, so the same channel
  string is correct even across spokes on different versions. Set
  `acm_cascade_upgrade_channel` if cascaded clusters should use a *different*
  channel than the hub itself.
- Credential material (kubeconfig files, extra-vars files with tokens in
  them) is written to a scratch directory under `report_output_dir` for the
  duration of the cascade and deleted immediately after - if a run is killed
  mid-cascade, check `<report_output_dir>/.acm-cascade-scratch/` before
  assuming it's gone.
- **The cascade's child processes do NOT inherit your outer command line.**
  Each cascaded cluster is a brand-new `ansible-playbook` process (see Layer 3
  above) with its own argv - `-e` flags, environment variables, and anything
  else you passed to the outer run are invisible to it unless this playbook
  explicitly writes them into that child's extra-vars file. Two exceptions
  are forwarded automatically:
  - `ansible_python_interpreter` - whatever value the outer run resolved
    (your own `-e ansible_python_interpreter=...` override, or the
    inventory's `{{ ansible_playbook_python }}` default) is captured and
    passed to every child, so if you're running with a pyenv/virtualenv
    Python that has `kubernetes`/`openshift` pip-installed (and
    `ansible-playbook` itself isn't launched from inside that venv, so
    Ansible can't discover it on its own), you only need `-e
    ansible_python_interpreter=/path/to/venv/bin/python` on the *outer*
    command - it now reaches every cascaded cluster too.
  - `upgrade_channel` - e.g. `-e upgrade_channel=eus` on the outer run is
    forwarded to every cascaded child as-is, and each child resolves it
    independently against its own `current_version` (see
    [Upgrade channel resolution](#upgrade-channel-resolution-eus-aware)
    above for why a relative channel forwards safely where a fixed
    `upgrade_target_version` doesn't). **Fixed in this project on
    2026-08-25** - earlier deliveries of the ACM cascade silently dropped
    `upgrade_channel` for every cascaded cluster (only `ansible_python_interpreter`
    was forwarded), so the upgrade-path section never appeared in any
    per-cluster report even when the hub run set `upgrade_channel` explicitly.
    If you're on an older delivery of this project, re-pull `tasks/85_acm.yml`
    to pick up the fix.

  If you need to forward anything else into the child runs (a different var,
  a proxy setting), or want a *different* value than what's auto-forwarded
  for either of the two above, set `acm_cascade_extra_vars` in
  `group_vars/all.yml` (or `-e acm_cascade_extra_vars='{"key":"value"}'`) -
  it's merged on top of everything else per-cluster, so it can override the
  interpreter, or `acm_cascade_upgrade_channel` specifically for `upgrade_channel`.

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
python3 tests/render_report_preview.py   # renders the templates with synthetic data -> tests/preview_out/,
                                         # then again through ansible-playbook when it is installed
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
tasks/65a_finalizer_scan_one.yml     included per CRD kind from 65 (keeps only objects being deleted)
tasks/70_portworx.yml
tasks/80_openshift_virtualization.yml
tasks/85_acm.yml                   ACM hub health, managed-cluster inventory, cascade
tasks/85a_acm_wait_msa_secret.yml    included per-cluster from 85_acm.yml
tasks/87_odf.yml                   OpenShift Data Foundation (ODF) + Ceph/OSD checks
tasks/88_cluster_operators_installed.yml   writes outputs/<cluster>/operators/cluster_operators_installed.json + .md
tasks/89_catalog_opm_render.yml    per-catalog opm render -> outputs/<cluster>/operators/<catalog>_<tag>.json
tasks/89a_catalog_opm_render_one.yml included per catalog from 89
tasks/89b_catalog_mirror_check.yml IDMS/ICSP/ITMS vs default catalogs and InstallPlans -> outputs/<cluster>/operators/catalog_mirror_check.json
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
excludes everything generated by a run (`outputs/`, the ACM cascade's
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

## License

Licensed under the Apache License, Version 2.0 - see [LICENSE](LICENSE) for
the full text. Apache 2.0 was chosen (over, say, MIT) because it includes an
explicit patent grant, matching the convention used across the
Kubernetes/OpenShift ecosystem this playbook operates against (kubectl, OLM,
the `kubernetes.core` and `redhat.openshift` Ansible collections, etc.).
