"""`python -m trader.options` runs the options worker (the same as `python -m trader.options.worker`)."""

from trader.options.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
