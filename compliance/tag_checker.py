# compliance/tag_checker.py
REQUIRED_TAGS = ["owner", "env", "cost-center"]

def check_tag_compliance(resources):
    violations = []

    for r in resources:
        tags = r.get("tags", {})

        missing = [t for t in REQUIRED_TAGS if t not in tags]

        if missing:
            violations.append({
                "resource_id": r["id"],
                "type": r["type"],
                "missing_tags": missing,
                "tags": tags
            })

    return violations
