"""Track SDK discovery boundaries, preserving inventory and completeness."""

from utils.aws_helpers import normalize_tags
from utils.schema import build_resource, resource_identity

SHAPES = {
    "describe_volumes": ("Volumes", "EBS", "VolumeId", "Tags"),
    "describe_addresses": ("Addresses", "EIP", "AllocationId", "Tags"),
    "describe_snapshots": ("Snapshots", "SNAPSHOT", "SnapshotId", "Tags"),
    "describe_db_instances": (
        "DBInstances",
        "RDS_INSTANCE",
        "DBInstanceIdentifier",
        "TagList",
    ),
    "describe_db_clusters": (
        "DBClusters",
        "RDS_CLUSTER",
        "DBClusterIdentifier",
        "TagList",
    ),
    "describe_load_balancers": ("LoadBalancers", "ELB", "LoadBalancerName", "Tags"),
}


class TrackedPaginator:
    def __init__(self, client, operation):
        self.client, self.operation = client, operation

    def paginate(self, **kwargs):
        context = self.client.context
        operation = self.operation
        context.operation(operation, "running")
        try:
            context.check()
            iterator = iter(self.client.raw.get_paginator(operation).paginate(**kwargs))
            while True:
                context.check()
                try:
                    page = next(iterator)
                except StopIteration:
                    break
                context.check()
                self.client.capture(operation, page)
                yield page
            context.operation(operation, "complete")
        except Exception as exc:
            context.operation(operation, "failed", str(exc))
            context.error(operation, exc)
            raise


class TrackedClient:
    def __init__(self, raw, context):
        self.raw, self.context = raw, context

    def get_paginator(self, name):
        return TrackedPaginator(self, name)

    def __getattr__(self, name):
        method = getattr(self.raw, name)
        if not callable(method):
            return method

        def call(**kwargs):
            tracks_operation = name.startswith(("describe_", "list_", "get_metric_"))
            if tracks_operation:
                self.context.operation(name, "running")
            try:
                self.context.check()
                if name in ("get_metric_data", "get_metric_statistics"):
                    self.context.scan_window = {
                        "start": kwargs["StartTime"].isoformat(),
                        "end": kwargs["EndTime"].isoformat(),
                    }
                response = method(**kwargs)
                self.context.check()
                self.capture(name, response)
                if name == "list_tags_for_resource":
                    for resource in self.context.inventory.values():
                        if resource.get("arn") == kwargs.get("ResourceName"):
                            resource.update(
                                tags=normalize_tags(response.get("TagList")),
                                tags_status="known",
                            )
                if tracks_operation:
                    self.context.operation(name, "complete")
                return response
            except Exception as exc:
                if tracks_operation:
                    self.context.operation(name, "failed", str(exc))
                self.context.error(name, exc)
                raise

        return call

    def capture(self, operation, page):
        if operation == "describe_instances":
            records = [
                i for r in page.get("Reservations", []) for i in r.get("Instances", [])
            ]
            shape = (None, "EC2", "InstanceId", "Tags")
        elif operation in SHAPES:
            shape = SHAPES[operation]
            records = page.get(shape[0], page.get("LoadBalancerDescriptions", []))
        elif operation == "describe_tags":
            for item in page.get("TagDescriptions", []):
                for resource in self.context.inventory.values():
                    if item.get("ResourceArn", item.get("LoadBalancerName")) in (
                        resource.get("arn"),
                        resource["id"],
                    ):
                        resource["tags"] = normalize_tags(item.get("Tags"))
                        resource["tags_status"] = "known"
            return
        else:
            return
        _, kind, id_key, tag_key = shape
        for item in records:
            rid = item.get(id_key) or (
                f"public-ip/{item['PublicIp']}" if item.get("PublicIp") else None
            )
            if not rid:
                continue
            arn = (
                item.get("DBInstanceArn")
                or item.get("DBClusterArn")
                or item.get("LoadBalancerArn")
            )
            tags = item.get(tag_key)
            status = (
                "known"
                if tags is not None
                or kind not in ("RDS_INSTANCE", "RDS_CLUSTER", "ELB")
                else "unknown"
            )
            if kind.startswith("RDS") and tags is None and arn:
                try:
                    self.context.check()
                    self.context.operation("list_tags_for_resource", "running")
                    tags = self.raw.list_tags_for_resource(ResourceName=arn).get(
                        "TagList", []
                    )
                    item["TagList"] = tags
                    status = "known"
                    self.context.operation("list_tags_for_resource", "complete")
                except Exception as exc:
                    self.context.operation("list_tags_for_resource", "failed", str(exc))
                    self.context.error("inventory-tags", exc)
            resource = build_resource(
                kind, rid, region=self.context.config.region, arn=arn, tags=tags
            )
            resource["tags_status"] = status
            if kind == "ELB" and not arn:
                resource["resource_key"] = resource_identity(
                    {**resource, "type": "ELB_CLASSIC"}
                )
            self.context.add(resource)
