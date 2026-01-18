# scanner/ec2_scanner.py
import boto3
import os
from datetime import datetime, timedelta

ec2 = boto3.client("ec2")
cloudwatch = boto3.client("cloudwatch")

CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "2"))  # percent

def scan_idle_ec2():
    idle = []
    now = datetime.utcnow()
    start = now - timedelta(days=7)

    paginator = ec2.get_paginator("describe_instances")
    reservations = []

    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    ):
        reservations.extend(page.get("Reservations", []))

    for r in reservations:
        for i in r["Instances"]:
            iid = i["InstanceId"]

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
                continue

            avg = sum(d["Average"] for d in metrics["Datapoints"]) / len(metrics["Datapoints"])
            if avg < CPU_THRESHOLD:
                idle.append({
                    "type": "EC2",
                    "id": iid,
                    "avg_cpu": round(avg, 2),
                    "instance_type": i["InstanceType"],
                    "az": i["Placement"]["AvailabilityZone"],
                    "region": i["Placement"]["AvailabilityZone"][:-1],
                    "tags": {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                })

    return idle
