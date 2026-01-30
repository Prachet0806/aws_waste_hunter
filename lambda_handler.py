# lambda_handler.py
import logging
from utils.logging_config import setup_logging

# Initialize logging first
logger = setup_logging()

from scanner.ebs_scanner import scan_unattached_ebs
from scanner.ec2_scanner import scan_idle_ec2
from scanner.elb_scanner import scan_unused_elb
from scanner.rds_scanner import scan_stopped_rds

from cost_engine.estimator import estimate_monthly_waste
from compliance.tag_checker import check_tag_compliance
from reporting.report_builder import build_report
from delivery.sns_sender import send_report
from delivery.s3_archiver import archive_report


def _safe_scan(name, func, errors):
    """Execute scanner with error handling."""
    try:
        logger.info(f"Starting {name}")
        result = func()
        logger.info(f"Completed {name}, found {len(result)} resources")
        return result
    except Exception as exc:
        logger.error(f"Error in {name}: {exc}", exc_info=True)
        errors.append({"stage": name, "error": str(exc), "type": type(exc).__name__})
        return []


def _safe_deliver(name, func, report, errors):
    """Execute delivery with error handling."""
    try:
        logger.info(f"Starting {name}")
        func(report)
        logger.info(f"Completed {name}")
    except Exception as exc:
        logger.error(f"Error in {name}: {exc}", exc_info=True)
        errors.append({"stage": name, "error": str(exc), "type": type(exc).__name__})


def handler(event, context):
    """Main Lambda handler for AWS Waste Hunter."""
    logger.info("AWS Waste Hunter started")
    logger.info(f"Event: {event}")
    
    resources = []
    scan_errors = []
    delivery_errors = []

    # Run all scanners
    resources += _safe_scan("scan_unattached_ebs", scan_unattached_ebs, scan_errors)
    resources += _safe_scan("scan_idle_ec2", scan_idle_ec2, scan_errors)
    resources += _safe_scan("scan_unused_elb", scan_unused_elb, scan_errors)
    resources += _safe_scan("scan_stopped_rds", scan_stopped_rds, scan_errors)

    logger.info(f"Total resources found: {len(resources)}")

    # Estimate costs
    try:
        estimated, total = estimate_monthly_waste(resources)
        logger.info(f"Cost estimation complete, total waste: ${total}")
    except Exception as e:
        logger.error(f"Error estimating costs: {e}", exc_info=True)
        scan_errors.append({"stage": "cost_estimation", "error": str(e), "type": type(e).__name__})
        estimated, total = [], 0

    # Check compliance
    try:
        violations = check_tag_compliance(estimated)
        logger.info(f"Found {len(violations)} tag compliance violations")
    except Exception as e:
        logger.error(f"Error checking compliance: {e}", exc_info=True)
        scan_errors.append({"stage": "tag_compliance", "error": str(e), "type": type(e).__name__})
        violations = []

    # Build report
    try:
        report = build_report(estimated, total, violations, scan_errors, delivery_errors)
    except Exception as e:
        logger.error(f"Error building report: {e}", exc_info=True)
        report = f"Error building report: {e}"

    # Deliver report
    _safe_deliver("send_report", send_report, report, delivery_errors)
    _safe_deliver("archive_report", archive_report, report, delivery_errors)

    result = {
        "status": "ok" if not scan_errors and not delivery_errors else "partial",
        "resources": len(resources),
        "monthly_waste": total,
        "scan_errors": len(scan_errors),
        "delivery_errors": len(delivery_errors),
    }
    
    logger.info(f"AWS Waste Hunter completed: {result}")
    return result
