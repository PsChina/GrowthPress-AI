"""`python -m growthpress` 入口 → cli.main."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
