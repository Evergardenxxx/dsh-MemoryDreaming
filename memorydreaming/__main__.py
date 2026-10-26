"""让 `python -m memorydreaming ...` 可以直接运行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
