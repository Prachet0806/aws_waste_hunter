# scanner/ebs_scanner.py
import boto3
import logging
import os
from utils.aws_helpers import BOTO3_CONFIG, get_region_from_az

logger = logging.getLogger(__name__)

_ec2_client = None


def _get_ec2_client():
    """Lazy initialization of EC2 client."""
    global _ec2_client
    if _ec2_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _ec2_client = boto3.client("ec2", region_name=region, config=BOTO3_CONFIG)
    return _ec2_client


def scan_unattached_ebs():
    """Scan for unattached EBS volumes."""
    ec2 = _get_ec2_client()
    volumes = []
    logger.info("Starting EBS volume scan")
    
    try:
        paginator = ec2.get_paginator("describe_volumes")

        for page in paginator.paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]
        ):
            for v in page["Volumes"]:
                az = v.get("AvailabilityZone", "")
                volumes.append({
                    "type": "EBS",
                    "id": v["VolumeId"],
                    "size_gb": v["Size"],
                    "volume_type": v.get("VolumeType"),
                    "az": az,
                    "region": get_region_from_az(az),
                    "tags": {t["Key"]: t["Value"] for t in v.get("Tags", [])}
                })
        
        logger.info(f"Found {len(volumes)} unattached EBS volumes")
    except Exception as e:
        logger.error(f"Error scanning EBS volumes: {e}", exc_info=True)
        raise

    return volumes
