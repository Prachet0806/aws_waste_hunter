"""Real botocore service models with Stubber, not invented SDK operations."""

import boto3
import pytest
from botocore.stub import Stubber

from scanner import eip_scanner
from scanner.inventory import TrackedClient
from utils.config import Config
from utils.scan_context import ScanContext, activate


def client(service):
    return boto3.session.Session().client(service, region_name="us-east-1")


def test_eip_uses_supported_nonpageable_operation(monkeypatch):
    ec2 = client("ec2")
    assert not ec2.can_paginate("describe_addresses")
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_addresses",
            {
                "Addresses": [
                    {
                        "AllocationId": "eipalloc-12345678",
                        "PublicIp": "192.0.2.1",
                        "Domain": "vpc",
                    },
                    {
                        "AllocationId": "eipalloc-87654321",
                        "PublicIp": "192.0.2.2",
                        "AssociationId": "eipassoc-12345678",
                        "Domain": "vpc",
                    },
                ]
            },
            {},
        )
        monkeypatch.setattr(eip_scanner, "_ec2_client", ec2)
        rows = eip_scanner.scan_unattached_eips()
        assert [r["id"] for r in rows] == ["eipalloc-12345678"]
        stub.assert_no_pending_responses()


def test_image_reference_request_includes_disabled_and_deprecated():
    from scanner.snapshot_scanner import _collect_ami_snapshot_ids

    ec2 = client("ec2")
    with Stubber(ec2) as stub:
        stub.add_response(
            "describe_images",
            {"Images": []},
            {"Owners": ["self"], "IncludeDeprecated": True, "IncludeDisabled": True},
        )
        assert _collect_ami_snapshot_ids(ec2) == set()
        stub.assert_no_pending_responses()


def test_pagination_failure_keeps_inventory_but_marks_incomplete():
    ec2 = client("ec2")
    scope = ScanContext("volumes", Config.load(), "123456789012")
    tracked = TrackedClient(ec2, scope)
    with activate(scope), Stubber(ec2) as stub:
        stub.add_response(
            "describe_volumes",
            {
                "Volumes": [
                    {"VolumeId": "vol-12345678", "State": "available", "Size": 10}
                ],
                "NextToken": "next",
            },
            {},
        )
        stub.add_client_error(
            "describe_volumes",
            service_error_code="UnauthorizedOperation",
            expected_params={"NextToken": "next"},
        )
        with pytest.raises(Exception):
            list(tracked.get_paginator("describe_volumes").paginate())
    result = scope.result([], 1)
    assert result["discovered_count"] == 1 and not result["discovery_complete"]
    assert result["errors"][0]["code"] == "UnauthorizedOperation"


def test_tag_failure_is_unknown_rather_than_missing():
    rds = client("rds")
    scope = ScanContext("rds", Config.load(), "123456789012")
    tracked = TrackedClient(rds, scope)
    arn = "arn:aws:rds:us-east-1:123456789012:db:active"
    with activate(scope), Stubber(rds) as stub:
        stub.add_response(
            "describe_db_instances",
            {"DBInstances": [{"DBInstanceIdentifier": "active", "DBInstanceArn": arn}]},
            {},
        )
        stub.add_client_error(
            "list_tags_for_resource",
            service_error_code="AccessDenied",
            expected_params={"ResourceName": arn},
        )
        list(tracked.get_paginator("describe_db_instances").paginate())
    resource = next(iter(scope.inventory.values()))
    assert resource["tags_status"] == "unknown" and scope.errors


def test_inventory_limit_stops_instead_of_silently_truncating():
    from dataclasses import replace

    from utils.schema import build_resource

    scope = ScanContext("limited", replace(Config.load(), max_resources=1))
    scope.add(build_resource("EBS", "one"))
    with pytest.raises(RuntimeError, match="Inventory limit"):
        scope.add(build_resource("EBS", "two"))
