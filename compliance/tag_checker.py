# compliance/tag_checker.py
import logging
import os

logger = logging.getLogger(__name__)

# Default required tags (can be overridden via env var)
DEFAULT_REQUIRED_TAGS = ["owner", "env", "cost-center"]


def _get_required_tags():
    """Get required tags from environment or use defaults."""
    # Check if env var exists (distinguish between unset and empty string)
    if "REQUIRED_TAGS" in os.environ:
        tags_env = os.environ["REQUIRED_TAGS"]
        tags = [t.strip() for t in tags_env.split(",") if t.strip()]
        logger.info(f"Using required tags from env: {tags}")
        return tags
    return DEFAULT_REQUIRED_TAGS


def check_tag_compliance(resources):
    """Check resources for required tag compliance."""
    required_tags = _get_required_tags()
    violations = []

    logger.info(f"Checking {len(resources)} resources for tag compliance")
    logger.info(f"Required tags: {required_tags}")

    for r in resources:
        tags = r.get("tags", {})

        missing = [t for t in required_tags if t not in tags]

        if missing:
            violations.append({
                "resource_id": r.get("id"),
                "type": r.get("type"),
                "missing_tags": missing,
                "tags": tags
            })

    logger.info(f"Found {len(violations)} tag compliance violations")
    return violations
