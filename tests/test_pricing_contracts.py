import json
from unittest.mock import Mock

import pytest

from cost_engine import estimator as e


def offer(unit="Hrs", amount="0.1"):
    return {
        "PriceList": [
            json.dumps(
                {
                    "terms": {
                        "OnDemand": {
                            "sku": {
                                "priceDimensions": {
                                    "rate": {
                                        "unit": unit,
                                        "pricePerUnit": {"USD": amount},
                                    }
                                }
                            }
                        }
                    }
                }
            )
        ]
    }


LOOKUPS = [
    ("_get_ec2_hourly_price", ("t3.micro", "us-east-1"), "Hrs", 0.1),
    ("_get_ebs_gb_month_price", ("gp3", "us-east-1"), "GB-Mo", 0.1),
    ("_get_alb_hourly_price", ("us-east-1",), "Hrs", 0.1),
    ("_get_nlb_hourly_price", ("us-east-1",), "Hrs", 0.1),
    ("_get_classic_elb_hourly_price", ("us-east-1",), "Hrs", 0.1),
    ("_get_rds_monthly_price", ("db.t3.micro", "mysql", "us-east-1"), "Hrs", 73),
    ("_get_eip_monthly_price", ("us-east-1",), "Hrs", 73),
    ("_get_snapshot_gb_month_price", ("us-east-1",), "GB-Mo", 0.1),
]


@pytest.mark.parametrize("name,args,unit,expected", LOOKUPS)
def test_live_catalog_units_and_queries(monkeypatch, name, args, unit, expected):
    client = Mock(get_products=Mock(return_value=offer(unit)))
    monkeypatch.setattr(e, "_get_pricing_client", lambda: client)
    assert getattr(e, name)(*args) == expected
    filters = {
        f["Field"]: f["Value"] for f in client.get_products.call_args.kwargs["Filters"]
    }
    assert filters["location"] == "US East (N. Virginia)"
    if name == "_get_rds_monthly_price":
        assert (
            filters["deploymentOption"] == "Single-AZ"
            and filters["databaseEngine"] == "MySQL"
        )


@pytest.mark.parametrize("name,args,unit,expected", LOOKUPS)
def test_catalog_transport_failure_never_fabricates_live_price(
    monkeypatch, name, args, unit, expected
):
    monkeypatch.setattr(
        e,
        "_get_pricing_client",
        lambda: Mock(get_products=Mock(side_effect=RuntimeError("offline"))),
    )
    assert getattr(e, name)(*args) is None


@pytest.mark.parametrize("name,args,unit,expected", LOOKUPS)
def test_wrong_price_units_rejected(monkeypatch, name, args, unit, expected):
    monkeypatch.setattr(
        e,
        "_get_pricing_client",
        lambda: Mock(get_products=Mock(return_value=offer("Requests"))),
    )
    assert getattr(e, name)(*args) is None


@pytest.mark.parametrize(
    "response",
    [
        offer("Hrs", "NaN"),
        offer("Hrs", "-1"),
        {"PriceList": ["not json"]},
        {"PriceList": [None]},
        {"PriceList": []},
        {"Incomplete": True, **offer()},
    ],
)
def test_invalid_catalog_offers_rejected(response):
    assert e._extract_price_per_unit(response) is None


def test_ambiguous_rates_not_selected_arbitrarily():
    response = offer()
    response["PriceList"] += offer(amount="0.2")["PriceList"]
    assert e._extract_price_per_unit(response) is None


def test_product_pagination_complete_or_explicitly_unknown(monkeypatch):
    client = Mock(
        get_products=Mock(side_effect=[{**offer(), "NextToken": "next"}, offer()])
    )
    monkeypatch.setattr(e, "_get_pricing_client", lambda: client)
    data = e._query_products("AmazonEC2", [])
    assert len(data["PriceList"]) == 2 and not data["Incomplete"]
    client.get_products = Mock(return_value={**offer(), "NextToken": "more"})
    assert e._query_products("AmazonEC2", [], max_pages=1)["Incomplete"]


def test_supported_rds_configuration_required(monkeypatch):
    client = Mock()
    monkeypatch.setattr(e, "_get_pricing_client", lambda: client)
    assert e._get_rds_monthly_price(None, "mysql", "us-east-1") is None
    assert e._get_rds_monthly_price("db.t3.micro", "oracle-ee", "us-east-1") is None
    client.get_products.assert_not_called()


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("EC2", {"instance_type": "t3.micro"}),
        ("EBS", {"volume_type": "gp3"}),
        ("ELB", {}),
        ("NLB", {}),
        ("ELB_CLASSIC", {}),
        ("RDS_INSTANCE", {"instance_class": "db.t3.micro", "engine": "mysql"}),
        ("EIP", {}),
        ("SNAPSHOT", {}),
    ],
)
def test_live_cache_retains_provenance(monkeypatch, kind, kwargs):
    names = {
        "EC2": "_get_ec2_hourly_price",
        "EBS": "_get_ebs_gb_month_price",
        "ELB": "_get_alb_hourly_price",
        "NLB": "_get_nlb_hourly_price",
        "ELB_CLASSIC": "_get_classic_elb_hourly_price",
        "RDS_INSTANCE": "_get_rds_monthly_price",
        "EIP": "_get_eip_monthly_price",
        "SNAPSHOT": "_get_snapshot_gb_month_price",
    }
    fn = Mock(return_value=0.3)
    monkeypatch.setattr(e, names[kind], fn)
    assert e.get_price(kind, "us-east-1", "live", **kwargs) == 0.3
    assert e.get_price(kind, "us-east-1", "live", **kwargs) == 0.3
    assert fn.call_count == 1 and e.PRICE_SOURCE.get() == "live-catalog"


def test_components_reconcile_and_preserve_unmeasured_costs(monkeypatch):
    monkeypatch.setenv(
        "PRICING_JSON",
        '{"EBS":{"gp3":0.1},"EBS_IOPS":{"gp3":0.01},"EBS_THROUGHPUT":{"gp3":0.02}}',
    )
    rows, total = e.estimate_monthly_waste(
        [
            {
                "type": "EBS",
                "id": "v",
                "region": "us-east-1",
                "size_gb": 100,
                "volume_type": "gp3",
                "iops": 4000,
                "throughput": 225,
            }
        ]
    )
    assert total == 22 and rows[0]["pricing"]["complete"]
    assert len(rows[0]["pricing"]["components"]) == 3


def test_stopped_iops_never_include_compute(monkeypatch):
    monkeypatch.setenv(
        "PRICING_JSON", '{"RDS_STORAGE":{"io1":0.1},"RDS_IOPS":{"io1":0.02}}'
    )
    rows, total = e.estimate_monthly_waste(
        [
            {
                "type": "RDS_INSTANCE",
                "id": "db",
                "region": "us-east-1",
                "reason": "stopped",
                "storage_gb": 100,
                "storage_type": "io1",
                "iops": 1000,
            }
        ]
    )
    assert total == 30 and not rows[0]["pricing"]["complete"]
    assert all("compute" not in c["name"] for c in rows[0]["pricing"]["components"])


def test_snapshot_billed_bytes_supported_without_capacity_assumption():
    rows, total = e.estimate_monthly_waste(
        [
            {
                "type": "SNAPSHOT",
                "id": "snap",
                "region": "us-east-1",
                "size_gb": 1000,
                "billed_storage_gb": 10,
            }
        ]
    )
    assert total == 0.5


def test_pricing_deadline_preserves_unpriced_rows(monkeypatch):
    monkeypatch.setattr(e, "check_deadline", Mock(side_effect=TimeoutError()))
    rows, total = e.estimate_monthly_waste(
        [
            {"type": "EIP", "id": "one", "region": "us-east-1"},
            {"type": "EIP", "id": "two", "region": "us-east-1"},
        ]
    )
    assert len(rows) == 2 and total == 0
    assert all(r["monthly_cost"] is None for r in rows)


def test_gp3_missing_throughput_is_an_incomplete_component():
    from cost_engine.estimator import estimate_monthly_waste

    rows, total = estimate_monthly_waste(
        [
            {
                "type": "EBS",
                "id": "volume",
                "region": "us-east-1",
                "volume_type": "gp3",
                "size_gb": 100,
                "iops": 3000,
            }
        ]
    )
    assert total is not None and rows[0]["monthly_cost"] is not None
    assert not rows[0]["pricing"]["complete"]
    assert "throughput unavailable" in rows[0]["pricing"]["notes"]


@pytest.mark.parametrize("region", ["us-east-1", "ap-south-1"])
def test_static_estimate_golden_values_for_supported_baselines(monkeypatch, region):
    """Lock the documented placeholder-rate math independently of lookup code."""
    monkeypatch.setenv("PRICING_MODE", "static")
    monkeypatch.delenv("PRICING_JSON", raising=False)
    e._PRICE_CACHE.clear()
    resources = [
        {
            "type": "EC2",
            "id": "i-small",
            "region": region,
            "instance_type": "t3.micro",
            "platform": "Linux/UNIX",
            "tenancy": "default",
            "purchasing": "on-demand",
        },
        {
            "type": "EBS",
            "id": "vol-gp3",
            "region": region,
            "volume_type": "gp3",
            "size_gb": 100,
            "iops": 3000,
            "throughput": 125,
        },
        {"type": "EIP", "id": "eip", "region": region},
        {"type": "ELB", "id": "alb", "region": region},
        {
            "type": "RDS_INSTANCE",
            "id": "db",
            "region": region,
            "engine": "mysql",
            "instance_class": "db.t3.micro",
            "multi_az": False,
        },
    ]

    rows, total = e.estimate_monthly_waste(resources)

    # Default placeholders: EC2 8.50/month, EBS 100 * .10, EIP 3.60,
    # ALB 18.00, and RDS db.t3.micro 13.00.
    assert [row["monthly_cost"] for row in rows] == [8.50, 10.00, 3.60, 18.00, 13.00]
    assert total == 53.10
    assert all(row["pricing"]["complete"] for row in rows)
