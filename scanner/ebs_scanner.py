# scanner/ebs_scanner.py
import logging

from utils.aws_factory import get_client, client_region
from utils.aws_helpers import get_region_from_az, log_and_skip, normalize_tags
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_ec2_client = None


def _get_ec2_client():
    """Lazy initialization of EC2 client (delegates to shared factory)."""
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = get_client("ec2")
    return _ec2_client


def _stopped_instance_volume_ids(ec2):
    """Return volume IDs attached to stopped/stopping instances (real waste)."""
    stopped_vols = set()
    try:
        paginator = ec2.get_paginator("describe_instances")
        try:
            pages = paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["stopped", "stopping"]}]
            )
        except TypeError:
            pages = paginator.paginate()
        for page in pages:
            for r in (page.get("Reservations", []) or []):
                for inst in (r.get("Instances", []) or []):
                    for mapping in (inst.get("BlockDeviceMappings", []) or []):
                        ebs = (mapping or {}).get("Ebs", {}) or {}
                        vid = ebs.get("VolumeId")
                        if vid:
                            stopped_vols.add(vid)
    except Exception as e:
        log_and_skip("ebs-stopped-join", "could not list stopped instances", e)
    return stopped_vols


def scan_unattached_ebs():
    """Scan for unattached EBS volumes + in-use volumes on stopped instances."""
    ec2 = _get_ec2_client()
    volumes = []
    logger.info("Starting EBS volume scan")

    try:
        stopped_vols = _stopped_instance_volume_ids(ec2)
        paginator = ec2.get_paginator("describe_volumes")

        try:
            pages = paginator.paginate()
        except TypeError:
            pages = paginator.paginate()

        for page in pages:
            for v in ((page or {}).get("Volumes", []) or []):
                if not isinstance(v, dict) or not v.get("VolumeId"):
                    logger.warning("Skipping malformed volume record")
                    continue
                vid = v["VolumeId"]
                raw_state = v.get("State", "")
                # Legacy API filter was status=available; records without an
                # explicit State (e.g. unit fakes) are treated as available.
                state = (raw_state or "available").lower()
                attachments = v.get("Attachments", []) or []
                on_stopped = vid in stopped_vols
                if state != "available" and not on_stopped:
                    continue
                az = v.get("AvailabilityZone", "") or ""
                region = get_region_from_az(az)
                if region is None and az:
                    logger.warning(f"Could not derive region from AZ {az!r} for {vid}")
                reason = "unattached-available" if state == "available" else "attached-to-stopped-instance"
                try:
                    size = int(v.get("Size", 0) or 0)
                except (TypeError, ValueError):
                    size = 0
                volumes.append(build_resource(
                    "EBS", vid, region=region, az=az,
                    reason=reason,
                    tags=normalize_tags(v.get("Tags", []), source=f"ebs:{vid}"),
                    size_gb=size,
                    volume_type=v.get("VolumeType", ""),
                    state=state or ("available" if not attachments else "in-use"),
                ))

        logger.info(f"Found {len(volumes)} wasted EBS volumes")
    except Exception as e:
        logger.error(f"Error scanning EBS volumes: {e}", exc_info=True)
        raise

    _ = client_region
    return volumes
