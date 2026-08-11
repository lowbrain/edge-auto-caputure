"""pytest 共通設定。

プロジェクト直下（このファイルの親の親）を import パスへ入れ、
tests/ からトップレベルモジュール（infra / config / capture / badge / edge_auto_capture）を
そのまま import できるようにする。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
