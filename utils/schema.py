# utils/schema.py
"""Normalized waste-resource schema (Decision 3: breaking change allowed).

Base fields every scanner must return:
  {type, id, region, az?, arn?, reason, age_days?, tags, details}

- type: EC2 | EBS | EIP | ELB | RDS_CLUSTER | RDS_INSTANCE | SNAPSHOT | RDS_IDLE_*
- reason: machine-readable cause, e.g. idle-low-cpu-network, unattached,
  unused-zero-traffic, stopped, idle-low-cpu-connections, orphaned-old-snapshot
- details: scanner-specific metrics (avg_cpu, size_gb, ...) — keeps base stable.
- Legacy top-level metric keys (avg_cpu, size_gb, ...) are ALSO kept for
  estimator/report backward-compat during migration; new code should read
  details first, then top-level fallback.
"""
import logging

logger = logging.getLogger(__name__)


def build_resource(resource_type, resource_id, region=None, az="", arn=None,
                   reason="", age_days=None, tags=None, **details):
    tags = dict(tags) if tags else {}
    resource = {
        "type": resource_type,
        "id": resource_id,
        "region": region,
        "az": az or "",
        "reason": reason,
        "tags": tags,
        "details": dict(details),
    }
    if arn:
        resource["arn"] = arn
    if age_days is not None:
        try:
            resource["age_days"] = float(age_days)
        except (TypeError, ValueError):
            pass
    # Legacy mirror: keep metric keys top-level so estimator/report keep working.
    for k, v in details.items():
        if k not in resource:
            resource[k] = v
    return resource
