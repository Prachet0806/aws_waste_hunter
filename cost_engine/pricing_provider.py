"""Billable-component estimates; unknown values never become fabricated savings."""

import math
from decimal import Decimal

from utils.schema import field


def estimate(resource, mode, lookup, config):
    kind = resource.get("type")
    region = resource.get("region")
    info = {
        "source": "static-approximation",
        "complete": True,
        "currency": "USD",
        "basis": "list-price-opportunity-not-realized-savings",
        "components": [],
        "notes": [],
    }
    if not region:
        return None, {
            **info,
            "complete": False,
            "source": "unknown",
            "notes": ["region missing"],
        }

    def unknown(reason):
        return None, {**info, "source": "unknown", "complete": False, "notes": [reason]}

    def price(price_type, **kwargs):
        value = lookup(price_type, region, mode, **kwargs)
        if value is None or not math.isfinite(float(value)) or value < 0:
            return None
        # Cache source is determined by the lookup outcome, not the requested mode.
        from cost_engine import estimator

        live = estimator.PRICE_SOURCE.get() == "live-catalog"
        info["source"] = "live-catalog" if live else "static-approximation"
        if mode == "live" and not live:
            info["notes"].append("live lookup unavailable; static fallback")
        return Decimal(str(value))

    def component(name, quantity, rate, unit):
        if quantity is None or rate is None:
            info["complete"] = False
            info["notes"].append(f"{name} unavailable")
            return None
        quantity, rate = Decimal(str(quantity)), Decimal(str(rate))
        if not quantity.is_finite() or not rate.is_finite() or quantity < 0 or rate < 0:
            raise ValueError(
                "Component quantities and prices must be finite and nonnegative"
            )
        amount = quantity * rate
        info["components"].append(
            {
                "name": name,
                "quantity": str(quantity),
                "rate": str(rate),
                "unit": unit,
                "monthly_amount": str(amount),
            }
        )
        return amount

    if kind == "EC2":
        identity = "|".join(
            str(v)
            for v in (
                region,
                field(resource, "platform", "Linux/UNIX"),
                field(resource, "tenancy", "default"),
                field(resource, "purchasing", "on-demand"),
                field(resource, "instance_type"),
            )
        )
        if identity in config.get("EC2_CONFIGS", {}):
            info["source"] = "configured-deployment-rate"
            return component(
                "compute", 1, config["EC2_CONFIGS"][identity], "USD/month"
            ), info
        if (
            field(resource, "platform", "Linux/UNIX") not in ("Linux/UNIX", "Linux")
            or field(resource, "tenancy", "default") != "default"
            or field(resource, "purchasing", "on-demand") != "on-demand"
        ):
            return unknown("Only Linux shared on-demand baseline is supported")
        cost = component(
            "compute",
            730,
            price("EC2", instance_type=field(resource, "instance_type")),
            "USD/hour",
        )
    elif kind == "EBS":
        volume_type = field(resource, "volume_type", "gp3")
        cost = component(
            "storage",
            field(resource, "size_gb"),
            price("EBS", volume_type=volume_type),
            "USD/GB-month",
        )
        if volume_type in ("gp3", "io1", "io2"):
            iops = field(resource, "iops")
            extra = (
                max(0, iops - (3000 if volume_type == "gp3" else 0))
                if iops is not None
                else None
            )
            if extra is None or extra:
                add = component(
                    "provisioned IOPS",
                    extra,
                    config.get("EBS_IOPS", {}).get(volume_type),
                    "USD/IOPS-month",
                )
                if cost is not None and add is not None:
                    cost += add
            throughput = field(resource, "throughput")
            if volume_type == "gp3" and (throughput is None or throughput > 125):
                extra = throughput - 125 if throughput is not None else None
                add = component(
                    "throughput",
                    extra,
                    config.get("EBS_THROUGHPUT", {}).get(volume_type),
                    "USD/MBps-month",
                )
                if cost is not None and add is not None:
                    cost += add
    elif kind in ("RDS", "RDS_CLUSTER", "RDS_INSTANCE"):
        if resource.get("reason") == "stopped":
            cost = component(
                "stopped storage",
                field(resource, "storage_gb"),
                config.get("RDS_STORAGE", {}).get(field(resource, "storage_type")),
                "USD/GB-month",
            )
            if field(resource, "iops", 0):
                add = component(
                    "stopped IOPS",
                    field(resource, "iops"),
                    config.get("RDS_IOPS", {}).get(field(resource, "storage_type")),
                    "USD/IOPS-month",
                )
                if cost is not None and add is not None:
                    cost += add
            info["complete"] = False
            info["notes"].append(
                "Backup storage attribution unavailable; stopped compute excluded"
            )
        elif kind != "RDS_INSTANCE":
            return unknown(
                "Cluster compute belongs to member instances; cluster storage unmeasured"
            )
        elif "|".join(
            str(v)
            for v in (
                region,
                field(resource, "engine"),
                field(resource, "instance_class"),
                "Multi-AZ" if field(resource, "multi_az", False) else "Single-AZ",
            )
        ) in config.get("RDS_CONFIGS", {}):
            identity = "|".join(
                str(v)
                for v in (
                    region,
                    field(resource, "engine"),
                    field(resource, "instance_class"),
                    "Multi-AZ" if field(resource, "multi_az", False) else "Single-AZ",
                )
            )
            info["source"] = "configured-deployment-rate"
            cost = component("compute", 1, config["RDS_CONFIGS"][identity], "USD/month")
        elif field(resource, "multi_az", False) or str(
            field(resource, "engine", "")
        ).startswith("aurora"):
            return unknown(
                "Deployment-specific RDS price requires an explicit supported catalog match"
            )
        else:
            cost = component(
                "compute",
                1,
                price(
                    "RDS_INSTANCE",
                    instance_class=field(resource, "instance_class"),
                    engine=field(resource, "engine"),
                ),
                "USD/month",
            )
            info["notes"].append(
                "Compute opportunity only; storage and commitments excluded"
            )
    elif kind == "SNAPSHOT":
        stored = field(resource, "billed_storage_gb")
        if stored is None:
            return unknown(
                "Incremental stored bytes and deletion savings are unmeasured"
            )
        cost = component("snapshot storage", stored, price("SNAPSHOT"), "USD/GB-month")
    elif kind == "EIP":
        cost = component("IPv4", 1, price("EIP"), "USD/month")
    elif kind == "ELB":
        price_type = {"NETWORK": "NLB", "CLASSIC": "ELB_CLASSIC"}.get(
            str(field(resource, "lb_type", "APPLICATION")).upper(), "ELB"
        )
        cost = component("base LB", 730, price(price_type), "USD/hour")
        info["notes"].append(
            "Base rate only; capacity units and reserved capacity excluded"
        )
    else:
        return unknown("Unsupported resource type")
    if cost is None:
        info.update(source="unknown", complete=False)
    return cost, info
