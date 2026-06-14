"""Straight-through entry point for the BTC Futures DCA bot."""

import logging
import sys

from config import LOG_FILE
from dca_engine import DCABot


class SafeConsoleHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            msg = self.format(record)
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            safe = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.stream.write(safe + self.terminator)
            self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        SafeConsoleHandler(sys.stdout),
    ],
)

logger = logging.getLogger("main")


def main() -> None:
    logger.info("=" * 60)
    logger.info("BTC Futures DCA bot starting")
    logger.info("=" * 60)
    DCABot().run()


if __name__ == "__main__":
    main()
