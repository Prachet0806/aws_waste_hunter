# lambda_handler.py
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
    try:
        return func()
    except Exception as exc:
        errors.append({"stage": name, "error": str(exc)})
        return []

def _safe_deliver(name, func, report, errors):
    try:
        func(report)
    except Exception as exc:
        errors.append({"stage": name, "error": str(exc)})

def handler(event, context):
    resources = []
    scan_errors = []
    delivery_errors = []

    resources += _safe_scan("scan_unattached_ebs", scan_unattached_ebs, scan_errors)
    resources += _safe_scan("scan_idle_ec2", scan_idle_ec2, scan_errors)
    resources += _safe_scan("scan_unused_elb", scan_unused_elb, scan_errors)
    resources += _safe_scan("scan_stopped_rds", scan_stopped_rds, scan_errors)

    estimated, total = estimate_monthly_waste(resources)
    violations = check_tag_compliance(estimated)

    report = build_report(estimated, total, violations, scan_errors, delivery_errors)

    _safe_deliver("send_report", send_report, report, delivery_errors)
    _safe_deliver("archive_report", archive_report, report, delivery_errors)

    return {
        "status": "ok" if not scan_errors and not delivery_errors else "partial",
        "resources": len(resources),
        "monthly_waste": total,
        "scan_errors": len(scan_errors),
        "delivery_errors": len(delivery_errors),
    }
