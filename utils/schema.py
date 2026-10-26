"""Version 2 identities and canonical details, with generated legacy mirrors."""

import math

from utils.aws_helpers import normalize_tags


def resource_identity(resource):
    if resource.get("resource_key"):
        return resource["resource_key"]
    if resource.get("arn"):
        return resource["arn"]
    return ":".join(
        str(resource.get(k) or "unknown")
        for k in ("partition", "account_id", "region", "type", "id")
    )


def field(resource, name, default=None):
    return (resource.get("details") or {}).get(name, resource.get(name, default))


def build_resource(
    resource_type,
    resource_id,
    region=None,
    az="",
    arn=None,
    reason="",
    age_days=None,
    tags=None,
    **details,
):
    from utils.scan_context import CURRENT

    context = CURRENT.get()
    resource = {
        "schema_version": 2,
        "type": resource_type,
        "id": resource_id,
        "region": region or (context.config.region if context else None),
        "az": az or "",
        "account_id": context.account_id if context else "unknown",
        "partition": "aws",
        "reason": reason,
        "tags": normalize_tags(tags),
        "details": dict(details),
        "confidence": "review",
        "tags_status": "known",
    }
    if arn:
        resource["arn"] = arn
        parts = arn.split(":")
        if len(parts) >= 6:
            resource.update(partition=parts[1], account_id=parts[4], region=parts[3])
    if age_days is not None:
        try:
            value = float(age_days)
            if math.isfinite(value) and value >= 0:
                resource["age_days"] = value
        except (TypeError, ValueError):
            pass
    resource.update({k: v for k, v in details.items() if k not in resource})
    resource["resource_key"] = resource_identity(resource)
    return resource
