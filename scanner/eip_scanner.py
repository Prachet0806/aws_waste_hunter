from utils.scan_context import CURRENT, record_error, protected
# scanner/eip_scanner.py
import logging

from utils.aws_factory import get_client, client_region
from utils.aws_helpers import normalize_tags
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


def scan_unattached_eips():
    """Scan for unattached Elastic IP addresses."""
    ec2 = _get_ec2_client()
    region = client_region(ec2)
    unattached = []
    logger.info("Starting Elastic IP scan")

    try:
        for page in [ec2.describe_addresses()]:
            for a in ((page or {}).get("Addresses", []) or []):
                if not isinstance(a, dict):
                    continue
                associated = bool(
                    a.get("AssociationId")
                    or a.get("InstanceId")
                    or a.get("NetworkInterfaceId")
                    or a.get("NetworkInterfaceOwnerId") and a.get("AssociationId")
                )
                if associated:
                    continue

                allocation_id = a.get("AllocationId")
                public_ip = a.get("PublicIp", "") or ""
                # Stable id format: prefer AllocationId, else namespaced IP.
                stable_id = allocation_id if allocation_id else (f"public-ip/{public_ip}" if public_ip else None)
                if not stable_id:
                    logger.warning("Skipping EIP record without AllocationId or PublicIp")
                    continue
                unattached.append(build_resource(
                    "EIP", stable_id, region=region, az="",
                    reason="unattached",
                    tags=normalize_tags(a.get("Tags", []), source=f"eip:{stable_id}"),
                    public_ip=public_ip,
                    domain=a.get("Domain", "") or "",
                    allocation_id=allocation_id or "",
                ))

        logger.info(f"Found {len(unattached)} unattached Elastic IPs")
    except Exception as e:
        logger.error(f"Error scanning Elastic IPs: {e}", exc_info=True)
        raise

    return unattached
