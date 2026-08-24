"""Synthetic OpenShift API objects used to exercise the filter plugins and
templates without needing a live cluster. Shapes mirror real `oc get -o json`
output closely enough to validate the filter logic and Jinja rendering.
"""

CLUSTERVERSION = {
    "spec": {"clusterID": "11111111-2222-3333-4444-555555555555", "channel": "stable-4.16"},
    "status": {
        "desired": {"version": "4.16.20"},
        "history": [
            {"version": "4.16.20", "state": "Completed", "startedTime": "2026-08-01T00:00:00Z",
             "completionTime": "2026-08-01T01:00:00Z", "verified": True},
            {"version": "4.16.18", "state": "Completed", "startedTime": "2026-06-01T00:00:00Z",
             "completionTime": "2026-06-01T01:00:00Z", "verified": True},
        ],
        "conditions": [
            {"type": "Available", "status": "True"},
            {"type": "Progressing", "status": "False"},
            {"type": "Failing", "status": "False"},
            {"type": "RetrievedUpdates", "status": "True"},
        ],
        "availableUpdates": [{"version": "4.17.14"}],
        "conditionalUpdates": [],
    },
}

NODES = [
    {
        "metadata": {
            "name": "master-0",
            "labels": {"node-role.kubernetes.io/master": "", "node-role.kubernetes.io/control-plane": ""},
            "annotations": {
                "machineconfiguration.openshift.io/currentConfig": "rendered-master-abc123",
                "machineconfiguration.openshift.io/desiredConfig": "rendered-master-abc123",
                "machineconfiguration.openshift.io/state": "Done",
            },
        },
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    {
        "metadata": {
            "name": "worker-0",
            "labels": {"node-role.kubernetes.io/worker": ""},
            "annotations": {
                "machineconfiguration.openshift.io/currentConfig": "rendered-worker-old111",
                "machineconfiguration.openshift.io/desiredConfig": "rendered-worker-new222",
                "machineconfiguration.openshift.io/state": "Working",
            },
        },
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    {
        "metadata": {
            "name": "worker-1",
            "labels": {"node-role.kubernetes.io/worker": "", "px/service": "stop"},
            "annotations": {
                "machineconfiguration.openshift.io/currentConfig": "rendered-worker-new222",
                "machineconfiguration.openshift.io/desiredConfig": "rendered-worker-new222",
                "machineconfiguration.openshift.io/state": "Done",
            },
        },
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
]

MACHINECONFIGPOOLS = [
    {
        "metadata": {"name": "master"},
        "spec": {"nodeSelector": {"matchLabels": {"node-role.kubernetes.io/master": ""}}},
        "status": {
            "configuration": {"name": "rendered-master-abc123"},
            "machineCount": 1, "readyMachineCount": 1, "updatedMachineCount": 1,
            "unavailableMachineCount": 0, "degradedMachineCount": 0,
            "conditions": [{"type": "Updated", "status": "True"}, {"type": "Updating", "status": "False"},
                            {"type": "Degraded", "status": "False"}],
        },
    },
    {
        "metadata": {"name": "worker"},
        "spec": {"nodeSelector": {"matchLabels": {"node-role.kubernetes.io/worker": ""}}},
        "status": {
            "configuration": {"name": "rendered-worker-new222"},
            "machineCount": 2, "readyMachineCount": 1, "updatedMachineCount": 1,
            "unavailableMachineCount": 1, "degradedMachineCount": 0,
            "conditions": [{"type": "Updated", "status": "False"}, {"type": "Updating", "status": "True"},
                            {"type": "Degraded", "status": "False"}],
        },
    },
]

CLUSTEROPERATORS = [
    {"metadata": {"name": "authentication"},
     "status": {"versions": [{"name": "operator", "version": "4.16.20"}],
                "conditions": [{"type": "Available", "status": "True"}, {"type": "Progressing", "status": "False"},
                                {"type": "Degraded", "status": "False"}, {"type": "Upgradeable", "status": "True"}]}},
    {"metadata": {"name": "storage"},
     "status": {"versions": [{"name": "operator", "version": "4.16.20"}],
                "conditions": [{"type": "Available", "status": "False", "message": "waiting for deployment"},
                                {"type": "Progressing", "status": "True"},
                                {"type": "Degraded", "status": "True", "message": "1 of 3 pods unavailable"},
                                {"type": "Upgradeable", "status": "True"}]}},
]

MACHINESETS = [
    {"metadata": {"name": "cluster-worker-us-east-1a", "namespace": "openshift-machine-api"},
     "spec": {"replicas": 2},
     "status": {"replicas": 2, "readyReplicas": 2, "availableReplicas": 2}},
    {"metadata": {"name": "cluster-worker-us-east-1b", "namespace": "openshift-machine-api"},
     "spec": {"replicas": 1},
     "status": {"replicas": 1, "readyReplicas": 0, "availableReplicas": 0}},
]

MACHINES = [
    {"metadata": {"name": "cluster-worker-us-east-1a-1", "namespace": "openshift-machine-api",
                  "labels": {"machine.openshift.io/cluster-api-machineset": "cluster-worker-us-east-1a"}},
     "status": {"phase": "Running", "nodeRef": {"name": "worker-0"}}},
    {"metadata": {"name": "cluster-worker-us-east-1a-2", "namespace": "openshift-machine-api",
                  "labels": {"machine.openshift.io/cluster-api-machineset": "cluster-worker-us-east-1a"}},
     "status": {"phase": "Running", "nodeRef": {"name": "worker-1"}}},
    {"metadata": {"name": "cluster-worker-us-east-1b-1", "namespace": "openshift-machine-api",
                  "labels": {"machine.openshift.io/cluster-api-machineset": "cluster-worker-us-east-1b"}},
     "status": {"phase": "Provisioning"}},
]

HYPERCONVERGED = [
    {"metadata": {"name": "kubevirt-hyperconverged", "namespace": "openshift-cnv"},
     "status": {"versions": [{"name": "operator", "version": "4.16.5"}],
                "conditions": [{"type": "Available", "status": "True"}, {"type": "Progressing", "status": "False"},
                                {"type": "Degraded", "status": "False"}, {"type": "Upgradeable", "status": "True"}]}},
]

KUBEVIRT = [
    {"metadata": {"name": "kubevirt-kubevirt-hyperconverged", "namespace": "openshift-cnv"},
     "spec": {"workloadUpdateStrategy": {"workloadUpdateMethods": ["LiveMigrate"]}},
     "status": {"versions": [{"name": "operator", "version": "4.16.5"}],
                "conditions": [{"type": "Available", "status": "True"}, {"type": "Progressing", "status": "False"},
                                {"type": "Degraded", "status": "False"}, {"type": "Upgradeable", "status": "True"}],
                "outdatedVirtualMachineInstanceWorkloads": 1}},
]

VMIS = [
    {"metadata": {"name": "web-vm-1", "namespace": "apps"},
     "spec": {"evictionStrategy": "LiveMigrate", "domain": {"devices": {"disks": [{"name": "rootdisk", "disk": {"bus": "virtio"}}]}}},
     "status": {"phase": "Running", "nodeName": "worker-0",
                "conditions": [{"type": "LiveMigratable", "status": "True"}]}},
    {"metadata": {"name": "installer-vm", "namespace": "apps"},
     "spec": {"evictionStrategy": "LiveMigrate",
              "domain": {"devices": {"disks": [{"name": "rootdisk", "disk": {"bus": "virtio"}},
                                                {"name": "cdrom-iso", "cdrom": {"bus": "sata", "readonly": True}}]}}},
     "status": {"phase": "Running", "nodeName": "worker-1",
                "conditions": [{"type": "LiveMigratable", "status": "True"}]}},
    {"metadata": {"name": "hostpath-vm", "namespace": "legacy-app"},
     "spec": {"evictionStrategy": "LiveMigrate", "domain": {"devices": {"disks": [{"name": "rootdisk", "disk": {"bus": "virtio"}}]}}},
     "status": {"phase": "Running", "nodeName": "worker-1",
                "conditions": [{"type": "LiveMigratable", "status": "False", "reason": "NotMigratable",
                                 "message": "cannot migrate VMI with hostpath-provisioner storage"}]}},
    {"metadata": {"name": "shutdown-ok-vm", "namespace": "legacy-app"},
     "spec": {"evictionStrategy": "None", "domain": {"devices": {"disks": [{"name": "rootdisk", "disk": {"bus": "virtio"}}]}}},
     "status": {"phase": "Running", "nodeName": "worker-0",
                "conditions": [{"type": "LiveMigratable", "status": "False", "reason": "NotMigratable",
                                 "message": "cannot migrate VMI with hostpath-provisioner storage"}]}},
]

VMIMS = [
    {"metadata": {"name": "web-vm-1-migration", "namespace": "apps"},
     "spec": {"vmiName": "web-vm-1"}, "status": {"phase": "Succeeded"}},
    {"metadata": {"name": "hostpath-vm-migration", "namespace": "legacy-app"},
     "spec": {"vmiName": "hostpath-vm"}, "status": {"phase": "Failed"}},
]

ETCD_PODS = [
    {"metadata": {"name": "etcd-master-0"}, "spec": {"nodeName": "master-0"},
     "status": {"phase": "Running", "containerStatuses": [{"name": "etcd", "ready": True}, {"name": "etcdctl", "ready": True}]}},
    {"metadata": {"name": "etcd-master-1"}, "spec": {"nodeName": "master-1"},
     "status": {"phase": "Running", "containerStatuses": [{"name": "etcd", "ready": True}, {"name": "etcdctl", "ready": True}]}},
    {"metadata": {"name": "etcd-master-2"}, "spec": {"nodeName": "master-2"},
     "status": {"phase": "Running", "containerStatuses": [{"name": "etcd", "ready": False}, {"name": "etcdctl", "ready": True}]}},
]

ETCD_HEALTH_OK = [
    {"endpoint": "https://10.0.0.1:2379", "health": True, "took": "12.34ms"},
    {"endpoint": "https://10.0.0.2:2379", "health": True, "took": "9.1ms"},
    {"endpoint": "https://10.0.0.3:2379", "health": True, "took": "15.0ms"},
]

ETCD_HEALTH_SLOW_AND_DOWN = [
    {"endpoint": "https://10.0.0.1:2379", "health": True, "took": "12.34ms"},
    {"endpoint": "https://10.0.0.2:2379", "health": True, "took": "120.0ms"},   # slow -> WARNING
    {"endpoint": "https://10.0.0.3:2379", "health": False, "took": "", "error": "context deadline exceeded"},  # down -> CRITICAL
]

ETCD_STATUS_OK = [
    {"Endpoint": "https://10.0.0.1:2379", "Status": {"version": "3.5.9", "dbSize": 200000000, "dbSizeInUse": 150000000, "leader": 111, "raftTerm": 5, "isLearner": False}},
    {"Endpoint": "https://10.0.0.2:2379", "Status": {"version": "3.5.9", "dbSize": 205000000, "dbSizeInUse": 150000000, "leader": 111, "raftTerm": 5, "isLearner": False}},
    {"Endpoint": "https://10.0.0.3:2379", "Status": {"version": "3.5.9", "dbSize": 198000000, "dbSizeInUse": 150000000, "leader": 111, "raftTerm": 5, "isLearner": False}},
]

ETCD_STATUS_DB_NEAR_QUOTA = [
    {"Endpoint": "https://10.0.0.1:2379", "Status": {"version": "3.5.9", "dbSize": 8200000000, "dbSizeInUse": 8000000000, "leader": 111, "raftTerm": 5, "isLearner": False}},
]

ETCD_STATUS_SPLIT_LEADER = [
    {"Endpoint": "https://10.0.0.1:2379", "Status": {"version": "3.5.9", "dbSize": 200000000, "leader": 111, "raftTerm": 5, "isLearner": False}},
    {"Endpoint": "https://10.0.0.2:2379", "Status": {"version": "3.5.9", "dbSize": 200000000, "leader": 222, "raftTerm": 5, "isLearner": False}},
]

ETCD_ALARM_NONE = ""
ETCD_ALARM_NOSPACE = "memberID:12345678901234567890 alarm:NOSPACE"

NOW_ISO = "2026-08-23T20:30:00Z"

NAMESPACES = [
    {"metadata": {"name": "apps"}, "status": {"phase": "Active"}},
    {"metadata": {"name": "stuck-ns", "deletionTimestamp": "2026-08-23T19:50:00Z",
                  "finalizers": ["kubernetes"]},
     "status": {"phase": "Terminating"}},
    {"metadata": {"name": "just-deleting-ns", "deletionTimestamp": "2026-08-23T20:29:50Z",
                  "finalizers": ["kubernetes"]},
     "status": {"phase": "Terminating"}},
]

PERSISTENTVOLUMES = [
    {"metadata": {"name": "pv-ok"}, "status": {"phase": "Bound"}},
]

PERSISTENTVOLUMECLAIMS = [
    {"metadata": {"name": "stuck-pvc", "namespace": "legacy-app",
                  "deletionTimestamp": "2026-08-23T19:00:00Z",
                  "finalizers": ["kubernetes.io/pvc-protection"]},
     "status": {"phase": "Terminating"}},
]

CRDS = [
    {"metadata": {"name": "widgets.example.com"},
     "spec": {"group": "example.com", "scope": "Namespaced", "names": {"kind": "Widget", "plural": "widgets"},
              "versions": [{"name": "v1", "served": True, "storage": True}]},
     "status": {"conditions": [{"type": "Established", "status": "True"}]}},
    {"metadata": {"name": "gizmos.example.com"},
     "spec": {"group": "example.com", "scope": "Namespaced", "names": {"kind": "Gizmo", "plural": "gizmos"},
              "versions": [{"name": "v1", "served": True, "storage": True}]},
     "status": {"conditions": [{"type": "Established", "status": "False"}]}},  # never established -> skipped
]

# Simulates the registered result of looping kubernetes.core.k8s_info over crd_scan_targets()
CRD_SCAN_RESULTS = [
    {"item": {"crd_name": "widgets.example.com", "group": "example.com", "version": "v1", "kind": "Widget", "scope": "Namespaced"},
     "failed": False,
     "resources": [
         {"metadata": {"name": "widget-stuck", "namespace": "apps",
                       "deletionTimestamp": "2026-08-23T20:00:00Z",
                       "finalizers": ["example.com/cleanup"]}},
         {"metadata": {"name": "widget-fine", "namespace": "apps"}},
     ]},
    {"item": {"crd_name": "unreadable.example.com", "group": "example.com", "version": "v1", "kind": "Unreadable", "scope": "Namespaced"},
     "failed": True, "resources": []},
]

APIREQUESTCOUNTS = [
    {"metadata": {"name": "cronjobs.v1beta1.batch"},
     "status": {
         "removedInRelease": "1.25",
         "requestCount": 42,
         "currentHour": {"byNode": [{"nodeName": "master-0", "byUser": [
             {"username": "system:serviceaccount:legacy-app:controller", "requestCount": 40,
              "byVerb": [{"verb": "list", "requestCount": 40}]},
         ]}]},
         "last24h": [],
     }},
    {"metadata": {"name": "poddisruptionbudgets.v1beta1.policy"},
     "status": {
         "removedInRelease": "1.25",
         "requestCount": 5,
         "currentHour": {"byNode": [{"nodeName": "master-0", "byUser": [
             {"username": "system:serviceaccount:openshift-monitoring:prometheus-k8s", "requestCount": 5,
              "byVerb": [{"verb": "get", "requestCount": 5}]},
         ]}]},
         "last24h": [],
     }},
    {"metadata": {"name": "pods.v1"},
     "status": {"removedInRelease": "", "requestCount": 100000, "currentHour": {}, "last24h": []}},
]

# ---- ACM (Advanced Cluster Management) --------------------------------------
import base64 as _base64  # noqa: E402


def _b64(s: str) -> str:
    return _base64.b64encode(s.encode("utf-8")).decode("ascii")


MCH_RUNNING = [
    {
        "metadata": {"name": "multiclusterhub", "namespace": "open-cluster-management"},
        "status": {
            "phase": "Running",
            "currentVersion": "2.10.0",
            "conditions": [{"type": "Complete", "status": "True", "message": "All components are available"}],
        },
    }
]

MCH_ERROR = [
    {
        "metadata": {"name": "multiclusterhub", "namespace": "open-cluster-management"},
        "status": {
            "phase": "Error",
            "currentVersion": "2.10.0",
            "conditions": [{"type": "Complete", "status": "False", "message": "grc component unavailable"}],
        },
    }
]

MANAGED_CLUSTERS = [
    {
        "metadata": {"name": "local-cluster", "labels": {"openshiftVersion": "4.16.20", "vendor": "OpenShift", "cloud": "Amazon"}},
        "spec": {"managedClusterClientConfigs": [{"url": "https://api.hub.example.com:6443"}]},
        "status": {
            "version": {"kubernetes": "v1.29.6"},
            "conditions": [
                {"type": "ManagedClusterConditionAvailable", "status": "True"},
                {"type": "HubAcceptedManagedCluster", "status": "True"},
                {"type": "ManagedClusterJoined", "status": "True"},
            ],
        },
    },
    {
        "metadata": {"name": "spoke-hive", "labels": {"openshiftVersion": "4.16.18", "vendor": "OpenShift", "cloud": "Amazon"}},
        "spec": {"managedClusterClientConfigs": [{"url": "https://api.spoke-hive.example.com:6443"}]},
        "status": {
            "version": {"kubernetes": "v1.29.5"},
            "conditions": [
                {"type": "ManagedClusterConditionAvailable", "status": "True"},
                {"type": "HubAcceptedManagedCluster", "status": "True"},
                {"type": "ManagedClusterJoined", "status": "True"},
            ],
        },
    },
    {
        "metadata": {"name": "spoke-msa", "labels": {"openshiftVersion": "4.15.30", "vendor": "OpenShift", "cloud": "Azure"}},
        "spec": {"managedClusterClientConfigs": [{"url": "https://api.spoke-msa.example.com:6443"}]},
        "status": {
            "version": {"kubernetes": "v1.28.9"},
            "conditions": [
                {"type": "ManagedClusterConditionAvailable", "status": "True"},
                {"type": "HubAcceptedManagedCluster", "status": "True"},
                {"type": "ManagedClusterJoined", "status": "True"},
            ],
        },
    },
    {
        "metadata": {"name": "spoke-down", "labels": {"openshiftVersion": "4.14.40", "vendor": "OpenShift", "cloud": "GCP"}},
        "spec": {"managedClusterClientConfigs": [{"url": "https://api.spoke-down.example.com:6443"}]},
        "status": {
            "version": {"kubernetes": "v1.27.13"},
            "conditions": [
                {"type": "ManagedClusterConditionAvailable", "status": "False"},
                {"type": "HubAcceptedManagedCluster", "status": "True"},
                {"type": "ManagedClusterJoined", "status": "True"},
            ],
        },
    },
]

# Simulated `.results` from a looped kubernetes.core.k8s_info over Secret
# lookups, one per candidate cluster name (`.item` = the loop item).
ACM_HIVE_SECRET_RESULTS = [
    {"item": "local-cluster", "resources": []},
    {"item": "spoke-hive", "resources": [{"data": {"kubeconfig": _b64("apiVersion: v1\nkind: Config\n# fake\n")}}]},
    {"item": "spoke-msa", "resources": []},
    {"item": "spoke-down", "resources": []},
]

ACM_MSA_SECRET_RESULTS = [
    {"item": "local-cluster", "resources": []},
    {"item": "spoke-msa", "resources": [{"data": {"token": _b64("fake-msa-token-xyz"), "ca.crt": _b64("fake-ca")}}]},
]

# ----------------------------------------------------------------------------
# Cincinnati /graph API responses, for resolve_upgrade_channel /
# cincinnati_shortest_path unit tests. Real Cincinnati EUS channel graphs
# never contain a direct edge from one EUS minor straight to the next - the
# fixture below deliberately mirrors that (4.18.14 -> ... -> 4.20.1 only via
# 4.19.x nodes), so a test that skipped the intermediate minor would fail.
CINCINNATI_GRAPH_EUS_4_20 = {
    "nodes": [
        {"version": "4.18.14"},  # 0 - matches CLUSTERVERSION-adjacent fixtures' current_version in some tests
        {"version": "4.18.15"},  # 1
        {"version": "4.19.0"},   # 2
        {"version": "4.19.5"},   # 3
        {"version": "4.19.8"},   # 4
        {"version": "4.20.0"},   # 5
        {"version": "4.20.1"},   # 6 - highest 4.20.z -> the expected resolved target
    ],
    "edges": [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6]],
}

# Same target minor (4.20) but the current version (4.18.14) isn't a node in
# this graph at all - simulates querying an eus-4.20 channel before the
# cluster has actually reached a version that channel's graph covers.
CINCINNATI_GRAPH_MISSING_CURRENT = {
    "nodes": [
        {"version": "4.19.0"},
        {"version": "4.19.8"},
        {"version": "4.20.0"},
        {"version": "4.20.1"},
    ],
    "edges": [[0, 1], [1, 2], [2, 3]],
}

# Current version present, target minor (4.20) has no z-stream in this graph
# yet (e.g. queried right after a channel switch, before 4.20 GA).
CINCINNATI_GRAPH_NO_TARGET_YET = {
    "nodes": [
        {"version": "4.18.14"},
        {"version": "4.18.15"},
        {"version": "4.19.0"},
    ],
    "edges": [[0, 1], [1, 2]],
}

# Current version and a 4.20.z node both present, but genuinely disconnected
# (no edge path between them) - e.g. a stale/partial graph snapshot.
CINCINNATI_GRAPH_DISCONNECTED = {
    "nodes": [
        {"version": "4.18.14"},  # 0
        {"version": "4.18.15"},  # 1 - reachable from 0, but a dead end
        {"version": "4.20.0"},   # 2 - the target, unreachable from 0
    ],
    "edges": [[0, 1]],
}

# Plain next-minor graph for a non-EUS ("stable") channel test.
CINCINNATI_GRAPH_STABLE_4_19 = {
    "nodes": [
        {"version": "4.18.14"},
        {"version": "4.18.15"},
        {"version": "4.19.0"},
        {"version": "4.19.5"},
    ],
    "edges": [[0, 1], [1, 2], [2, 3]],
}
