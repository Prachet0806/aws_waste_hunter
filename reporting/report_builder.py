from jinja2 import Template

def build_report(resources, total_cost, violations, scan_errors=None, delivery_errors=None):
    scan_errors = scan_errors or []
    delivery_errors = delivery_errors or []
    template = Template("""
# AWS Waste Hunter — Weekly Cost Optimization Report

## Summary
**Total Monthly Waste:** ${{ total_cost }}

## Wasted Resources
{% for r in resources %}
- **{{ r.type }} {{ r.id }}** ({{ r.az }}): ${{ r.monthly_cost }}/month
{% endfor %}
{% if resources|length == 0 %}
- None detected
{% endif %}

## Tagging Violations
{% for v in violations %}
- **{{ v.type }} {{ v.resource_id }}** missing tags: {{ v.missing_tags }}
{% endfor %}
{% if violations|length == 0 %}
- None detected
{% endif %}

## Scan/Delivery Errors
{% for e in scan_errors %}
- Scan failure in {{ e.stage }}: {{ e.error }}
{% endfor %}
{% for e in delivery_errors %}
- Delivery failure in {{ e.stage }}: {{ e.error }}
{% endfor %}
{% if scan_errors|length == 0 and delivery_errors|length == 0 %}
- None
{% endif %}

## Recommended Actions
- Delete unattached EBS volumes  
- Stop or downsize idle EC2  
- Remove unused Load Balancers  
- Fix tagging for cost attribution  
""")

    return template.render(
        resources=resources,
        total_cost=total_cost,
        violations=violations,
        scan_errors=scan_errors,
        delivery_errors=delivery_errors,
    )
