# compliance/tag_checker.py
import logging
import os

logger = logging.getLogger(__name__)

# Default required tags (can be overridden via env var)
DEFAULT_REQUIRED_TAGS = ["owner", "env", "cost-center"]


def _get_required_tags():
    """Get required tags from environment or use defaults.

    Empty/whitespace-only REQUIRED_TAGS falls back to defaults with a warning
    (never silently disables compliance). Duplicates are removed, order kept.
    """
    if "REQUIRED_TAGS" in os.environ:
        tags_env = os.environ["REQUIRED_TAGS"]
        tags = [t.strip() for t in str(tags_env).split(",") if t.strip()]
        # Dedupe preserving order.
        seen = set()
        deduped = []
        for t in tags:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if not deduped:
            logger.warning("REQUIRED_TAGS is empty; falling back to defaults")
            return list(DEFAULT_REQUIRED_TAGS)
        logger.debug(f"Using required tags from env: {deduped}")
        return deduped
    return list(DEFAULT_REQUIRED_TAGS)


def _normalize_resource_tags(raw):
    """Coerce scanner tags (dict | list[{Key,Value}] | None) to dict."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, (list, tuple)):
        out = {}
        for t in raw:
            if not isinstance(t, dict):
                continue
            key = t.get("Key", t.get("key"))
            value = t.get("Value", t.get("value", ""))
            if key is not None:
                out[str(key)] = value
        return out
    return {}


def check_tag_compliance(resources):
    """Check resources for required tag compliance."""
    required_tags = _get_required_tags()
    violations = []

    resources = resources or []
    logger.debug(f"Checking {len(resources)} resources for tag compliance")

    for r in resources:
        if not isinstance(r, dict):
            continue
        tags = _normalize_resource_tags(r.get("tags"))

        missing = [t for t in required_tags if t not in tags]

        if missing:
            violations.append({
                "resource_id": r.get("id"),
                "type": r.get("type"),
                "missing_tags": missing,
                "tags": dict(tags),
            })

    logger.info(f"Found {len(violations)} tag compliance violations")
    return violations


def compute_compliance_stats(resources, violations=None):
    """Compute compliance score and breakdown from scan results.

    Returns a dict with total/compliant/violation counts, compliance percentage,
    violations by resource type, and counts of each missing tag.
    """
    resources = resources or []
    if violations is None:
        violations = check_tag_compliance(resources)
    violations = violations or []

    total = len(resources)
    violation_ids = {v.get("resource_id") for v in violations if isinstance(v, dict)}
    violation_ids.discard(None)
    compliant = total - len(violation_ids)
    if compliant < 0:
        logger.warning("More violating IDs than resources; check for mismatched inputs")
        compliant = 0
    percent = round((compliant / total * 100), 1) if total else 0.0

    by_type = {}
    missing_counts = {}
    for v in violations:
        if not isinstance(v, dict):
            continue
        rtype = v.get("type") or "UNKNOWN"
        by_type[rtype] = by_type.get(rtype, 0) + 1
        for tag in v.get("missing_tags", []) or []:
            missing_counts[tag] = missing_counts.get(tag, 0) + 1

    stats = {
        "total": total,
        "compliant": compliant,
        "violations": len(violations),
        "percent": percent,
        "by_type": by_type,
        "missing_counts": missing_counts,
    }
    logger.info(f"Compliance score: {percent}% ({compliant}/{total} compliant)")
    return stats
