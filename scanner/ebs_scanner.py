# scanner/ebs_scanner.py
import boto3

ec2 = boto3.client("ec2")

def scan_unattached_ebs():
    volumes = []
    paginator = ec2.get_paginator("describe_volumes")

    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        for v in page["Volumes"]:
            volumes.append({
                "type": "EBS",
                "id": v["VolumeId"],
                "size_gb": v["Size"],
                "volume_type": v.get("VolumeType"),
                "az": v["AvailabilityZone"],
                "region": v["AvailabilityZone"][:-1],
                "tags": {t["Key"]: t["Value"] for t in v.get("Tags", [])}
            })

    return volumes
