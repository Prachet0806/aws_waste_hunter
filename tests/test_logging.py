import logging

from utils.logging_config import setup_logging


class TestLoggingSetup:
    """Test logging configuration."""

    def test_setup_logging_creates_logger(self):
        """Test that setup_logging creates a configured logger."""
        logger = setup_logging()

        assert logger is not None
        assert isinstance(logger, logging.Logger)
        assert logger.level == logging.INFO

    def test_logger_has_handler(self):
        """Test that logger has a stream handler."""
        logger = setup_logging()

        assert len(logger.handlers) > 0
        has_stream_handler = any(
            isinstance(h, logging.StreamHandler) for h in logger.handlers
        )
        assert has_stream_handler

    def test_logger_format(self):
        import json

        from utils.logging_config import JsonFormatter

        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        result = json.loads(JsonFormatter().format(record))
        assert result["level"] == "INFO" and result["message"] == "hello"

    def test_multiple_calls_idempotent(self):
        """Test that calling setup_logging multiple times doesn't create duplicate handlers."""
        logger1 = setup_logging()
        handler_count_1 = len(logger1.handlers)

        logger2 = setup_logging()
        handler_count_2 = len(logger2.handlers)

        # Should remove old handlers before adding new one
        assert handler_count_2 == handler_count_1
