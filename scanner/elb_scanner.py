# scanner/elb_scanner.py
import boto3
from datetime import datetime, timedelta

elbv2 = boto3.client("elbv2")
cloudwatch = boto3.client("cloudwatch")

def _get_lb_metric_dimension(lb_arn):
    if "loadbalancer/" in lb_arn:
        return lb_arn.split("loadbalancer/")[-1]
    return lb_arn.split("/")[-1]

def _get_lb_tags(lb_arn):
    tag_desc = elbv2.describe_tags(LoadBalancerArns=[lb_arn])["TagDescriptions"]
    if not tag_desc:
        return {}
    tags = tag_desc[0].get("Tags", [])
    return {t["Key"]: t["Value"] for t in tags}

def scan_unused_elb():
    unused = []
    paginator = elbv2.get_paginator("describe_load_balancers")
    lbs = []
    for page in paginator.paginate():
        lbs.extend(page.get("LoadBalancers", []))

    now = datetime.utcnow()
    start = now - timedelta(days=7)

    for lb in lbs:
        name = lb["LoadBalancerName"]

        metric_dimension = _get_lb_metric_dimension(lb["LoadBalancerArn"])
        metrics = cloudwatch.get_metric_statistics(
            Namespace="AWS/ApplicationELB",
            MetricName="RequestCount",
            Dimensions=[{"Name": "LoadBalancer", "Value": metric_dimension}],
            StartTime=start,
            EndTime=now,
            Period=86400,
            Statistics=["Sum"],
        )

        total = sum(d["Sum"] for d in metrics["Datapoints"])

        if total == 0:
            unused.append({
                "type": "ELB",
                "id": name,
                "scheme": lb["Scheme"],
                "az": lb["AvailabilityZones"][0]["ZoneName"],
                "region": lb["AvailabilityZones"][0]["ZoneName"][:-1],
                "tags": _get_lb_tags(lb["LoadBalancerArn"])
            })

    return unused
