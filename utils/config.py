"""Validated invocation configuration."""

import json
import math
import os
from dataclasses import dataclass


def number(name, default, minimum=0, maximum=None, integer=False):
    value = float(os.getenv(name, str(default)))
    if (
        not math.isfinite(value)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{name} is outside its supported range")
    if integer and not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(value) if integer else value


@dataclass(frozen=True)
class Config:
    region: str
    scan_seconds: float
    pricing_seconds: float
    max_workers: int
    min_coverage: float
    min_observation_days: float
    threshold: float
    max_resources: int
    max_finalizer_total_bytes: int
    excluded_tags: dict
    cpu_threshold: float = 2
    network_threshold: float = 1000000
    rds_cpu_threshold: float = 5
    rds_connections_threshold: int = 1
    min_snapshot_age_days: int = 7
    pricing_mode: str = "static"
    pricing_json: str = ""

    @classmethod
    def load(cls):
        region = (
            os.getenv("SCAN_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        if not region.strip():
            raise ValueError("Scan region is empty")
        if os.getenv("PRICING_MODE", "static") not in ("static", "live"):
            raise ValueError("PRICING_MODE must be static or live")
        for name, default, maximum in (
            ("CPU_THRESHOLD", 2, 100),
            ("RDS_CPU_THRESHOLD", 5, 100),
            ("NETWORK_THRESHOLD", 1000000, None),
            ("RDS_CONNECTIONS_THRESHOLD", 1, None),
            ("MIN_SNAPSHOT_AGE_DAYS", 7, None),
        ):
            number(name, default, maximum=maximum)
        excluded = json.loads(
            os.getenv(
                "EXCLUDED_TAGS_JSON",
                '{"waste-hunter:exclude":"true","waste-hunter-backup":"true"}',
            )
        )
        if not isinstance(excluded, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in excluded.items()
        ):
            raise ValueError("EXCLUDED_TAGS_JSON must map tag names to values")
        if os.getenv("S3_SSE", "AES256") not in ("AES256", "aws:kms"):
            raise ValueError("S3_SSE must be AES256 or aws:kms")
        if os.getenv("S3_SSE") == "aws:kms" and not os.getenv("S3_KMS_KEY_ID"):
            raise ValueError("S3_KMS_KEY_ID required for aws:kms")
        from cost_engine.estimator import _load_pricing

        prices = json.dumps(_load_pricing(), sort_keys=True)
        return cls(
            region.strip(),
            number("SCAN_TIMEOUT_SECONDS", 240, 1, 840),
            number("PRICING_TIMEOUT_SECONDS", 60, 1, 300),
            number("MAX_SCANNER_WORKERS", 4, 1, 10, True),
            number("MIN_METRIC_COVERAGE", 0.9, 0.5, 1),
            number("MIN_OBSERVATION_DAYS", 7, 1, 7),
            number("WASTE_ALERT_THRESHOLD", 1000),
            number("MAX_INVENTORY_RESOURCES", 100000, 1, integer=True),
            number(
                "MAX_FINALIZER_INPUT_BYTES",
                2 * 1024 * 1024,
                1024,
                2 * 1024 * 1024,
                True,
            ),
            excluded,
            number("CPU_THRESHOLD", 2, maximum=100),
            number("NETWORK_THRESHOLD", 1000000),
            number("RDS_CPU_THRESHOLD", 5, maximum=100),
            number("RDS_CONNECTIONS_THRESHOLD", 1, integer=True),
            number("MIN_SNAPSHOT_AGE_DAYS", 7, integer=True),
            os.getenv("PRICING_MODE", "static"),
            prices,
        )
