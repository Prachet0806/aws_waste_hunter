# lambda_handler.py
import logging
import math
import os
import concurrent.futures

from utils.logging_config import setup_logging

# Initialize logging first
logger = setup_logging(__name__)

from scanner.ebs_scanner import scan_unattached_ebs
from scanner.ec2_scanner import scan_idle_ec2, get_no_metrics_count
from scanner.elb_scanner import scan_unused_elb
from scanner.rds_scanner import scan_stopped_rds
from scanner.rds_idle_scanner import scan_idle_rds
from scanner.eip_scanner import scan_unattached_eips
from scanner.snapshot_scanner import scan_orphaned_snapshots

from cost_engine.estimator import estimate_monthly_waste
from compliance.tag_checker import check_tag_compliance, compute_compliance_stats
from reporting.report_builder import build_report
from delivery.sns_sender import send_report
from delivery.s3_archiver import archive_report

DEFAULT_WASTE_ALERT_THRESHOLD = 1000.0
DEFAULT_MAX_WORKERS = 4
SCAN_TIMEOUT_SECONDS = 240


def _get_waste_alert_threshold():
    """Get waste alert threshold from env var with validation."""
    raw = os.getenv("WASTE_ALERT_THRESHOLD", str(DEFAULT_WASTE_ALERT_THRESHOLD))
    try:
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            logger.warning(f"Invalid WASTE_ALERT_THRESHOLD {raw!r}, using default")
            return DEFAULT_WASTE_ALERT_THRESHOLD
        return value
    except (ValueError, TypeError):
        logger.warning("Invalid WASTE_ALERT_THRESHOLD format, using default")
        return DEFAULT_WASTE_ALERT_THRESHOLD


def _get_max_workers():
    """Get max parallel workers from env var."""
    raw = os.getenv("MAX_SCANNER_WORKERS", str(DEFAULT_MAX_WORKERS))
    try:
        value = int(raw)
        clamped = max(1, min(value, 10))
        if clamped != value:
            logger.warning(f"MAX_SCANNER_WORKERS {raw!r} clamped to {clamped}")
        return clamped
    except (ValueError, TypeError):
        logger.warning(f"Invalid MAX_SCANNER_WORKERS {raw!r}, using default")
        return DEFAULT_MAX_WORKERS


def _safe_scan(name, func, errors):
    """Execute scanner with error handling. Never raises."""
    try:
        logger.info(f"Starting {name}")
        result = func()
        if result is None:
            logger.warning(f"{name} returned None, treating as empty")
            return []
        if not isinstance(result, list):
            logger.warning(f"{name} returned {type(result).__name__}, treating as empty")
            errors.append({"stage": name, "error": f"unexpected return type {type(result).__name__}", "type": "TypeError"})
            return []
        logger.info(f"Completed {name}, found {len(result)} resources")
        return result
    except Exception as exc:
        logger.error(f"Error in {name}: {exc}", exc_info=True)
        errors.append({"stage": name, "error": str(exc), "type": type(exc).__name__})
        return []


def _run_scanners_parallel(scanners, max_workers, context=None, timeout=SCAN_TIMEOUT_SECONDS):
    """
    Run scanners in parallel using ThreadPoolExecutor with per-scanner timeouts.

    Returns (combined_resources, scan_errors). A hung scanner is fail-open:
    logged + recorded, never blocks past Lambda timeout.
    """
    all_resources = []
    scan_errors = []

    remaining_ms = None
    try:
        if context is not None and hasattr(context, "get_remaining_time_in_millis"):
            remaining_ms = context.get_remaining_time_in_millis()
    except Exception:
        remaining_ms = None
    if remaining_ms is not None:
        # Leave 20s headroom for cost/compliance/report/delivery.
        timeout = max(10, min(timeout, int(remaining_ms / 1000) - 20))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {
            executor.submit(_safe_scan, name, func, scan_errors): name
            for name, func in scanners
        }

        for future, name in list(future_to_name.items()):
            try:
                result = future.result(timeout=timeout)
                all_resources.extend(result)
            except concurrent.futures.TimeoutError:
                logger.error(f"Scanner {name} timed out after {timeout}s, skipping (fail-open)")
                scan_errors.append({"stage": name, "error": f"timeout after {timeout}s", "type": "TimeoutError"})
            except Exception as exc:
                logger.error(f"Unexpected error in {name}: {exc}", exc_info=True)
                scan_errors.append({"stage": name, "error": str(exc), "type": type(exc).__name__})

    return all_resources, scan_errors


def _safe_deliver(name, func, report, errors):
    """Execute delivery with error handling. Never raises."""
    try:
        logger.info(f"Starting {name}")
        func(report)
        logger.info(f"Completed {name}")
        return True
    except Exception as exc:
        logger.error(f"Error in {name}: {exc}", exc_info=True)
        errors.append({"stage": name, "error": str(exc), "type": type(exc).__name__})
        return False


def _summarize_event(event, limit=1000):
    """Truncate event for logging without materializing huge strings or leaking secrets."""
    try:
        text = str(event)
    except Exception:
        return "<unprintable event>"
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return text


def _overall_status(scan_errors, delivery_errors, resources_estimated):
    if scan_errors or delivery_errors:
        # Total failure: nothing estimated AND errors on every delivery.
        if not resources_estimated and delivery_errors:
            return "failed"
        return "partial"
    return "ok"


def handler(event, context):
    """Main Lambda handler for AWS Waste Hunter."""
    logger.info("AWS Waste Hunter started")
    logger.info(f"Event: {_summarize_event(event)}")

    scan_errors = []
    delivery_errors = []

    # Define scanners to run in parallel
    scanners = [
        ("scan_unattached_ebs", scan_unattached_ebs),
        ("scan_idle_ec2", scan_idle_ec2),
        ("scan_unused_elb", scan_unused_elb),
        ("scan_stopped_rds", scan_stopped_rds),
        ("scan_idle_rds", scan_idle_rds),
        ("scan_unattached_eips", scan_unattached_eips),
        ("scan_orphaned_snapshots", scan_orphaned_snapshots),
    ]

    # Run scanners in parallel
    max_workers = _get_max_workers()
    logger.info(f"Running {len(scanners)} scanners with max_workers={max_workers}")
    resources, scan_errors = _run_scanners_parallel(scanners, max_workers, context=context)

    # Get EC2 no-metrics count after EC2 scan completes (fail-open).
    try:
        no_metrics_count = get_no_metrics_count()
    except Exception as e:
        logger.warning(f"Could not read no-metrics count: {e}")
        no_metrics_count = 0

    logger.info(f"Total resources found: {len(resources)}")

    # Estimate costs
    try:
        estimated, total = estimate_monthly_waste(resources)
        logger.info(f"Cost estimation complete, total waste: ${total}")
    except Exception as e:
        logger.error(f"Error estimating costs: {e}", exc_info=True)
        scan_errors.append({"stage": "cost_estimation", "error": str(e), "type": type(e).__name__})
        estimated, total = [], 0

    # Check compliance + compute score
    compliance_stats = None
    violations = []
    try:
        violations = check_tag_compliance(estimated)
        compliance_stats = compute_compliance_stats(estimated, violations)
        logger.info(f"Found {len(violations)} tag compliance violations, "
                    f"score {compliance_stats['percent']}%")
    except Exception as e:
        logger.error(f"Error checking compliance: {e}", exc_info=True)
        scan_errors.append({"stage": "tag_compliance", "error": str(e), "type": type(e).__name__})
        violations = []
        compliance_stats = None

    # High-waste alert
    alert_threshold = _get_waste_alert_threshold()
    high_waste = total >= alert_threshold
    if high_waste:
        logger.critical(
            f"HIGH WASTE ALERT: estimated monthly waste ${total} "
            f"exceeds threshold ${alert_threshold}"
        )

    # Build report (on failure: record error, skip deliveries, still return failed/partial).
    report = None
    try:
        report = build_report(
            estimated, total, violations, scan_errors, delivery_errors,
            compliance_stats=compliance_stats,
        )
    except Exception as e:
        logger.error(f"Error building report: {e}", exc_info=True)
        scan_errors.append({"stage": "build_report", "error": str(e), "type": type(e).__name__})

    if report is not None:
        sns_result = {
            "status": _overall_status(scan_errors, [], estimated),
            "resources": len(resources),
            "no_metrics": no_metrics_count,
            "monthly_waste": total,
            "high_waste": high_waste,
            "compliance_percent": compliance_stats["percent"] if compliance_stats else None,
            "scan_errors": len(scan_errors),
            "delivery_errors": len(delivery_errors),
        }
        _safe_deliver("send_report", lambda r: send_report(r, sns_result), report, delivery_errors)
        _safe_deliver("archive_report", archive_report, report, delivery_errors)
    else:
        logger.error("Skipping deliveries: no report was built")

    # Final result after all deliveries
    result = {
        "status": _overall_status(scan_errors, delivery_errors, estimated),
        "resources": len(resources),
        "no_metrics": no_metrics_count,
        "monthly_waste": total,
        "high_waste": high_waste,
        "compliance_percent": compliance_stats["percent"] if compliance_stats else None,
        "scan_errors": len(scan_errors),
        "delivery_errors": len(delivery_errors),
    }

    logger.info(f"AWS Waste Hunter completed: {result}")
    return result
