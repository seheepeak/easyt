"""The easyt logger: stdlib logging, whose lines on stderr draw the tree of the spans they are
logged under (see spans.py)."""

import logging
import sys

from easyt.utils.spans import SUCCESS, SpanFormatter, stderr_is_journal


class _EasytLogger(logging.Logger):
    def success(self, msg, *args, **kwargs) -> None:
        if self.isEnabledFor(SUCCESS):
            self._log(SUCCESS, msg, args, **kwargs)


_prev_logger_class = logging.getLoggerClass()
logging.setLoggerClass(_EasytLogger)
log: _EasytLogger = logging.getLogger("easyt")  # type: ignore[assignment]
logging.setLoggerClass(_prev_logger_class)


def setup_logging(level: int = logging.INFO) -> None:
    """Write the log to stderr as a tree of spans, and set the verbosity of the ``easyt`` logger.

    The root logger stays at WARNING so third-party libraries (yt-dlp, httpx,
    anthropic, ...) do not flood the output at DEBUG.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(SpanFormatter(color=sys.stderr.isatty(), journal=stderr_is_journal()))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    log.setLevel(level)


setup_logging()


if __name__ == "__main__":
    log.debug("Looking for something")
    log.info("This is information")
    log.success("Something task finished!")
    log.warning("The pod bay doors are locked")
    log.error("Something terrible happened!")
    log.critical("Total meltdown")
