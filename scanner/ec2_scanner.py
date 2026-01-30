# scanner/ec2_scanner.py
import boto3
import os
import logging
from datetime import datetime, timedelta, timezone
from utils.aws_helpers import BOTO3_CONFIG, get_region_from_az

logger = logging.getLogger(__name__)

_ec2_client = None
_cloudwatch_client = None

CPU_THRESHOLD = None


def _get_cpu_threshold():
    """Get CPU threshold from env var with validation."""
    global CPU_THRESHOLD
    if CPU_THRESHOLD is None:
        try:
            CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "2"))
            if CPU_THRESHOLD < 0 or CPU_THRESHOLD > 100:
                logger.warning(f"Invalid CPU_THRESHOLD {CPU_THRESHOLD}, using default 2")
                CPU_THRESHOLD = 2.0
        except ValueError:
            logger.warning("Invalid CPU_THRESHOLD format, using default 2")
            CPU_THRESHOLD = 2.0
    return CPU_THRESHOLD


def _get_ec2_client():
    """Lazy initialization of EC2 client."""
    global _ec2_client
    if _ec2_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _ec2_client = boto3.client("ec2", region_name=region, config=BOTO3_CONFIG)
    return _ec2_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region, config=BOTO3_CONFIG)
    return _cloudwatch_client


def scan_idle_ec2():
    """Scan for idle EC2 instances based on CPU utilization."""
    ec2 = _get_ec2_client()
    cloudwatch = _get_cloudwatch_client()
    threshold = _get_cpu_threshold()
    
    idle = []
    no_metrics = []
    
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)

    logger.info(f"Starting EC2 scan with CPU threshold {threshold}%")

    try:
        paginator = ec2.get_paginator("describe_instances")
        reservations = []

        for page in paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ):
            reservations.extend(page.get("Reservations", []))

        logger.info(f"Found {sum(len(r['Instances']) for r in reservations)} running instances")

        for r in reservations:
            for i in r["Instances"]:
                iid = i["InstanceId"]

                try:
                    metrics = cloudwatch.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="CPUUtilization",
                        Dimensions=[{"Name": "InstanceId", "Value": iid}],
                        StartTime=start,
                        EndTime=now,
                        Period=86400,
                        Statistics=["Average"],
                    )

                    if not metrics["Datapoints"]:
                        logger.debug(f"No metrics for instance {iid}, skipping")
                        no_metrics.append(iid)
                        continue

                    avg = sum(d["Average"] for d in metrics["Datapoints"]) / len(
                        metrics["Datapoints"]
                    )
                    if avg < threshold:
                        az = i["Placement"].get("AvailabilityZone", "")
                        idle.append({
                            "type": "EC2",
                            "id": iid,
                            "avg_cpu": round(avg, 2),
                            "instance_type": i["InstanceType"],
                            "az": az,
                            "region": get_region_from_az(az),
                            "tags": {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                        })
                except Exception as e:
                    logger.warning(f"Error getting metrics for {iid}: {e}")
                    continue

        logger.info(f"Found {len(idle)} idle EC2 instances, {len(no_metrics)} with no metrics")
    except Exception as e:
        logger.error(f"Error scanning EC2 instances: {e}", exc_info=True)
        raise

    return idle
