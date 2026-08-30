"""`python -m celmis`, for an environment where the script is not on PATH."""

from celmis.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
