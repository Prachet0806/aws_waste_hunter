# scanner/rds_scanner.py
import boto3

rds = boto3.client("rds")

def _get_cluster_tags(cluster_arn):
    tags = rds.list_tags_for_resource(ResourceName=cluster_arn).get("TagList", [])
    return {t["Key"]: t["Value"] for t in tags}

def scan_stopped_rds():
    wasted = []

    paginator = rds.get_paginator("describe_db_clusters")
    clusters = []
    for page in paginator.paginate():
        clusters.extend(page.get("DBClusters", []))

    for c in clusters:
        if c["Status"] == "stopped":
            wasted.append({
                "type": "RDS",
                "id": c["DBClusterIdentifier"],
                "engine": c["Engine"],
                "az": c["AvailabilityZones"][0],
                "region": c["AvailabilityZones"][0][:-1],
                "tags": _get_cluster_tags(c["DBClusterArn"])
            })

    return wasted
