import logging
import math
import html
from copy import deepcopy

from jinja2 import Template

logger = logging.getLogger(__name__)

TIER_ORDER = ["<7d", "7d-30d", "30d-90d", "90d+", "Unknown"]
MAX_TABLE_ROWS = 500


def _get_age_tier(days):
    """Categorize resource age into tiers."""
    if days is None:
        return "Unknown"
    try:
        days = float(days)
    except (TypeError, ValueError):
        return "Unknown"
    if days < 0:
        return "<7d"
    if days >= 90:
        return "90d+"
    if days >= 30:
        return "30d-90d"
    if days >= 7:
        return "7d-30d"
    return "<7d"


def _resource_age_days(r):
    """Read age from normalized age_days, then legacy days_idle/days_old."""
    if not isinstance(r, dict):
        return None
    for key in ("age_days", "days_idle", "days_old"):
        val = r.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    details = r.get("details")
    if isinstance(details, dict):
        for key in ("age_days", "days_idle", "days_old"):
            val = details.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _safe_cost(r):
    try:
        value = float(r.get("monthly_cost", 0) or 0)
        return value if math.isfinite(value) and value >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _escape_md(value):
    if value is None:
        return ""
    text = html.escape(str(value))
    return text.replace("|", "\\|").replace("\n", " ").strip()


REPORT_TEMPLATE = Template("""# AWS Waste Hunter - Cost Optimization Report

## Summary
**Priced Monthly Subtotal (approximate list prices):** {% if total_available %}${{ "%.2f"|format(total_cost) }}{% else %}Unavailable{% endif %}
**Resources Found:** {{ resources|length }}{% if truncated %} (showing {{ resources|length }} of {{ total_resources }}){% endif %}
**Tag Violations:** {{ violations|length }}
{% if compliance_stats.get('percent') is not none %}**Compliance Score:** {{ compliance_stats.get('percent') }}%
{% endif %}**Scan Errors:** {{ scan_errors|length }}
**Delivery Errors Known at Report Generation:** {{ delivery_errors|length }}

## Resource Age Tiering
| Tier | Count | Monthly Cost |
|------|-------|--------------|
{% for tier in tier_order %}{% if tier in tier_counts %}| {{ tier }} | {{ tier_counts[tier] }} | ${{ "%.2f"|format(tier_costs.get(tier, 0)) }} |
{% endif %}{% endfor %}

## Wasted Resources
{% for r in resources %}
- **{{ r.type }} {{ r.id }}**{% if r.reason %} ({{ r.reason }}){% endif %}{% if r.details_text %} - {{ r.details_text }}{% endif %}{% if r.age_tier %} [{{ r.age_tier }}]{% endif %}
  - Location: {{ r.location }}
  - Cost: {{ r.cost_text }}
{% endfor %}
{% if resources|length == 0 %}
- None detected
{% endif %}
{% if truncated %}
- ... {{ truncated }} more rows in full S3 archive
{% endif %}

## Tagging Violations
{% for v in violations %}
- **{{ v.type }} {{ v.resource_id }}**
  - Missing tags: {{ v.missing_tags|join(', ') }}
  {% if v.tags %}
  - Existing tags: {{ v.tags.keys()|list|join(', ') }}
  {% endif %}
{% endfor %}
{% if violations|length == 0 %}
- None detected
{% endif %}
{% if compliance_stats %}
### Compliance Breakdown
- {{ compliance_stats.get('compliant') }}/{{ compliance_stats.get('total') }} compliant ({{ compliance_stats.get('percent') }}%)
{% if compliance_stats.get('by_type') %}
- Violations by type: {% for t, c in compliance_stats.get('by_type').items() %}{{ t }}: {{ c }}{% if not loop.last %}, {% endif %}{% endfor %}
{% endif %}
{% if compliance_stats.get('missing_counts') %}
- Most-missing tags: {% for t, c in compliance_stats.get('missing_counts').items() %}{{ t }}: {{ c }}{% if not loop.last %}, {% endif %}{% endfor %}
{% endif %}
{% endif %}

## Scan/Delivery Errors
{% for e in scan_errors %}
- **Scan failure** in {{ e.stage }}
  - Error: {{ e.error }}
  {% if e.type %}
  - Type: {{ e.type }}
  {% endif %}
{% endfor %}
{% for e in delivery_errors %}
- **Delivery failure** in {{ e.stage }}
  - Error: {{ e.error }}
  {% if e.type %}
  - Type: {{ e.type }}
  {% endif %}
{% endfor %}
{% if scan_errors|length == 0 and delivery_errors|length == 0 %}
- None
{% endif %}

## Recommended Actions
- **EBS volumes:** Verify every attachment and retention requirement; obtain owner approval before cleanup
- **EC2 instances:** Stop or downsize idle instances, notify owners
- **Load Balancers:** Remove unused ALB/NLB/Classic LBs
- **Elastic IPs:** Release unattached EIPs (you are billed while they are unassociated)
- **Snapshots:** Review retention, backup ownership, sharing and recovery requirements; AMI absence does not establish safe deletion
- **RDS:** Verify retention and obtain owner approval; stopped compute is not billed
- **Tags:** Fix missing tags for cost attribution and ownership
- **Errors:** Review scan/delivery errors above and check IAM permissions

---
*Report generated by AWS Waste Hunter*
""")


def _summarize_details(r):
    """Compact one-line metric summary from details/legacy keys."""
    bits = []
    get = lambda k: r.get(k) if r.get(k) not in (None, "") else (r.get("details", {}) or {}).get(k)
    if r.get("pricing"):
        bits.append("price: " + str(r["pricing"].get("source", "unknown")))
        bits.extend(r["pricing"].get("notes", []))
    if (r.get("tags") or {}).get("owner"):
        bits.append("owner: " + str(r["tags"]["owner"]))
    if get("avg_cpu") is not None:
        bits.append(f"CPU: {get('avg_cpu')}%")
    if get("max_connections") is not None:
        bits.append(f"conns: {get('max_connections')}")
    if get("instance_type"):
        bits.append(str(get("instance_type")))
    if get("instance_class"):
        bits.append(str(get("instance_class")))
    if get("public_ip"):
        bits.append(str(get("public_ip")))
    if get("size_gb") not in (None, 0, "0"):
        bits.append(f"{get('size_gb')}GB")
    if get("days_old") not in (None, 0, "0", ""):
        bits.append(f"{get('days_old')}d old")
    if get("lb_type"):
        bits.append(str(get("lb_type")))
    return ", ".join(bits)


def build_report(resources, total_cost, violations, scan_errors=None, delivery_errors=None, compliance_stats=None):
    """Build formatted Markdown report from scan results (no input mutation)."""
    scan_errors = list(scan_errors or [])
    delivery_errors = list(delivery_errors or [])
    compliance_stats = dict(compliance_stats or {})
    resources = [deepcopy(r) if isinstance(r, dict) else {} for r in (resources or [])]
    violations = list(violations or [])

    total_available = total_cost is not None
    try:
        total_cost = float(total_cost or 0)
    except (TypeError, ValueError):
        total_cost = 0.0

    logger.info(f"Building report with {len(resources)} resources, {len(violations)} violations")

    # Annotate copies (never mutate caller input).
    for r in resources:
        r["age_tier"] = _get_age_tier(_resource_age_days(r))

    # Group resources by age tier for summary (sorted deterministically).
    tier_counts = {}
    tier_costs = {}
    for r in resources:
        tier = r.get("age_tier", "Unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        tier_costs[tier] = tier_costs.get(tier, 0) + _safe_cost(r)

    total_resources = len(resources)
    truncated = 0
    resources.sort(key=lambda r: (-_safe_cost(r), str(r.get("resource_key", r.get("id", "")))))

    # Pre-format rows (escapes markdown-breaking chars, avoids Jinja Undefined leaks).
    rows = []
    for r in resources:
        az = _escape_md(r.get("az", ""))
        region = _escape_md(r.get("region", ""))
        location = az
        if region:
            location = f"{az} ({region})" if az else region
        if not location:
            location = "unknown"
        rows.append({
            "type": _escape_md(r.get("type", "UNKNOWN")),
            "id": _escape_md(r.get("id", "unknown")),
            "reason": _escape_md(r.get("reason", "")),
            "details_text": _escape_md(_summarize_details(r)),
            "age_tier": _escape_md(r.get("age_tier", "")),
            "location": location,
            "cost": f"{_safe_cost(r):.2f}" if r.get("monthly_cost") is not None else "unknown",
            "cost_text": f"${_safe_cost(r):.2f}/month" if r.get("monthly_cost") is not None else "Unavailable",
        })

    safe_violations = []
    for v in violations:
        if not isinstance(v, dict):
            continue
        safe_violations.append({
            "type": _escape_md(v.get("type", "UNKNOWN")),
            "resource_id": _escape_md(v.get("resource_id", "unknown")),
            "missing_tags": [_escape_md(t) for t in (v.get("missing_tags", []) or [])],
            "tags": {_escape_md(k): _escape_md(val) for k, val in ((v.get("tags", {}) or {}).items())} if isinstance(v.get("tags"), dict) else {},
        })

    def _safe_err(e):
        if not isinstance(e, dict):
            return {"stage": "unknown", "error": _escape_md(e), "type": ""}
        return {"stage": _escape_md(e.get("stage", "unknown")), "error": _escape_md(e.get("error", "")),
                "type": _escape_md(e.get("type", ""))}

    try:
        return REPORT_TEMPLATE.render(
            resources=rows,
            total_resources=total_resources,
            truncated=truncated,
            total_cost=total_cost, total_available=total_available,
            violations=safe_violations,
            scan_errors=[_safe_err(e) for e in scan_errors],
            delivery_errors=[_safe_err(e) for e in delivery_errors],
            compliance_stats={
                "compliant": compliance_stats.get("compliant"),
                "total": compliance_stats.get("total"),
                "percent": compliance_stats.get("percent"),
                "by_type": compliance_stats.get("by_type", {}),
                "missing_counts": compliance_stats.get("missing_counts", {}),
            } if compliance_stats else {},
            tier_counts=tier_counts,
            tier_costs=tier_costs,
            tier_order=TIER_ORDER,
        )
    except Exception as e:
        logger.error(f"Error rendering report template: {e}", exc_info=True)
        raise


def build_notification(result, archive_uri=None):
    status = result['status']
    total = result.get('monthly_waste')
    amount = f"${total:.2f}" if total is not None else "unavailable"
    return (f"AWS Waste Hunter run {result.get('run_id', 'manual')}\n"
            f"Status: {status}; scope: {result.get('account_id')}/{result.get('region')}\n"
            f"Priced monthly subtotal: {amount}; unpriced findings: {result.get('unpriced_resources', 0)}\n"
            f"Coverage errors: {result.get('scan_errors', 0)}; skipped: {result.get('no_metrics', 0)}\n"
            + (f"Complete archive: {archive_uri}" if archive_uri else "Archive unavailable; operational failure."))
