from contextvars import ContextVar
from utils.scan_context import CURRENT, record_error, protected
# scanner/ec2_scanner.py
import logging
import os
from datetime import datetime, timedelta, timezone

from utils.aws_factory import get_client
from utils.aws_helpers import (
    GLOBAL_NO_METRICS,
    get_region_from_az,
    chunk_list,
    log_and_skip,
    normalize_tags,
    reset_no_metrics,
)
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_ec2_client = None
_cloudwatch_client = None

CPU_THRESHOLD = None
_no_metrics_ids = ContextVar("legacy_ec2_skips", default=())

# 6-hour aggregation period for finer sensitivity over the 7-day window
METRIC_PERIOD = 21600
# Instances managed by an Auto Scaling Group are skipped (ASG handles scaling)
ASG_TAG = "aws:autoscaling:groupName"
# Maximum queries per get_metric_data call (AWS limit)
MAX_METRIC_QUERIES = 500
# Total bytes (NetworkIn + NetworkOut) tolerated over the window before an
# instance is considered "active". Configurable via NETWORK_THRESHOLD env var.
NETWORK_THRESHOLD = None


def get_no_metrics_count():
    """Return the count of instances skipped in the last scan due to missing metrics."""
    try:
        return len(_no_metrics_ids.get())
    except Exception:
        return GLOBAL_NO_METRICS.count()


def reset_no_metrics_state():
    _no_metrics_ids.set(())
    reset_no_metrics()


def _get_network_threshold():
    """Get network threshold (bytes) from env var with validation."""
    if CURRENT.get():
        return CURRENT.get().config.network_threshold
    global NETWORK_THRESHOLD
    if NETWORK_THRESHOLD is None:
        try:
            raw = os.getenv("NETWORK_THRESHOLD", "1000000")
            NETWORK_THRESHOLD = float(raw)
            if NETWORK_THRESHOLD < 0 or NETWORK_THRESHOLD == float("inf") or NETWORK_THRESHOLD != NETWORK_THRESHOLD:
                logger.warning(f"Invalid NETWORK_THRESHOLD {raw}, using default")
                NETWORK_THRESHOLD = 1000000.0
        except (ValueError, TypeError):
            logger.warning("Invalid NETWORK_THRESHOLD format, using default")
            NETWORK_THRESHOLD = 1000000.0
    return NETWORK_THRESHOLD


def _get_cpu_threshold():
    """Get CPU threshold from env var with validation."""
    if CURRENT.get():
        return CURRENT.get().config.cpu_threshold
    global CPU_THRESHOLD
    if CPU_THRESHOLD is None:
        try:
            raw = os.getenv("CPU_THRESHOLD", "2")
            CPU_THRESHOLD = float(raw)
            if CPU_THRESHOLD < 0 or CPU_THRESHOLD > 100 or CPU_THRESHOLD != CPU_THRESHOLD:
                logger.warning(f"Invalid CPU_THRESHOLD {raw}, using default 2")
                CPU_THRESHOLD = 2.0
        except (ValueError, TypeError):
            logger.warning("Invalid CPU_THRESHOLD format, using default 2")
            CPU_THRESHOLD = 2.0
    return CPU_THRESHOLD


def _get_ec2_client():
    """Lazy initialization of EC2 client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("ec2")
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = get_client("ec2")
    return _ec2_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("cloudwatch")
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = get_client("cloudwatch")
    return _cloudwatch_client


def _build_metric_queries(instance_ids):
    """Build GetMetricData queries for CPU and network stats per instance."""
    queries = []
    for idx, iid in enumerate(instance_ids):
        base_dimension = {"Name": "InstanceId", "Value": iid}
        queries.append({
            "Id": f"cpu_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [base_dimension],
                },
                "Period": METRIC_PERIOD,
                "Stat": "Average",
            },
            "ReturnData": True,
        })
        queries.append({
            "Id": f"netin_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "NetworkIn",
                    "Dimensions": [base_dimension],
                },
                "Period": METRIC_PERIOD,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })
        queries.append({
            "Id": f"netout_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "NetworkOut",
                    "Dimensions": [base_dimension],
                },
                "Period": METRIC_PERIOD,
                "Stat": "Sum",
            },
            "ReturnData": True,
        })
    return queries


def _parse_metric_id(metric_id):
    """Parse '<metric>_<idx>' ids safely; return (metric, idx) or (None, None)."""
    try:
        metric, _, idx_str = str(metric_id).rpartition("_")
        if not metric or not idx_str:
            return None, None
        return metric, int(idx_str)
    except (ValueError, AttributeError):
        return None, None


def _fetch_instance_metrics(cloudwatch, instance_ids, start, end):
    from utils.metrics import collect
    results = {}
    for chunk in chunk_list(instance_ids, MAX_METRIC_QUERIES // 3):
        data = collect(cloudwatch, _build_metric_queries(chunk), start, end, METRIC_PERIOD)
        for idx, iid in enumerate(chunk):
            cpu, netin, netout = [data[f"{prefix}_{idx}"] for prefix in ("cpu", "netin", "netout")]
            valid = all(m["valid"] for m in (cpu, netin, netout))
            results[iid] = {"cpu": sum(cpu["values"])/len(cpu["values"]) if valid else None,
                            "net_in": sum(netin["values"]) if valid else None,
                            "net_out": sum(netout["values"]) if valid else None,
                            "coverage": min(m["coverage"] for m in (cpu, netin, netout))}
    return results


def _is_asg_managed(instance):
    """Return True if the instance belongs to an Auto Scaling Group."""
    for t in instance.get("Tags", []) or []:
        if isinstance(t, dict) and t.get("Key") == ASG_TAG and t.get("Value"):
            return True
    return False


def scan_idle_ec2():
    """Scan for idle EC2 instances based on low CPU and negligible network activity."""
    reset_no_metrics_state()
    ec2 = _get_ec2_client()
    cloudwatch = _get_cloudwatch_client()
    cpu_threshold = _get_cpu_threshold()
    net_threshold = _get_network_threshold()

    idle = []
    no_metrics = []
    asg_skipped = []

    from utils.metrics import window_end
    now = window_end(METRIC_PERIOD)
    start = now - timedelta(days=7)

    logger.info(
        f"Starting EC2 scan with CPU threshold {cpu_threshold}%, "
        f"network threshold {net_threshold:.0f} bytes"
    )

    try:
        paginator = ec2.get_paginator("describe_instances")
        running = []
        try:
            pages = paginator.paginate(

            )
        except TypeError:
            pages = paginator.paginate()
        for page in pages:
            for r in (page.get("Reservations", []) or []):
                running.extend(r.get("Instances", []) or [])
        logger.info(f"Found {len(running)} running instances")

        # Skip ASG-managed instances (their scaling is handled by the ASG)
        candidates = []
        for i in running:
            if i.get("State", {}).get("Name") != "running":
                continue
            if not isinstance(i, dict) or not i.get("InstanceId"):
                logger.warning("Skipping malformed instance record without InstanceId")
                continue
            if protected(normalize_tags(i.get("Tags", []))):
                log_and_skip(i["InstanceId"], "protected resource")
                continue
            if _is_asg_managed(i):
                asg_skipped.append(i["InstanceId"])
                log_and_skip(i["InstanceId"], "ASG-managed")
            else:
                candidates.append(i)

        instance_ids = [i["InstanceId"] for i in candidates]
        metrics_map = _fetch_instance_metrics(cloudwatch, instance_ids, start, now) if instance_ids else {}

        for i in candidates:
            iid = i["InstanceId"]
            m = metrics_map.get(iid, {"cpu": None, "net_in": 0.0, "net_out": 0.0})

            if m.get("cpu") is None:
                log_and_skip(iid, "insufficient CPU/network metric coverage")
                no_metrics.append(iid)
                continue

            net_total = (m.get("net_in") or 0.0) + (m.get("net_out") or 0.0)
            if m["cpu"] < cpu_threshold and net_total <= net_threshold:
                az = (i.get("Placement", {}) or {}).get("AvailabilityZone", "") or ""
                region = get_region_from_az(az)
                if region is None and az:
                    logger.warning(f"Could not derive region from AZ {az!r} for {iid}")
                idle.append(build_resource(
                    "EC2", iid, region=region, az=az,
                    reason="idle-low-cpu-network",
                    tags=normalize_tags(i.get("Tags", []), source=f"ec2:{iid}"),
                    platform=i.get("PlatformDetails", "Linux/UNIX"), tenancy=i.get("Placement", {}).get("Tenancy", "default"),
                    purchasing="spot" if i.get("InstanceLifecycle") == "spot" else "on-demand",
                    metric_coverage=m.get("coverage"),
                    avg_cpu=round(m["cpu"], 2),
                    net_in_bytes=round(m.get("net_in") or 0.0, 2),
                    net_out_bytes=round(m.get("net_out") or 0.0, 2),
                    instance_type=i.get("InstanceType", ""),
                ))

        logger.info(
            f"Found {len(idle)} idle EC2 instances, {len(no_metrics)} with no metrics, "
            f"{len(asg_skipped)} ASG-managed skipped"
        )
    except Exception as e:
        logger.error(f"Error scanning EC2 instances: {e}", exc_info=True)
        raise

    _no_metrics_ids.set(tuple(no_metrics))
    return idle
