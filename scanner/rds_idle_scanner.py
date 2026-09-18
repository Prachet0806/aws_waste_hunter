# scanner/rds_idle_scanner.py
import logging
import os
from datetime import datetime, timedelta, timezone

from utils.aws_factory import get_client
from utils.aws_helpers import (
    get_region_from_az,
    safe_get_first,
    chunk_list,
    log_and_skip,
    normalize_tags,
)
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
    global _rds_client
    if _rds_client is None:
        _rds_client = get_client("rds")
    return _rds_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client (delegates to shared factory)."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = get_client("cloudwatch")
    return _cloudwatch_client


def _get_cpu_threshold():
    """Get CPU threshold from env var with validation."""
    try:
        raw = os.getenv("RDS_CPU_THRESHOLD", str(CPU_THRESHOLD))
        value = float(raw)
        if value < 0 or value > 100 or value != value:
            logger.warning(f"Invalid RDS_CPU_THRESHOLD {raw}, using default {CPU_THRESHOLD}")
            return CPU_THRESHOLD
        return value
    except (ValueError, TypeError):
        logger.warning("Invalid RDS_CPU_THRESHOLD format, using default")
        return CPU_THRESHOLD


def _get_connections_threshold():
    """Get connections threshold from env var with validation."""
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
            queries.append({
                "Id": f"{metric_name.lower().replace(' ', '')}_{idx}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": [base_dimension],
                    },
                    "Period": METRIC_PERIOD,
                    "Stat": "Average" if metric_name == "CPUUtilization" else "Maximum",
                },
                "ReturnData": True,
            })
    return queries


def _fetch_rds_metrics(cloudwatch, resource_ids, metric_names, start, end, kind="instance"):
    """
    Batch-fetch metrics for RDS resources via GetMetricData.

    Fail-open (Decision 1): CW errors mark the chunk as no-metrics and are
    logged; prior chunks are preserved. Never raises KeyError.
    Returns {resource_id: {"cpu": float|None, "connections": number}}.
    """
    results = {}
    if not resource_ids:
        return results
    if not metric_names:
        logger.warning("No metric names requested, returning empty metrics")
        return {rid: {"cpu": None, "connections": 0} for rid in resource_ids}
    metric_names_lower = [m.lower() for m in metric_names]
    chunk_size = MAX_METRIC_QUERIES // len(metric_names)
    if chunk_size <= 0:
        chunk_size = 1

    for chunk in chunk_list(resource_ids, chunk_size):
        queries = _build_metric_queries(chunk, "AWS/RDS", metric_names, kind)
        params = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
        next_token = None

        while True:
            if next_token:
                params["NextToken"] = next_token

            try:
                response = cloudwatch.get_metric_data(**params)
            except Exception as e:
                log_and_skip(f"rds-{kind}-chunk({len(chunk)})", "get_metric_data error", e)
                for rid in chunk:
                    results.setdefault(rid, {"cpu": None, "connections": 0})
                break

            for mdr in response.get("MetricDataResults", []) or []:
                raw_id = str(mdr.get("Id", ""))
                metric, _, idx_str = raw_id.rpartition("_")
                try:
                    idx = int(idx_str)
                except (ValueError, TypeError):
                    logger.warning(f"Skipping malformed MetricDataResult Id: {raw_id!r}")
                    continue
                if not (0 <= idx < len(chunk)):
                    logger.warning(f"Metric index out of range for Id: {raw_id!r}")
                    continue
                rid = chunk[idx]
                values = mdr.get("Values", []) or []

                entry = results.setdefault(rid, {})
                if metric == "cpuutilization":
                    entry["cpu"] = sum(values) / len(values) if values else None
                elif metric == "databaseconnections":
                    entry["connections"] = max(values) if values else 0

            # Fill defaults for resources with no results on this page.
            for rid in chunk:
                results.setdefault(rid, {})
                results[rid].setdefault("cpu", None)
                results[rid].setdefault("connections", 0)

            next_token = response.get("NextToken")
            if not next_token:
                break

    # Normalize: ensure every requested id has both keys.
    for rid in resource_ids:
        entry = results.setdefault(rid, {})
        entry.setdefault("cpu", None)
        entry.setdefault("connections", 0)
        # Tolerate legacy long keys if a mock returns them.
        if "cpuutilization" in entry and "cpu" not in entry:
            entry["cpu"] = entry.pop("cpuutilization")
        if "databaseconnections" in entry and "connections" not in entry:
            entry["connections"] = entry.pop("databaseconnections")
    # Drop stray legacy keys.
    for rid in resource_ids:
        results[rid].pop("cpuutilization", None)
        results[rid].pop("databaseconnections", None)
        # Ensure only canonical keys remain (plus nothing else).
        results[rid] = {"cpu": results[rid].get("cpu"), "connections": results[rid].get("connections", 0)}
    _ = metric_names_lower
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
            i for i in instances
            if isinstance(i, dict) and i.get("DBInstanceIdentifier")
            and (i.get("DBInstanceStatus") not in ("stopped", "stopped-storage-only", "failed", "inaccessible-encryption-credentials"))
        ]
        instance_ids = [i["DBInstanceIdentifier"] for i in running_instances]

        if instance_ids:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=7)

            metrics_map = _fetch_rds_metrics(
                cloudwatch, instance_ids, ["CPUUtilization", "DatabaseConnections"], start, now,
                kind="instance",
            )

            # Fetch tags only for idle candidates (saves API calls).
            idle_candidates = []
            for i in running_instances:
                iid = i["DBInstanceIdentifier"]
                m = metrics_map.get(iid, {"cpu": None, "connections": 0})
                if m.get("cpu") is None:
                    log_and_skip(iid, "no CPU metrics")
                    continue
                if m["cpu"] < cpu_threshold and (m.get("connections", 0) or 0) <= conn_threshold:
                    idle_candidates.append((i, m))

            tag_map = _get_batch_tags([i.get("DBInstanceArn") for i, _ in idle_candidates])
            for i, m in idle_candidates:
                iid = i["DBInstanceIdentifier"]
                az = i.get("AvailabilityZone", "") or ""
                max_conns = m.get("connections", 0)
                idle.append(build_resource(
                    "RDS_INSTANCE", iid, region=get_region_from_az(az), az=az,
                    arn=i.get("DBInstanceArn"),
                    reason="idle-low-cpu-connections",
                    tags=normalize_tags(tag_map.get(i.get("DBInstanceArn"), {}), source=f"rds:{iid}"),
                    engine=i.get("Engine", ""),
                    instance_class=i.get("DBInstanceClass", ""),
                    avg_cpu=round(m["cpu"], 2),
                    max_connections=max_conns,
                    avg_connections=max_conns,
                ))

        clusters = _scan_running(rds, "describe_db_clusters", "DBClusters")
        logger.info(f"Found {len(clusters)} RDS clusters")

        running_clusters = [
            c for c in clusters
            if isinstance(c, dict) and c.get("DBClusterIdentifier")
            and c.get("Status") not in ("stopped", "failed")
        ]
        cluster_ids = [c["DBClusterIdentifier"] for c in running_clusters]

        if cluster_ids:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=7)

            metrics_map = _fetch_rds_metrics(
                cloudwatch, cluster_ids, ["CPUUtilization", "DatabaseConnections"], start, now,
                kind="cluster",
            )

            idle_candidates = []
            for c in running_clusters:
                cid = c["DBClusterIdentifier"]
                m = metrics_map.get(cid, {"cpu": None, "connections": 0})
                if m.get("cpu") is None:
                    log_and_skip(cid, "no CPU metrics")
                    continue
                if m["cpu"] < cpu_threshold and (m.get("connections", 0) or 0) <= conn_threshold:
                    idle_candidates.append((c, m))

            tag_map = _get_batch_tags([c.get("DBClusterArn") for c, _ in idle_candidates])
            for c, m in idle_candidates:
                cid = c["DBClusterIdentifier"]
                az_list = c.get("AvailabilityZones", []) or []
                first_az = safe_get_first(az_list, "") or ""
                max_conns = m.get("connections", 0)
                idle.append(build_resource(
                    "RDS_CLUSTER", cid, region=get_region_from_az(first_az), az=first_az,
                    arn=c.get("DBClusterArn"),
                    reason="idle-low-cpu-connections",
                    tags=normalize_tags(tag_map.get(c.get("DBClusterArn"), {}), source=f"rds:{cid}"),
                    engine=c.get("Engine", ""),
                    avg_cpu=round(m["cpu"], 2),
                    max_connections=max_conns,
                    avg_connections=max_conns,
                ))

        logger.info(f"Found {len(idle)} idle RDS resources")

    except Exception as e:
        logger.error(f"Error scanning idle RDS resources: {e}", exc_info=True)
        raise

    return idle
