"""
Azure Resource Manager - Backend API
Uses Azure Python SDK with DefaultAzureCredential.
Supports: Managed Identity (Azure VM), Azure CLI (local dev), Environment variables.
Multi-subscription support.
"""

import logging
from flask import Flask, jsonify, request
from flask_cors import CORS

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.web import WebSiteManagementClient

try:
    from azure.mgmt.resource import SubscriptionClient
except ImportError:
    from azure.mgmt.resource.subscriptions import SubscriptionClient

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── AZURE AUTH ──────────────────────────────────────────────────
credential = DefaultAzureCredential()


import time

# Track resources that are transitioning (key: "type:rg:name", value: {"action": "starting"|"stopping", "since": timestamp})
_transitioning = {}
TRANSITION_TTL = 600  # Max 10 min to consider a resource as transitioning


def mark_transitioning(res_type: str, rg: str, name: str, action: str):
    key = f"{res_type}:{rg}:{name}".lower()
    _transitioning[key] = {"action": action, "since": time.time()}


def get_effective_status(res_type: str, rg: str, name: str, real_status: str) -> str:
    """Override status if resource was recently triggered and real status hasn't changed yet."""
    key = f"{res_type}:{rg}:{name}".lower()
    if key not in _transitioning:
        return real_status

    info = _transitioning[key]
    elapsed = time.time() - info["since"]

    # TTL expired — remove from tracking
    if elapsed > TRANSITION_TTL:
        del _transitioning[key]
        return real_status

    expected_action = info["action"]  # "starting" or "stopping"

    # Check if real status now matches the expected final state
    s = real_status.lower()
    if expected_action == "starting" and s in ("running", "started"):
        del _transitioning[key]
        return real_status
    if expected_action == "stopping" and s in ("stopped", "deallocated"):
        del _transitioning[key]
        return real_status

    # Still transitioning — return the transitioning status
    return expected_action.capitalize()


def get_sub_from_request() -> str:
    """Get subscription_id from query param ?sub=xxx"""
    sub = request.args.get("sub", "").strip()
    if not sub:
        raise ValueError("Missing 'sub' query parameter")
    return sub


def get_compute_client(sub_id: str) -> ComputeManagementClient:
    return ComputeManagementClient(credential, sub_id)


def get_container_client(sub_id: str) -> ContainerServiceClient:
    return ContainerServiceClient(credential, sub_id)


def get_web_client(sub_id: str) -> WebSiteManagementClient:
    return WebSiteManagementClient(credential, sub_id)


import re

def extract_resource_group(resource_id: str) -> str:
    """Extract resource group from Azure resource ID (case-insensitive)."""
    match = re.search(r"/resourceGroups/([^/]+)", resource_id, re.IGNORECASE)
    if not match:
        raise ValueError(f"Cannot extract RG from: {resource_id}")
    return match.group(1)


# ─── LIST SUBSCRIPTIONS ─────────────────────────────────────────

# Only show these subscriptions in the dashboard
ALLOWED_SUBSCRIPTIONS = {
    "163c1284-7060-4c7f-822f-efc086bbf95e",  # VS Enterprise 2025 – Nita Oktaviani
    "c943efc2-430f-4c7c-a428-293d6fb2c352",  # Visual Studio Enterprise – Ian Paulus Sinambela MPN
}


@app.route("/api/subscriptions", methods=["GET"])
def list_subscriptions():
    """List allowed subscriptions."""
    try:
        sub_client = SubscriptionClient(credential)
        subs = []
        for sub in sub_client.subscriptions.list():
            if sub.subscription_id in ALLOWED_SUBSCRIPTIONS:
                subs.append({
                    "id": sub.subscription_id,
                    "name": sub.display_name,
                    "state": sub.state.value if sub.state else "Unknown",
                })
        return jsonify(subs)
    except Exception as e:
        logger.error("Failed to list subscriptions: %s", e)
        return jsonify({"error": str(e)}), 500


# ─── LIST RESOURCES ──────────────────────────────────────────────

@app.route("/api/resources", methods=["GET"])
def list_all_resources():
    """List all VMs, AKS clusters, and App Services. Requires ?sub=<subscription_id>"""
    try:
        sub_id = get_sub_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    vms = get_vms(sub_id)
    aks = get_aks_clusters(sub_id)
    apps = get_app_services(sub_id)
    logger.info("Loaded: %d VMs, %d AKS, %d Apps for sub %s", len(vms), len(aks), len(apps), sub_id)
    return jsonify({"vms": vms, "aks": aks, "appServices": apps})


def get_vms(sub_id: str) -> list[dict]:
    try:
        client = get_compute_client(sub_id)
        results = []
        for vm in client.virtual_machines.list_all():
            try:
                rg = extract_resource_group(vm.id)
            except (ValueError, AttributeError):
                continue
            iv = client.virtual_machines.instance_view(rg, vm.name)
            power_state = "Unknown"
            for status in (iv.statuses or []):
                if status.code and status.code.startswith("PowerState/"):
                    power_state = status.code.replace("PowerState/", "")
                    break
            results.append({
                "id": vm.id,
                "name": vm.name,
                "resourceGroup": rg,
                "location": vm.location,
                "status": get_effective_status("vm", rg, vm.name, power_state),
                "type": "VM",
            })
        return results
    except Exception as e:
        logger.error("Failed to list VMs: %s", e)
        return []


def get_aks_clusters(sub_id: str) -> list[dict]:
    try:
        client = get_container_client(sub_id)
        results = []
        for cluster in client.managed_clusters.list():
            logger.info("Found AKS: name=%s, id=%s", cluster.name, cluster.id)
            try:
                rg = extract_resource_group(cluster.id)
            except (ValueError, AttributeError):
                logger.warning("Skipping AKS with invalid ID: %s", cluster.id)
                continue
            # Get fresh status per cluster
            try:
                detail = client.managed_clusters.get(rg, cluster.name)
                power = "Unknown"
                if hasattr(detail, 'power_state') and detail.power_state:
                    power = detail.power_state.code or "Unknown"
            except Exception:
                power = "Unknown"
            results.append({
                "id": cluster.id,
                "name": cluster.name,
                "resourceGroup": rg,
                "location": cluster.location,
                "status": get_effective_status("aks", rg, cluster.name, power),
                "type": "AKS",
            })
        logger.info("AKS total found: %d", len(results))
        return results
    except Exception as e:
        logger.error("Failed to list AKS: %s", e, exc_info=True)
        return []


def get_app_services(sub_id: str) -> list[dict]:
    try:
        client = get_web_client(sub_id)
        results = []
        for site in client.web_apps.list():
            try:
                rg = extract_resource_group(site.id)
            except (ValueError, AttributeError):
                continue
            results.append({
                "id": site.id,
                "name": site.name,
                "resourceGroup": rg,
                "location": site.location,
                "status": get_effective_status("appservice", rg, site.name, site.state or "Unknown"),
                "type": "AppService",
            })
        return results
    except Exception as e:
        logger.error("Failed to list App Services: %s", e)
        return []


# ─── START / STOP INDIVIDUAL ─────────────────────────────────────

@app.route("/api/vm/<resource_group>/<name>/start", methods=["POST"])
def start_vm(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_compute_client(sub_id)
        client.virtual_machines.begin_start(resource_group, name)
        mark_transitioning("vm", resource_group, name, "starting")
        return jsonify({"success": True, "message": f"VM {name} starting..."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<resource_group>/<name>/stop", methods=["POST"])
def stop_vm(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_compute_client(sub_id)
        client.virtual_machines.begin_deallocate(resource_group, name)
        mark_transitioning("vm", resource_group, name, "stopping")
        return jsonify({"success": True, "message": f"VM {name} stopping..."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/aks/<resource_group>/<name>/start", methods=["POST"])
def start_aks(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_container_client(sub_id)
        client.managed_clusters.begin_start(resource_group, name)
        mark_transitioning("aks", resource_group, name, "starting")
        return jsonify({"success": True, "message": f"AKS {name} starting... (takes 3-10 min)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/aks/<resource_group>/<name>/stop", methods=["POST"])
def stop_aks(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_container_client(sub_id)
        client.managed_clusters.begin_stop(resource_group, name)
        mark_transitioning("aks", resource_group, name, "stopping")
        return jsonify({"success": True, "message": f"AKS {name} stopping... (takes 3-10 min)"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/appservice/<resource_group>/<name>/start", methods=["POST"])
def start_app_service(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_web_client(sub_id)
        client.web_apps.start(resource_group, name)
        return jsonify({"success": True, "message": f"App Service {name} started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/appservice/<resource_group>/<name>/stop", methods=["POST"])
def stop_app_service(resource_group, name):
    try:
        sub_id = get_sub_from_request()
        client = get_web_client(sub_id)
        client.web_apps.stop(resource_group, name)
        return jsonify({"success": True, "message": f"App Service {name} stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── BULK OPERATIONS ─────────────────────────────────────────────

@app.route("/api/all/start", methods=["POST"])
def start_all():
    try:
        sub_id = get_sub_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    results = {"vms": [], "aks": [], "appServices": []}
    compute = get_compute_client(sub_id)
    container = get_container_client(sub_id)
    web = get_web_client(sub_id)

    for vm in get_vms(sub_id):
        try:
            compute.virtual_machines.begin_start(vm["resourceGroup"], vm["name"])
            mark_transitioning("vm", vm["resourceGroup"], vm["name"], "starting")
            results["vms"].append({"name": vm["name"], "success": True})
        except Exception as e:
            results["vms"].append({"name": vm["name"], "error": str(e)})

    for cluster in get_aks_clusters(sub_id):
        try:
            container.managed_clusters.begin_start(cluster["resourceGroup"], cluster["name"])
            mark_transitioning("aks", cluster["resourceGroup"], cluster["name"], "starting")
            results["aks"].append({"name": cluster["name"], "success": True})
        except Exception as e:
            results["aks"].append({"name": cluster["name"], "error": str(e)})

    for svc in get_app_services(sub_id):
        try:
            web.web_apps.start(svc["resourceGroup"], svc["name"])
            mark_transitioning("appservice", svc["resourceGroup"], svc["name"], "starting")
            results["appServices"].append({"name": svc["name"], "success": True})
        except Exception as e:
            results["appServices"].append({"name": svc["name"], "error": str(e)})

    return jsonify(results)


@app.route("/api/all/stop", methods=["POST"])
def stop_all():
    try:
        sub_id = get_sub_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    results = {"vms": [], "aks": [], "appServices": []}
    compute = get_compute_client(sub_id)
    container = get_container_client(sub_id)
    web = get_web_client(sub_id)

    for vm in get_vms(sub_id):
        try:
            compute.virtual_machines.begin_deallocate(vm["resourceGroup"], vm["name"])
            mark_transitioning("vm", vm["resourceGroup"], vm["name"], "stopping")
            results["vms"].append({"name": vm["name"], "success": True})
        except Exception as e:
            results["vms"].append({"name": vm["name"], "error": str(e)})

    for cluster in get_aks_clusters(sub_id):
        try:
            container.managed_clusters.begin_stop(cluster["resourceGroup"], cluster["name"])
            mark_transitioning("aks", cluster["resourceGroup"], cluster["name"], "stopping")
            results["aks"].append({"name": cluster["name"], "success": True})
        except Exception as e:
            results["aks"].append({"name": cluster["name"], "error": str(e)})

    for svc in get_app_services(sub_id):
        try:
            web.web_apps.stop(svc["resourceGroup"], svc["name"])
            mark_transitioning("appservice", svc["resourceGroup"], svc["name"], "stopping")
            results["appServices"].append({"name": svc["name"], "success": True})
        except Exception as e:
            results["appServices"].append({"name": svc["name"], "error": str(e)})

    return jsonify(results)


# ─── BULK BY TYPE ─────────────────────────────────────────────────

@app.route("/api/<resource_type>/start-all", methods=["POST"])
def start_all_by_type(resource_type):
    """Start all resources of a given type."""
    return _bulk_action(resource_type, "start")


@app.route("/api/<resource_type>/stop-all", methods=["POST"])
def stop_all_by_type(resource_type):
    """Stop all resources of a given type."""
    return _bulk_action(resource_type, "stop")


def _bulk_action(resource_type: str, action: str):
    allowed_types = {"vm", "aks", "appservice"}
    if resource_type not in allowed_types:
        return jsonify({"error": f"Invalid type. Use: {allowed_types}"}), 400

    try:
        sub_id = get_sub_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    results = []

    if resource_type == "vm":
        client = get_compute_client(sub_id)
        for res in get_vms(sub_id):
            try:
                if action == "start":
                    client.virtual_machines.begin_start(res["resourceGroup"], res["name"])
                else:
                    client.virtual_machines.begin_deallocate(res["resourceGroup"], res["name"])
                results.append({"name": res["name"], "success": True})
            except Exception as e:
                results.append({"name": res["name"], "error": str(e)})

    elif resource_type == "aks":
        client = get_container_client(sub_id)
        for res in get_aks_clusters(sub_id):
            try:
                if action == "start":
                    client.managed_clusters.begin_start(res["resourceGroup"], res["name"])
                else:
                    client.managed_clusters.begin_stop(res["resourceGroup"], res["name"])
                results.append({"name": res["name"], "success": True})
            except Exception as e:
                results.append({"name": res["name"], "error": str(e)})

    else:  # appservice
        client = get_web_client(sub_id)
        for res in get_app_services(sub_id):
            try:
                if action == "start":
                    client.web_apps.start(res["resourceGroup"], res["name"])
                else:
                    client.web_apps.stop(res["resourceGroup"], res["name"])
                results.append({"name": res["name"], "success": True})
            except Exception as e:
                results.append({"name": res["name"], "error": str(e)})

    return jsonify(results)


# ─── SERVE FRONTEND ──────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
