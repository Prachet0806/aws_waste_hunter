"""Structured application logs and low-cardinality Embedded Metric Format."""
import json
import logging
import os
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record):
        output = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if hasattr(record, "result"):
            output["result"] = record.result
        if record.exc_info:
            output["exception"] = self.formatException(record.exc_info)
        return json.dumps(output, default=str, allow_nan=False)


def setup_logging(name=None, force=False):
    root = logging.getLogger()
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    if force:
        root.handlers.clear()
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stdout))
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
    root.setLevel(level)
    for noisy in ("boto3", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger = logging.getLogger(name) if name else root
    logger.setLevel(level)
    return logger


def emit_metrics(result):
    values = {"RunsCompleted": 1, "RunsFailed": int(result['status'] == 'failed'),
              "RunsPartial": int(result['status'] == 'partial'),
              "ScanErrors": result.get('scan_errors', 0), "DeliveryErrors": result.get('delivery_errors', 0),
              "UnpricedFindings": result.get('unpriced_resources', 0),
              "PricingFallbacks": result.get('pricing_fallbacks', 0)}
    print(json.dumps({"_aws": {"Timestamp": int(time.time()*1000), "CloudWatchMetrics": [
        {"Namespace": "AWSWasteHunter", "Dimensions": [["Service", "Deployment"]],
         "Metrics": [{"Name": k, "Unit": "Count"} for k in values]}]},
        "Service": "waste-hunter", "Deployment": os.getenv("DEPLOYMENT_ID", "local"), "run_id": result.get('run_id'), **values}, allow_nan=False))


def emit_scanner_metrics(result):
    print(json.dumps({"_aws":{"Timestamp":int(time.time()*1000),"CloudWatchMetrics":[{
        "Namespace":"AWSWasteHunter","Dimensions":[["Service","Deployment","Scanner"]],
        "Metrics":[{"Name":"ScannerDuration","Unit":"Seconds"},{"Name":"ScannerFailures","Unit":"Count"},
                   {"Name":"SkippedResources","Unit":"Count"}]}]},
        "Service":"waste-hunter","Deployment":os.getenv("DEPLOYMENT_ID","local"),"Scanner":result['scanner'],
        "ScannerDuration":result.get('duration',0),"ScannerFailures":int(result['status']=='failed'),
        "SkippedResources":result.get('skipped_count',0)},allow_nan=False))
