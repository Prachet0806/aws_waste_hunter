"""Complete, timestamped CloudWatch aggregation across all response pages."""
import math
from datetime import datetime
from utils.config import Config
from utils.scan_context import CURRENT, check_deadline, record_error


def collect(cloudwatch, queries, start, end, period):
    samples = {q["Id"]: {} for q in queries}
    bad, seen_tokens, token = set(), set(), None
    try:
        while True:
            check_deadline()
            params = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end,
                      "ScanBy": "TimestampAscending"}
            if token:
                params["NextToken"] = token
            response = cloudwatch.get_metric_data(**params)
            for result in response.get("MetricDataResults", []):
                key = result.get("Id")
                if key not in samples:
                    continue
                if result.get("Messages"):
                    bad.add(key)
                if result.get("StatusCode") not in ("Complete", "PartialData"):
                    bad.add(key)
                if result.get("StatusCode") == "PartialData" and not response.get("NextToken"):
                    bad.add(key)
                values, timestamps = result.get("Values", []), result.get("Timestamps", [])
                if len(values) != len(timestamps):
                    bad.add(key)
                    continue
                for stamp, value in zip(timestamps, values):
                    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                        bad.add(key)
                        continue
                    value = float(value)
                    if not math.isfinite(value) or value < 0 or not start <= stamp < end:
                        bad.add(key)
                        continue
                    if stamp in samples[key] and samples[key][stamp] != value:
                        bad.add(key)
                    samples[key][stamp] = value
            token = response.get("NextToken")
            if not token:
                break
            if token in seen_tokens:
                raise ValueError("Repeated CloudWatch pagination token")
            seen_tokens.add(token)
    except Exception as exc:
        record_error("get_metric_data", exc)
        bad.update(samples)
    context = CURRENT.get()
    config = context.config if context else Config.load()
    expected = math.ceil((end-start).total_seconds()/period)
    output = {}
    for key, points in samples.items():
        buckets = {int((t-start).total_seconds()//period) for t in points}
        coverage = min(1.0, len(buckets)/max(1, expected))
        span = ((max(points)-min(points)).total_seconds()+period)/86400 if points else 0
        valid = key not in bad and coverage >= config.min_coverage and span >= config.min_observation_days
        output[key] = {"values": list(points.values()), "coverage": coverage,
                       "valid": valid, "start": start.isoformat(), "end": end.isoformat()}
        if context and not valid:
            context.operation("get_metric_data", "incomplete",
                              f"{key}: coverage={coverage:.3f}, observation_days={span:.2f}")
    return output


def statistics(response, start, end):
    """Positive traffic is usable even with sparse samples; zero requires coverage."""
    points = response.get("Datapoints", []) if isinstance(response, dict) else []
    seen = {}
    for point in points:
        stamp, value = point.get("Timestamp"), point.get("Sum")
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or value is None:
            return {"complete": False, "total": None}
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return {"complete": False, "total": None}
        if start <= stamp < end:
            if stamp in seen and seen[stamp] != value:
                return {"complete": False, "total": None}
            seen[stamp] = value
    config = CURRENT.get().config if CURRENT.get() else Config.load()
    buckets = {int((t-start).total_seconds()//86400) for t in seen}
    expected = math.ceil((end-start).total_seconds()/86400)
    complete = len(buckets)/max(1, expected) >= config.min_coverage
    context = CURRENT.get()
    if context and not complete:
        context.operation("get_metric_statistics", "incomplete",
                          f"daily coverage={len(buckets)/max(1, expected):.3f}")
    return {"total": sum(seen.values()) if seen else None, "complete": complete}


def window_end(period):
    from datetime import timezone
    stamp = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(int(stamp//period)*period, timezone.utc)
