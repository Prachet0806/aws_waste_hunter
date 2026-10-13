"""Per-invocation evidence and deadlines; no process-wide scan counters."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time

CURRENT = ContextVar("scan_context", default=None)


@dataclass
class ScanContext:
    scanner: str
    config: object
    account_id: str = "unknown"
    deadline: float = float("inf")
    clients: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    skips: list = field(default_factory=list)
    operations: dict = field(default_factory=dict)
    scan_window: dict = field(default_factory=dict)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Invocation stage deadline exhausted")

    def error(self, stage, exc):
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        retryable = code in ("Throttling", "ThrottlingException", "RequestLimitExceeded", "SlowDown", "InternalError", "ServiceUnavailable") or type(exc).__name__ in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError")
        self.errors.append({"stage": stage, "type": type(exc).__name__, "error": str(exc)[:1000],
                            "code": code, "retryable": retryable})

    def skip(self, resource_id, reason):
        self.skips.append({"resource_id": resource_id, "reason": reason,
                           "intentional": reason in ("protected resource", "protected or incomplete snapshot", "shared snapshot", "ASG-managed") or reason.startswith("unsupported lb_type")})

    def add(self, resource):
        from utils.schema import resource_identity
        resource.setdefault("account_id", self.account_id)
        resource.setdefault("region", self.config.region)
        key = resource.get("resource_key") or resource_identity(resource)
        if key not in self.inventory and len(self.inventory) >= self.config.max_resources:
            raise RuntimeError("Inventory limit exceeded; coverage is incomplete")
        self.inventory[key] = resource

    def result(self, findings, elapsed):
        complete = not self.errors and all(self.operations.values())
        return {"schema_version": 2, "scanner": self.scanner,
                "account_id": self.account_id, "region": self.config.region,
                "status": "complete" if complete and not any(not s["intentional"] for s in self.skips) else "partial",
                "discovery_complete": complete, "findings": findings,
                "inventory": list(self.inventory.values()), "scan_window": self.scan_window,
                "discovered_count": len(self.inventory), "evaluated_count": max(0, len(self.inventory)-len(self.skips)),
                "skipped_count": len(self.skips), "skip_reasons": self.skips,
                "errors": self.errors, "operations": self.operations, "duration": elapsed}


@contextmanager
def activate(context):
    token = CURRENT.set(context)
    try:
        yield context
    finally:
        CURRENT.reset(token)


def check_deadline():
    context = CURRENT.get()
    if context:
        context.check()


def record_error(stage, exc):
    context = CURRENT.get()
    if context:
        context.error(stage, exc)


def protected(tags):
    from utils.config import Config
    context = CURRENT.get()
    rules = context.config.excluded_tags if context else Config.load().excluded_tags
    return any(str(tags.get(k, "")).lower() == v.lower() for k, v in rules.items()) or any(
        k.startswith("aws:backup:") or k.startswith("aws:dlm:") for k in tags)
