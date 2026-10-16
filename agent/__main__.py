"""Allow ``python -m agent`` to run the capture agent."""

from .agent import main

if __name__ == "__main__":
    raise SystemExit(main())
