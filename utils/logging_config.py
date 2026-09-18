# utils/logging_config.py
import logging
import os
import sys


def setup_logging(name=None, force=False):
    """Configure logging for Lambda without clobbering the runtime.

    - Respects LOG_LEVEL env (DEBUG/INFO/WARNING/ERROR), defaults INFO.
    - Only adds a handler if none exists (unless force=True), so Lambda's
      pre-configured handler keeps working.
    - Quiets noisy boto3/botocore to WARNING.
    - Returns a module-style logger (not root) when name given.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if force or not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("boto3", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if name is None:
        root.setLevel(level)
        return root
    target = name if name else __name__
    logger = logging.getLogger(target)
    logger.setLevel(level)
    return logger
