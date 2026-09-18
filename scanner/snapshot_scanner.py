# scanner/snapshot_scanner.py
import logging
import os
from datetime import datetime, timezone

from utils.aws_factory import get_client, client_region
from utils.aws_helpers import log_and_skip, normalize_tags
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_ec2_client = None

DEFAULT_MIN_AGE_DAYS = 7


def _get_min_age_days():
    """Get minimum snapshot age (days) from env var with validation."""
    try:
        raw = os.getenv("MIN_SNAPSHOT_AGE_DAYS", str(DEFAULT_MIN_AGE_DAYS))
        value = int(float(raw)) if isinstance(raw, str) and "." in raw else int(raw)
        if value < 0:
            logger.warning(f"Invalid MIN_SNAPSHOT_AGE_DAYS {raw}, using default")
            return DEFAULT_MIN_AGE_DAYS
        return value
    except (ValueError, TypeError):
        logger.warning("Invalid MIN_SNAPSHOT_AGE_DAYS format, using default")
        return DEFAULT_MIN_AGE_DAYS


def _get_ec2_client():
    """Lazy initialization of EC2 client (delegates to shared factory)."""
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = get_client("ec2")
    return _ec2_client


def _collect_ami_snapshot_ids(ec2):
    """Collect all SnapshotIds referenced by self-owned AMIs (fail-open)."""
    referenced = set()
    try:
        paginator = ec2.get_paginator("describe_images")
    except Exception as e:
        log_and_skip("ami-snapshots", "describe_images paginator unavailable", e)
        return referenced
    try:
        for page in paginator.paginate(Owners=["self"]):
            for image in ((page or {}).get("Images", []) or []):
                for mapping in ((image or {}).get("BlockDeviceMappings", []) or []):
                    ebs = (mapping or {}).get("Ebs", {}) or {}
                    snapshot_id = ebs.get("SnapshotId")
                    if snapshot_id:
                        referenced.add(snapshot_id)
    except Exception as e:
        log_and_skip("ami-snapshots", "describe_images paginate error", e)
    return referenced


def scan_orphaned_snapshots():
    """Scan for self-owned EBS snapshots not referenced by any AMI and past minimum age."""
    ec2 = _get_ec2_client()
    region = client_region(ec2)
    min_age_days = _get_min_age_days()
    orphaned = []
    logger.info("Starting orphaned snapshot scan")

    try:
        referenced = _collect_ami_snapshot_ids(ec2)
        logger.info(f"Found {len(referenced)} snapshot references from AMIs")

        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - min_age_days * 86400
        paginator = ec2.get_paginator("describe_snapshots")

        for page in paginator.paginate(OwnerIds=["self"]):
            for s in ((page or {}).get("Snapshots", []) or []):
                if not isinstance(s, dict):
                    continue
                sid = s.get("SnapshotId")
                if not sid:
                    logger.warning("Skipping snapshot record without SnapshotId")
                    continue
                if sid in referenced:
                    continue

                start_time = s.get("StartTime")
                if start_time is None:
                    # Fail-open skip with explicit log (Decision 1).
                    log_and_skip(sid, "missing StartTime")
                    continue
                try:
                    start_ts = start_time.timestamp()
                except Exception:
                    log_and_skip(sid, "unparseable StartTime")
                    continue
                if start_ts > cutoff:
                    logger.debug(f"Snapshot {sid} younger than {min_age_days}d, skipping")
                    continue

                try:
                    size = int(s.get("VolumeSize", 0) or 0)
                except (TypeError, ValueError):
                    size = 0
                try:
                    days_old = round((now - start_time).total_seconds() / 86400, 1)
                except Exception:
                    days_old = round((now.timestamp() - start_ts) / 86400, 1)
                orphaned.append(build_resource(
                    "SNAPSHOT", sid, region=region, az="",
                    reason="orphaned-old-snapshot",
                    age_days=days_old,
                    tags=normalize_tags(s.get("Tags", []), source=f"snap:{sid}"),
                    size_gb=size,
                    volume_id=s.get("VolumeId", "") or "",
                    created_at=start_time.isoformat() if hasattr(start_time, "isoformat") else "",
                    days_old=days_old,
                ))

        logger.info(f"Found {len(orphaned)} orphaned snapshots")
    except Exception as e:
        logger.error(f"Error scanning snapshots: {e}", exc_info=True)
        raise

    return orphaned
