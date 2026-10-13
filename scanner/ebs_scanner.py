from utils.scan_context import CURRENT, record_error, protected
# scanner/ebs_scanner.py
import logging

from utils.aws_factory import get_client, client_region
from utils.aws_helpers import get_region_from_az, log_and_skip, normalize_tags
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_ec2_client = None


def _get_ec2_client():
    """Lazy initialization of EC2 client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("ec2")
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = get_client("ec2")
    return _ec2_client


def _stopped_instance_volume_ids(ec2):
    """Return volume IDs attached to stopped/stopping instances (real waste)."""
    stopped_vols = set()
    stopped_ids = set()
    try:
        paginator = ec2.get_paginator("describe_instances")
        try:
            pages = paginator.paginate(

            )
        except TypeError:
            pages = paginator.paginate()
        for page in pages:
            for r in (page.get("Reservations", []) or []):
                for inst in (r.get("Instances", []) or []):
                    if inst.get("State", {}).get("Name", "stopped") not in ("stopped", "stopping"):
                        continue
                    stopped_ids.add(inst.get("InstanceId"))
                    for mapping in (inst.get("BlockDeviceMappings", []) or []):
                        ebs = (mapping or {}).get("Ebs", {}) or {}
                        vid = ebs.get("VolumeId")
                        if vid:
                            stopped_vols.add(vid)
    except Exception as e:
        log_and_skip("ebs-stopped-join", "could not list stopped instances", e)
    ec2._stopped_ids = stopped_ids
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
                # Missing state cannot establish that a volume is unused.
                if not raw_state:
                    log_and_skip(vid, "volume state missing")
                    continue
                state = raw_state.lower()
                attachments = v.get("Attachments", []) or []
                on_stopped = bool(attachments) and vid in stopped_vols and all(
                    a.get("InstanceId") in getattr(ec2, "_stopped_ids", set()) for a in attachments)
                if state != "available" and not on_stopped:
                    continue
                if protected(normalize_tags(v.get("Tags", []))):
                    log_and_skip(vid, "protected resource")
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
                    iops=v.get("Iops"), throughput=v.get("Throughput"),
                    state=state or ("available" if not attachments else "in-use"),
                ))

        logger.info(f"Found {len(volumes)} wasted EBS volumes")
    except Exception as e:
        logger.error(f"Error scanning EBS volumes: {e}", exc_info=True)
        raise

    _ = client_region
    return volumes
