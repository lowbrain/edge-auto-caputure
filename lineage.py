"""タブ系譜（lineage / グループ）の識別・保存先規約と、実行時の解決レジストリ。

- 系譜 id の採番と、表示名・保存先サブフォルダの規約（group_stamp / group_folder_name / group_subdir）
- 系譜 1 つぶんの実行時状態（GroupState）と、その生成（make_group）
- ページ → 所属グループの解決とメモ化を担うレジストリ（LineageRegistry）

以前は capture.py のモジュール関数（group_stamp / group_folder_name / group_subdir）と、
edge_auto_capture.py の CaptureSession が持つ groups / page_root および
_resolve_group / _make_group / _find_root に分かれていた。系譜まわりの規約と解決ロジックを
1 か所へ寄せ、状態の所在を明確にし単体テストしやすくするために新設した（#36）。
「既存クラスの移動」ではなく「新設して寄せた」もの。

後方互換のため、capture.py は group_folder_name / group_subdir を、edge_auto_capture.py は
GroupState をここから再輸出しており、既存の import 経路（capture.group_subdir 等）は保たれる。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page

from infra import log


def group_stamp() -> str:
    """系譜（lineage）の id を「YYYYMMDDHHMMSSmmm」（ミリ秒まで・区切りなし）で返す。

    系譜を新たに作った時刻をそのまま id にする。区切り記号を入れないので `lineage-<id>` の
    <id> 部分は連続した数字になる（例: 20260814102028731）。
    """
    now = datetime.now()
    return f"{now:%Y%m%d%H%M%S}{now.strftime('%f')[:3]}"


def group_folder_name(group_id: str) -> str:
    """系譜（lineage）の表示名を返す（フォルダ名とログ表記で共用）。

    id は系譜を新たに作った時刻（ミリ秒まで・区切りなし）。`lineage-<id>` の形にして、
    フォルダ名とログのトークンを一致させ、ログから保存フォルダをそのまま辿れるようにする。
    """
    return f"lineage-{group_id}"


def group_subdir(output_dir: Path, group_id: str) -> Path:
    """系譜（lineage）ごとの保存先サブフォルダを返す。

    保存物を系譜ごとにまとめるための共通規約（`output_dir/lineage-<id>`）。edge_auto_capture の
    ダウンロード退避先もこれに揃える。group_id が空（未採番）なら output_dir 直下を返す。
    """
    return output_dir / group_folder_name(group_id) if group_id else output_dir


@dataclass
class GroupState:
    """タブ系譜（グループ）1 つぶんの実行時状態。

    グループ = root ページ（起動時の最初のタブ、または手動で開いた別タブ）と、そこから
    window.open / target="_blank" で派生したポップアップ/ウィンドウの一族。各グループが
    記録ON/OFF・SPA検知・対象セレクタを独立して持ち、系譜内のページはこの状態を共有する。
    """

    on: bool          # 記録中か（このグループの自動保存マスタースイッチ）
    spa_on: bool      # SPA検知（中身変化を契機に保存）
    selector: str     # 検知/抜き出しの対象 CSS セレクタ
    id: str = ""      # 系譜を作った時刻（ミリ秒まで・区切りなし）。フォルダ名/ログの識別子。空＝未採番


def make_group(on: bool, spa_on: bool, selector: str) -> GroupState:
    """GroupState を作る。id は「作った時刻（ミリ秒まで・区切りなし）」で、フォルダ名/ログに使う。"""
    return GroupState(on=on, spa_on=spa_on, selector=selector, id=group_stamp())


class LineageRegistry:
    """ページ → 所属グループ（タブ系譜）の解決とメモ化を担うレジストリ。

    グループ単位の実行時状態（groups）と、各ページ→所属 root のメモ（page_root）を own する。
    opener 連鎖から root を決めてメモ化し、まだグループの無い root（＝手動で開かれた新規タブ）は
    初期OFF（無関係タブを勝手に撮らない）の独立グループとして採番する。

    以前は CaptureSession が groups / page_root と _resolve_group / _find_root を直接持っていた。
    系譜の解決を CaptureSession から切り離してここへ寄せ、ブラウザ無しで単体テストできるようにする。
    起動時の最初のグループは呼び出し側（setup() が start_recording に従って）先に用意する。
    """

    def __init__(self, default_selector: str) -> None:
        # 新規タブ（手動で開かれたタブ）へ与える既定セレクタ。config.target_selector 由来。
        self.default_selector = default_selector
        self.groups: dict[Page, GroupState] = {}   # root ページ -> そのグループの状態
        self.page_root: dict[Page, Page] = {}      # 各ページ -> 所属グループの root（メモ化）

    async def find_root(self, page) -> Page:
        """page の所属グループの root ページを opener 連鎖から求める。

        既知の root（page_root に載っているページ）に達したらそれを、opener が None に達したら
        その末端ページ自身を root とする。opener 取得に失敗したら、その時点のページを root 扱い
        にする（系譜を辿れないページは、それ自身を独立グループの起点にする）。
        """
        p = page
        while True:
            known = self.page_root.get(p)
            if known is not None:
                return known
            try:
                parent = await p.opener()
            except Exception:
                return p
            if parent is None:
                return p
            p = parent

    async def resolve(self, page) -> GroupState:
        """page が属するグループの状態を返す（無ければ新規グループを OFF で作る）。

        opener を辿って root を決めてメモ化する。root のグループがまだ無い＝手動で開かれた
        新規タブなので、初期OFF（無関係タブを勝手に撮らない）の独立グループを作る。起動時の
        最初のグループは setup() が start_recording に従って先に用意している。
        """
        root = self.page_root.get(page)
        if root is None:
            root = await self.find_root(page)
            self.page_root[page] = root
        grp = self.groups.get(root)
        if grp is None:
            grp = make_group(on=False, spa_on=False, selector=self.default_selector)
            self.groups[root] = grp
            log(f"[{group_folder_name(grp.id)}] 新しいタブを認識しました（初期は待機）")
        return grp
