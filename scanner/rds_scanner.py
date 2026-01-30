# scanner/rds_scanner.py
import boto3
import os
import logging
from utils.aws_helpers import BOTO3_CONFIG, get_region_from_az, safe_get_first, chunk_list

logger = logging.getLogger(__name__)

_rds_client = None


def _get_rds_client():
    """Lazy initialization of RDS client."""
    global _rds_client
    if _rds_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _rds_client = boto3.client("rds", region_name=region, config=BOTO3_CONFIG)
    return _rds_client


def _get_batch_tags(arns):
    """Batch fetch tags for multiple RDS resources."""
    rds = _get_rds_client()
    tag_map = {}
    
    for arn in arns:
        try:
            response = rds.list_tags_for_resource(ResourceName=arn)
            tags = response.get("TagList", [])
            tag_map[arn] = {t["Key"]: t["Value"] for t in tags}
        except Exception as e:
            logger.warning(f"Error fetching tags for {arn}: {e}")
            tag_map[arn] = {}
    
    return tag_map


def scan_stopped_rds():
    """Scan for stopped RDS clusters and instances."""
    rds = _get_rds_client()
    wasted = []

    logger.info("Starting RDS scan (clusters and instances)")

    try:
        # Scan RDS Clusters
        cluster_paginator = rds.get_paginator("describe_db_clusters")
        clusters = []
        for page in cluster_paginator.paginate():
            clusters.extend(page.get("DBClusters", []))

        logger.info(f"Found {len(clusters)} RDS clusters")

        stopped_clusters = [c for c in clusters if c.get("Status") == "stopped"]
        
        if stopped_clusters:
            cluster_arns = [c["DBClusterArn"] for c in stopped_clusters]
            cluster_tag_map = _get_batch_tags(cluster_arns)

            for c in stopped_clusters:
                az_list = c.get("AvailabilityZones", [])
                first_az = safe_get_first(az_list, "")
                
                wasted.append({
                    "type": "RDS_CLUSTER",
                    "id": c["DBClusterIdentifier"],
                    "engine": c.get("Engine", ""),
                    "az": first_az,
                    "region": get_region_from_az(first_az),
                    "tags": cluster_tag_map.get(c["DBClusterArn"], {})
                })

        # Scan RDS Instances
        instance_paginator = rds.get_paginator("describe_db_instances")
        instances = []
        for page in instance_paginator.paginate():
            instances.extend(page.get("DBInstances", []))

        logger.info(f"Found {len(instances)} RDS instances")

        stopped_instances = [i for i in instances if i.get("DBInstanceStatus") == "stopped"]
        
        if stopped_instances:
            instance_arns = [i["DBInstanceArn"] for i in stopped_instances]
            instance_tag_map = _get_batch_tags(instance_arns)

            for i in stopped_instances:
                az = i.get("AvailabilityZone", "")
                
                wasted.append({
                    "type": "RDS_INSTANCE",
                    "id": i["DBInstanceIdentifier"],
                    "engine": i.get("Engine", ""),
                    "instance_class": i.get("DBInstanceClass", ""),
                    "az": az,
                    "region": get_region_from_az(az),
                    "tags": instance_tag_map.get(i["DBInstanceArn"], {})
                })

        logger.info(f"Found {len(wasted)} stopped RDS resources")
    except Exception as e:
        logger.error(f"Error scanning RDS resources: {e}", exc_info=True)
        raise

    return wasted
