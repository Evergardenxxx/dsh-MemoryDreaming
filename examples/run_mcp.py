"""从任意工作目录启动源码仓库中的服务，无需预先安装 Python 包。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memorydreaming.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
