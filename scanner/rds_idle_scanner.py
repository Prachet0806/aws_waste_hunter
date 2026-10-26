# scanner/rds_idle_scanner.py
import logging
import os
from datetime import timedelta

from utils.aws_factory import get_client
from utils.aws_helpers import (
    chunk_list,
    get_region_from_az,
    log_and_skip,
    normalize_tags,
)
from utils.scan_context import CURRENT, protected, record_error
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_rds_client = None
_cloudwatch_client = None

# 6-hour aggregation period for finer sensitivity over the 7-day window
METRIC_PERIOD = 21600
# Maximum queries per get_metric_data call (AWS limit)
MAX_METRIC_QUERIES = 500
# CPU threshold (%) - instances below this are considered idle
CPU_THRESHOLD = 5.0
# Connections threshold - instances below this are considered idle
CONNECTIONS_THRESHOLD = 1


def _get_rds_client():
    """Lazy initialization of RDS client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("rds")
    global _rds_client
    if _rds_client is None:
        _rds_client = get_client("rds")
    return _rds_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("cloudwatch")
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = get_client("cloudwatch")
    return _cloudwatch_client


def _get_cpu_threshold():
    """Get CPU threshold from env var with validation."""
    if CURRENT.get():
        return CURRENT.get().config.rds_cpu_threshold
    try:
        raw = os.getenv("RDS_CPU_THRESHOLD", str(CPU_THRESHOLD))
        value = float(raw)
        if value < 0 or value > 100 or value != value:
            logger.warning(
                f"Invalid RDS_CPU_THRESHOLD {raw}, using default {CPU_THRESHOLD}"
            )
            return CPU_THRESHOLD
        return value
    except (ValueError, TypeError):
        logger.warning("Invalid RDS_CPU_THRESHOLD format, using default")
        return CPU_THRESHOLD


def _get_connections_threshold():
    """Get connections threshold from env var with validation."""
    if CURRENT.get():
        return CURRENT.get().config.rds_connections_threshold
    try:
        raw = os.getenv("RDS_CONNECTIONS_THRESHOLD", str(CONNECTIONS_THRESHOLD))
        value = int(float(raw)) if isinstance(raw, str) and "." in raw else int(raw)
        if value < 0:
            logger.warning(f"Invalid RDS_CONNECTIONS_THRESHOLD {raw}, using default")
            return CONNECTIONS_THRESHOLD
        return value
    except (ValueError, TypeError):
        logger.warning("Invalid RDS_CONNECTIONS_THRESHOLD format, using default")
        return CONNECTIONS_THRESHOLD


def _dimension_for_kind(kind, resource_id):
    if kind == "cluster":
        return {"Name": "DBClusterIdentifier", "Value": resource_id}
    return {"Name": "DBInstanceIdentifier", "Value": resource_id}


def _build_metric_queries(resource_ids, namespace, metric_names, kind):
    """Build GetMetricData queries for multiple metrics per resource."""
    queries = []
    for idx, rid in enumerate(resource_ids):
        base_dimension = _dimension_for_kind(kind, rid)
        for metric_name in metric_names:
            queries.append(
                {
                    "Id": f"{metric_name.lower().replace(' ', '')}_{idx}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": [base_dimension],
                        },
                        "Period": METRIC_PERIOD,
                        "Stat": "Average"
                        if metric_name == "CPUUtilization"
                        else "Maximum",
                    },
                    "ReturnData": True,
                }
            )
    return queries


def _fetch_rds_metrics(
    cloudwatch, resource_ids, metric_names, start, end, kind="instance"
):
    from utils.metrics import collect

    results = {}
    if not metric_names:
        return {rid: {"cpu": None, "connections": None} for rid in resource_ids}
    for chunk in chunk_list(resource_ids, MAX_METRIC_QUERIES // len(metric_names)):
        data = collect(
            cloudwatch,
            _build_metric_queries(chunk, "AWS/RDS", metric_names, kind),
            start,
            end,
            METRIC_PERIOD,
        )
        for idx, rid in enumerate(chunk):
            cpu = data.get(f"cpuutilization_{idx}", {"valid": False})
            conns = data.get(f"databaseconnections_{idx}", {"valid": False})
            valid = cpu["valid"] and conns["valid"]
            results[rid] = {
                "cpu": sum(cpu["values"]) / len(cpu["values"]) if valid else None,
                "connections": max(conns["values"]) if valid else None,
            }
    return results


def _get_batch_tags(arns):
    """Batch fetch tags for multiple RDS resources (fail-open per ARN)."""
    rds = _get_rds_client()
    tag_map = {}

    for arn in arns or []:
        if not arn:
            continue
        try:
            response = rds.list_tags_for_resource(ResourceName=arn)
            tags = response.get("TagList", []) or []
            tag_map[arn] = normalize_tags(tags, source=f"rds:{arn}")
        except Exception as e:
            record_error("rds-tags", e)
            logger.warning(f"Error fetching tags for {arn}: {e}")
            tag_map[arn] = {}

    return tag_map


def _scan_running(rds, paginator_name, list_key):
    try:
        paginator = rds.get_paginator(paginator_name)
    except Exception as e:
        log_and_skip(paginator_name, "paginator unavailable", e)
        return []
    items = []
    try:
        for page in paginator.paginate():
            items.extend((page or {}).get(list_key, []) or [])
    except Exception as e:
        log_and_skip(paginator_name, "paginate error", e)
    return items


def scan_idle_rds():
    """Scan for idle RDS instances/clusters based on low CPU and low connections."""
    rds = _get_rds_client()
    cloudwatch = _get_cloudwatch_client()
    cpu_threshold = _get_cpu_threshold()
    conn_threshold = _get_connections_threshold()

    idle = []

    logger.info(
        f"Starting RDS idle scan with CPU threshold {cpu_threshold}%, "
        f"connections threshold {conn_threshold}"
    )

    try:
        instances = _scan_running(rds, "describe_db_instances", "DBInstances")
        logger.info(f"Found {len(instances)} RDS instances")

        # 'available' plus maintenance-ish states still serving metrics; only
        # truly inactive states are excluded. Anything not stopped/failed counts.
        running_instances = [
            i
            for i in instances
            if isinstance(i, dict)
            and i.get("DBInstanceIdentifier")
            and i.get("DBInstanceStatus") in ("available", "backing-up", "modifying")
        ]
        instance_ids = [i["DBInstanceIdentifier"] for i in running_instances]

        if instance_ids:
            from utils.metrics import window_end

            now = window_end(METRIC_PERIOD)
            start = now - timedelta(days=7)

            metrics_map = _fetch_rds_metrics(
                cloudwatch,
                instance_ids,
                ["CPUUtilization", "DatabaseConnections"],
                start,
                now,
                kind="instance",
            )

            # Fetch tags only for idle candidates (saves API calls).
            idle_candidates = []
            for i in running_instances:
                iid = i["DBInstanceIdentifier"]
                m = metrics_map.get(iid, {"cpu": None, "connections": 0})
                if m.get("cpu") is None:
                    log_and_skip(iid, "insufficient CPU/connection metric coverage")
                    continue
                if protected(normalize_tags(i.get("TagList", []))):
                    log_and_skip(iid, "protected resource")
                    continue
                if (
                    m["cpu"] < cpu_threshold
                    and (m.get("connections", 0) or 0) <= conn_threshold
                ):
                    idle_candidates.append((i, m))

            tag_map = _get_batch_tags(
                [i.get("DBInstanceArn") for i, _ in idle_candidates]
            )
            for i, m in idle_candidates:
                iid = i["DBInstanceIdentifier"]
                az = i.get("AvailabilityZone", "") or ""
                max_conns = m.get("connections", 0)
                idle.append(
                    build_resource(
                        "RDS_INSTANCE",
                        iid,
                        region=get_region_from_az(az),
                        az=az,
                        arn=i.get("DBInstanceArn"),
                        reason="idle-low-cpu-connections",
                        tags=normalize_tags(
                            tag_map.get(i.get("DBInstanceArn"), {}), source=f"rds:{iid}"
                        ),
                        engine=i.get("Engine", ""),
                        instance_class=i.get("DBInstanceClass", ""),
                        parent_id=i.get("DBClusterIdentifier"),
                        multi_az=i.get("MultiAZ", False),
                        storage_gb=i.get("AllocatedStorage"),
                        storage_type=i.get("StorageType"),
                        avg_cpu=round(m["cpu"], 2),
                        max_connections=max_conns,
                        avg_connections=max_conns,
                    )
                )

        # Cluster compute belongs to its members. Reporting aggregate CPU would
        # both obscure a busy member and duplicate member compute charges.
        logger.info(f"Found {len(idle)} idle RDS resources")

    except Exception as e:
        logger.error(f"Error scanning idle RDS resources: {e}", exc_info=True)
        raise

    return idle
