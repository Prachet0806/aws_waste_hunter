# scanner/rds_scanner.py
import logging

from utils.aws_factory import get_client
from utils.aws_helpers import get_region_from_az, safe_get_first, normalize_tags
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_rds_client = None


def _get_rds_client():
    """Lazy initialization of RDS client (delegates to shared factory)."""
    global _rds_client
    if _rds_client is None:
        _rds_client = get_client("rds")
    return _rds_client


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


def scan_stopped_rds():
    """Scan for stopped RDS clusters and instances.

    Dedupe: members of a stopped Aurora cluster are NOT separately reported
    (avoids double-count); standalone stopped instances are reported.
    """
    rds = _get_rds_client()
    wasted = []

    logger.info("Starting RDS scan (clusters and instances)")

    try:
        # Scan RDS Clusters
        cluster_paginator = rds.get_paginator("describe_db_clusters")
        clusters = []
        for page in cluster_paginator.paginate():
            clusters.extend((page or {}).get("DBClusters", []) or [])

        logger.info(f"Found {len(clusters)} RDS clusters")

        stopped_clusters = [
            c for c in clusters
            if isinstance(c, dict) and c.get("Status") == "stopped" and c.get("DBClusterIdentifier")
        ]
        stopped_cluster_ids = {c["DBClusterIdentifier"] for c in stopped_clusters}

        if stopped_clusters:
            cluster_arns = [c.get("DBClusterArn") for c in stopped_clusters if c.get("DBClusterArn")]
            cluster_tag_map = _get_batch_tags(cluster_arns)

            for c in stopped_clusters:
                az_list = c.get("AvailabilityZones", []) or []
                first_az = safe_get_first(az_list, "") or ""
                cid = c["DBClusterIdentifier"]
                wasted.append(build_resource(
                    "RDS_CLUSTER", cid, region=get_region_from_az(first_az), az=first_az,
                    arn=c.get("DBClusterArn"),
                    reason="stopped",
                    tags=cluster_tag_map.get(c.get("DBClusterArn"), {}),
                    engine=c.get("Engine", "") or "",
                ))

        # Scan RDS Instances
        instance_paginator = rds.get_paginator("describe_db_instances")
        instances = []
        for page in instance_paginator.paginate():
            instances.extend((page or {}).get("DBInstances", []) or [])

        logger.info(f"Found {len(instances)} RDS instances")

        stopped_instances = [
            i for i in instances
            if isinstance(i, dict) and i.get("DBInstanceStatus") == "stopped" and i.get("DBInstanceIdentifier")
        ]

        # Skip members of an already-reported stopped cluster.
        standalone = []
        for i in stopped_instances:
            cluster_id = i.get("DBClusterIdentifier", "") or ""
            if cluster_id and cluster_id in stopped_cluster_ids:
                logger.debug(
                    f"Skipping {i['DBInstanceIdentifier']}: member of stopped cluster {cluster_id} (already counted)"
                )
                continue
            standalone.append(i)

        if standalone:
            instance_arns = [i.get("DBInstanceArn") for i in standalone if i.get("DBInstanceArn")]
            instance_tag_map = _get_batch_tags(instance_arns)

            for i in standalone:
                az = i.get("AvailabilityZone", "") or ""
                iid = i["DBInstanceIdentifier"]
                wasted.append(build_resource(
                    "RDS_INSTANCE", iid, region=get_region_from_az(az), az=az,
                    arn=i.get("DBInstanceArn"),
                    reason="stopped",
                    tags=instance_tag_map.get(i.get("DBInstanceArn"), {}),
                    engine=i.get("Engine", "") or "",
                    instance_class=i.get("DBInstanceClass", "") or "",
                ))

        logger.info(f"Found {len(wasted)} stopped RDS resources")
    except Exception as e:
        logger.error(f"Error scanning RDS resources: {e}", exc_info=True)
        raise

    return wasted
