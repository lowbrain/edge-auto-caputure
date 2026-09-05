"""ダウンロードの退避（E-4）。保存先規約・衝突回避・実際の退避処理をまとめる。

以前は edge_auto_capture.py の CaptureSession が直接持っていた。撮影物の保存先規約を
lineage.py が持っているのと同じ粒度の関心事なのに本体へ同居していたため、独立モジュールへ
切り出した（#59）。系譜（lineage）の解決（_resolve_group）はページ集合の状態を持つ
CaptureSession 側の責務のまま残し、ここでは「group_id が決まったあとの保存」だけを担う。
CaptureSession.on_download は系譜解決だけ行い、本体の保存は save() へ委譲する薄いアダプタになる。
"""

from pathlib import Path

from config import Config
from infra import log
from lineage import group_folder_name, group_subdir


def _downloads_dir(config: Config, group_id: str = "") -> Path:
    """利用者のダウンロードを残す保存先を返す（E-4）。

    撮影成果物（png/txt/log.txt）と混ざらないよう downloads サブフォルダに分ける。
    保存物と同じく系譜（lineage）ごとにまとめるため、group_id 採番済みなら
    output_dir/lineage-<id>/downloads、未採番(空)なら output_dir/downloads を返す。
    """
    return group_subdir(config.output_dir, group_id) / "downloads"


def _unique_path(directory: Path, name: str) -> Path:
    """directory/name を返す。既に在れば name(1)/name(2)… と連番を付けて衝突を避ける。

    同名ファイルを続けて落としても上書きしないようにするため（拡張子は保つ）。
    """
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        cand = directory / f"{stem}({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


async def save(download, config: Config, group_id: str = "") -> None:
    """利用者がブラウザで落としたファイルを保存先へ退避する（E-4）。

    Playwright は既定でダウンロードをコンテキスト終了時に削除する。
    accept_downloads / downloads_path を指定しても削除される（一時置き場が変わる
    だけ）ので、ここで save_as して初めて手元に残る。元のファイル名のまま、撮影物と
    同じく系譜（lineage）ごとの output_dir/lineage-<id>/downloads へ保存し、同名衝突時は連番を付ける。
    group_id は呼び出し側（CaptureSession.on_download）が発生元ページから解決済みのものを渡す
    （未採番なら空文字のまま output_dir/downloads へ）。
    token 照合は不要（ブラウザ本体が発火するイベントで、ページ側から詐称できない）。
    """
    name = download.suggested_filename or "download"
    dl_dir = _downloads_dir(config, group_id)
    try:
        dl_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(dl_dir, name)
        await download.save_as(str(target))
        log(f"[DL] {group_folder_name(group_id)} 保存しました: {target.name}" if group_id
            else f"[DL] 保存しました: {target.name}")
    except Exception as e:
        # 例: 保存前にウィンドウを閉じられ一時ファイルが消えた等。無言にはしない。
        log(f"[DL] ダウンロードの保存に失敗しました: {name} ({e})")
