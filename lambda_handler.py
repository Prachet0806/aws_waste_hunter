"""Version 2 synchronous/manual pipeline and reusable scanner/finalizer stages."""
import concurrent.futures
import logging
import math
import os
import time
import uuid
from utils.config import Config
from utils.scan_context import ScanContext, activate, protected
from utils.schema import resource_identity
from utils.logging_config import setup_logging
from scanner.ebs_scanner import scan_unattached_ebs
from scanner.ec2_scanner import scan_idle_ec2, get_no_metrics_count
from scanner.elb_scanner import scan_unused_elb
from scanner.rds_scanner import scan_stopped_rds
from scanner.rds_idle_scanner import scan_idle_rds
from scanner.eip_scanner import scan_unattached_eips
from scanner.snapshot_scanner import scan_orphaned_snapshots
from cost_engine.estimator import estimate_monthly_waste
from compliance.tag_checker import check_tag_compliance, compute_compliance_stats
from reporting.report_builder import build_report, build_notification
from delivery.sns_sender import send_report
from delivery.s3_archiver import archive_report, archive_json

logger = setup_logging(__name__)
DEFAULT_WASTE_ALERT_THRESHOLD = 1000.0
SCAN_TIMEOUT_SECONDS = 240
DEFAULT_MAX_WORKERS = 4


def scanners():
    return [(name, globals()[name]) for name in (
        'scan_unattached_ebs', 'scan_idle_ec2', 'scan_unused_elb', 'scan_stopped_rds',
        'scan_idle_rds', 'scan_unattached_eips', 'scan_orphaned_snapshots')]


def _get_waste_alert_threshold():
    try:
        value = float(os.getenv('WASTE_ALERT_THRESHOLD', '1000'))
        return value if math.isfinite(value) and value >= 0 else DEFAULT_WASTE_ALERT_THRESHOLD
    except ValueError:
        return DEFAULT_WASTE_ALERT_THRESHOLD


def _get_max_workers():
    return Config.load().max_workers


def run_scanner(name, func, config, account_id, deadline):
    started = time.monotonic()
    scope = ScanContext(name, config, account_id, deadline)
    with activate(scope):
        try:
            scope.check()
            findings = func()
            if not isinstance(findings, list):
                raise TypeError('Scanner must return a findings list')
            findings = [r for r in findings if not protected(r.get('tags', {}))]
            fallback_inventory = not scope.inventory
            for finding in findings:
                finding.setdefault('region', config.region)
                finding.setdefault('account_id', account_id)
                known = scope.inventory.get(resource_identity(finding))
                if known:
                    finding['tags_status'] = known.get('tags_status', 'known')
                elif fallback_inventory:
                    scope.add(dict(finding))
            result = scope.result(findings, time.monotonic()-started)
            if scope.errors and not scope.inventory and not findings:
                result["status"] = "failed"
        except Exception as exc:
            scope.error(name, exc)
            result = scope.result([], time.monotonic()-started)
            result['status'] = 'partial' if scope.inventory else 'failed'
    return result


def collect_scanners(items, config, account_id, deadline):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=config.max_workers)
    futures = {executor.submit(run_scanner, name, func, config, account_id, deadline): name for name, func in items}
    results = []
    try:
        completed, pending = concurrent.futures.wait(futures, timeout=max(0, deadline-time.monotonic()))
        for future in completed:
            results.append(future.result())
        for future in pending:
            future.cancel()
            results.append({'scanner': futures[future], 'status': 'failed', 'discovery_complete': False,
                            'findings': [], 'inventory': [], 'skip_reasons': [],
                            'errors': [{'stage': futures[future], 'type': 'TimeoutError', 'error': 'stage deadline exhausted'}]})
    finally:
        # Running threads cannot be forcibly cancelled. Production uses isolated
        # Lambda tasks; local callers get a bounded wait and task-local evidence.
        executor.shutdown(wait=False, cancel_futures=True)
    return sorted(results, key=lambda r: r['scanner'])


def _run_scanners_parallel(items, max_workers, context=None, timeout=SCAN_TIMEOUT_SECONDS):
    from dataclasses import replace
    config = replace(Config.load(), max_workers=max_workers)
    budget = timeout
    if context and hasattr(context, 'get_remaining_time_in_millis'):
        budget = min(budget, max(0, context.get_remaining_time_in_millis()/1000-30))
    results = collect_scanners(items, config, 'unknown', time.monotonic()+budget)
    return [f for r in results for f in r['findings']], [e for r in results for e in r['errors']]


def _safe_scan(name, func, errors):
    result = run_scanner(name, func, Config.load(), 'unknown', float('inf'))
    errors.extend(result['errors'])
    return result['findings']


def _safe_deliver(name, func, report, errors):
    try:
        func(report)
        return True
    except Exception as exc:
        errors.append({'stage': name, 'type': type(exc).__name__, 'error': str(exc)[:1000]})
        return False


def _summarize_event(event, limit=1000):
    # An allowlist prevents nested credentials or arbitrary payloads reaching logs.
    if not isinstance(event, dict):
        return '<non-object event>'
    return str({k: event[k] for k in ('id', 'source', 'time', 'version') if k in event})[:limit]


def _overall_status(scan_errors, delivery_errors, resources_estimated, *, successful_scans=None,
                    report_built=True, delivered=None):
    if not report_built or successful_scans == 0 or delivered == 0:
        return 'failed'
    if scan_errors or delivery_errors:
        return 'partial'
    return 'ok'


def finalize(results, config, account_id, run_id, deadline=float('inf'), durable=True):
    scan_errors = [e for r in results for e in r.get('errors', [])]
    delivery_errors = []
    successes = sum(r['status'] != 'failed' for r in results)
    findings = [f for r in results for f in r.get('findings', [])]
    inventory = {}
    for result in results:
        for resource in result.get('inventory', []):
            key = resource.get("resource_key") or resource_identity(resource)
            if key not in inventory or resource.get('tags_status') == 'known':
                inventory[key] = resource
    scope = ScanContext('finalize', config, account_id, deadline)
    estimated, total, stats, violations, report = [], None, None, [], None
    with activate(scope):
        try:
            scope.deadline = min(deadline-30, time.monotonic()+config.pricing_seconds)
            if successes:
                estimated, total = estimate_monthly_waste(findings)
        except Exception as exc:
            scan_errors.append({'stage': 'cost_estimation', 'type': type(exc).__name__, 'error': str(exc)[:1000]})
        scope.deadline = deadline
        try:
            violations = check_tag_compliance(list(inventory.values()))
            stats = compute_compliance_stats(list(inventory.values()), violations)
        except Exception as exc:
            scan_errors.append({'stage': 'tag_compliance', 'type': type(exc).__name__, 'error': str(exc)[:1000]})
        unpriced = sum(r.get('monthly_cost') is None or not r['pricing']['complete'] for r in estimated)
        partial = any(r['status'] != 'complete' for r in results) or bool(unpriced)
        result = {'schema_version': 2, 'run_id': run_id, 'account_id': account_id, 'region': config.region,
                  'status': _overall_status(scan_errors, [], estimated, successful_scans=successes),
                  'resources': len(findings), 'priced_resources': sum(r.get('monthly_cost') is not None for r in estimated), 'inventory_resources': len(inventory),
                  'no_metrics': sum(any(word in s['reason'].lower() for word in ('metric', 'coverage', 'cpu')) for r in results for s in r.get('skip_reasons', [])),
                  'skipped_resources': sum(len(r.get('skip_reasons', [])) for r in results),
                  'pricing_fallbacks': sum(any('fallback' in n for n in r['pricing']['notes']) for r in estimated),
                  'monthly_waste': total, 'financial_basis': 'priced-list-price-subtotal',
                  'unpriced_resources': unpriced, 'pricing_complete': not unpriced and total is not None,
                  'high_waste': total >= config.threshold if total is not None and (total >= config.threshold or not partial) else None,
                  'compliance_percent': stats['percent'] if stats else None,
                  'scan_errors': len(scan_errors), 'delivery_errors': 0,
                  'scanner_outcomes': {r['scanner']: r['status'] for r in results},
                  'incomplete_scanners': sum(r['status'] != 'complete' for r in results)}
        if partial and result['status'] == 'ok':
            result['status'] = 'partial'
        try:
            scope.check()
            report = build_report(estimated, total, violations, scan_errors, compliance_stats=stats)
            report = (f"Run: {run_id}; scope: {account_id}/{config.region}\n"
                      f"Discovery/analysis status: {result['status']}; unpriced: {unpriced}\n"
                      f"Scanner outcomes: {result['scanner_outcomes']}\n\n" + report)
        except Exception as exc:
            scan_errors.append({'stage': 'build_report', 'type': type(exc).__name__, 'error': str(exc)[:1000]})
            result['status'] = 'failed'
        prefix = os.getenv('REPORT_PREFIX', 'reports').strip('/') or 'reports'
        base = f'{prefix}/{account_id}/{config.region}/{run_id}'
        archive_uri = None
        archived = notified = False
        if report is not None:
            try:
                if durable:
                    archive_json({'result': result, 'scanners': results, 'findings': estimated,
                                  'compliance': stats, 'violations': violations}, base+'/findings.json')
                    key = archive_report(report, key=base+'/report.md')
                else:
                    key = archive_report(report)
                archive_uri = f"s3://{os.getenv('REPORT_BUCKET', '')}/{key}"
                archived = True
            except Exception as exc:
                delivery_errors.append({'stage': 'archive_report', 'type': type(exc).__name__, 'error': str(exc)[:1000]})
            if not archived:
                result['status'] = 'partial' if successes else 'failed'
            result['delivery_errors'] = len(delivery_errors)
            notified = _safe_deliver('send_report', lambda text: send_report(text, result),
                                    build_notification(result, archive_uri), delivery_errors)
        result.update(archive_uri=archive_uri, delivery_errors=len(delivery_errors), scan_errors=len(scan_errors),
                      delivery={'archive': archived, 'notification': notified})
        if report is None or successes == 0 or not (archived or notified):
            result['status'] = 'failed'
        elif delivery_errors or scan_errors or partial:
            result['status'] = 'partial'
        if durable:
            try:
                archive_json({'result': result, 'scan_errors': scan_errors, 'delivery_errors': delivery_errors}, base+'/manifest.json')
            except Exception as exc:
                result.update(status='failed', manifest_error=str(exc)[:1000])
        from utils.logging_config import emit_metrics
        emit_metrics(result)
        logger.info('Run completed', extra={'result': result})
    return result


def handler(event, context):
    config = Config.load()
    logger.info('Run started: %s', _summarize_event(event))
    from utils.aws_factory import get_client
    account_id = os.getenv('ACCOUNT_ID') or get_client('sts').get_caller_identity()['Account']
    started = time.monotonic()
    remaining = context.get_remaining_time_in_millis()/1000 if context and hasattr(context, 'get_remaining_time_in_millis') else config.scan_seconds+config.pricing_seconds+60
    deadline = started+max(0, remaining-5)
    scan_deadline = min(started+config.scan_seconds, deadline-config.pricing_seconds-30)
    results = collect_scanners(scanners(), config, account_id, scan_deadline)
    return finalize(results, config, account_id, uuid.uuid4().hex, deadline,
                    durable=os.getenv('REPORT_BUCKET') is not None)
