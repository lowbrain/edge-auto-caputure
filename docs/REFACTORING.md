# リファクタリング計画（ROADMAP 機能実装のための基盤整備）

対象コミット: `46105d5` 時点 / **作成: 2026-08-16**

> **このファイルの位置づけ**
> [`ROADMAP.md`](ROADMAP.md) の機能（F-A1 索引CSV 等）を**すべて実装する**方針が決まった。
> 現在のコードは構造が良好（役割分割・依存一方向・回帰テスト完備・豊富な設計コメント）だが、
> ROADMAP が繰り返し警告するとおり、いくつかの機能は「同じ引数を 4〜5 箇所へ通す」配線を要する。
> このファイルは、**機能を散らかさずに全実装へ持っていくための順序と、機能が引き込む
> リファクタの中身**を、別セッションでそのまま着手できる粒度でまとめる。
>
> - **本ファイル**（`REFACTORING.md`）… リファクタの順序・対象・検証手順（コード作業の設計図）。
> - [`ROADMAP.md`](ROADMAP.md) … 何を作る／直すか（機能・不具合の一覧と優先順）。
> - [`HANDOFF.md`](HANDOFF.md) … このコードを触る人向けの落とし穴・作業環境・検証手順。
>
> **進め方**: 各フェーズ／機能は **GitHub Issue として起票済みの課題への対応**として取り組む。
> ブランチ・コミット・PR には対応 Issue 番号を紐付ける（`Fixes #NN` 等）。下の表の「Issue」欄に
> 該当番号を記入して使う（本ファイル作成時は `gh` 不在で自動取得できず未記入。リポジトリは
> [`lowbrain/edge-auto-caputure`](https://github.com/lowbrain/edge-auto-caputure/issues)）。

---

## 0. 大原則（ここを外すと価値を壊す）

1. **振る舞い不変**。リファクタのコミットでは出力・ログ・保存物を 1 バイトも変えない。
   機能追加はリファクタと**別コミット**に分ける（レビューと切り分けのため）。
2. **コメントは資産。丸ごと運ぶ**。各所の日本語コメントは A-1/A-2/B-3/E-6 等の落とし穴回避の
   記録。関数を移動するときはコメントも一緒に移す。削らない・要約しない。
3. **機能実装の順序＝リファクタの順序**。「実装してからまとめて片付ける」はしない
   （同じ配線苦痛を N 回繰り返し、より絡まった状態を後から直すことになる）。
   基盤リファクタだけは全機能の前に先行させてよい（全機能を作ると確定しており投機ではない）。
4. **各フェーズ末で 4 点セットを通す**（下記「検証」）。緑でなければ次へ進まない。

---

## 1. 配信順序（フェーズ表）

上から順に進める。フェーズ 0・1 を機能実装より前に置くのが肝。
「Issue」欄には各フェーズ／機能に対応する起票済み GitHub Issue の番号を記入する。

| # | フェーズ | 中身 | 対応 ROADMAP | Issue | コスト |
|---|---|---|---|---|---|
| **0** | CI 整備（**最初に実施**） | Actions で `pytest`+`ruff`+`mypy`。smoke は Edge 必須なので Windows runner 限定 or 手動トリガで `--strict` 付き | ROADMAP §1 CI | [#1](https://github.com/lowbrain/edge-auto-caputure/issues/1) | 小 |
| **1** | capture 経路の基盤リファクタ | `CaptureRequest` 化（R1）＋ `_capture` 保存ステップ分割（R2）＋ `BADGE_SCRIPT` 遅延化（R5a） | （下地。機能ではない） | #— | 中 |
| **2** | 撮影メタ・UX クラスタ | F-A1 索引CSV + F-A4 時刻 → F-D3 カウンタ/失敗表示 → F-A2 `_part.png` → F-B1 プリロード → F-B2 バナー除去。B-4（settle 二重待ち）も相乗り | F-A1/F-A4/F-D3/F-A2/F-B1/F-B2, B-4 | #— | 中〜大 |
| **3** | URL 判定クラスタ | F-C2 allow_urls + B-5（前方一致/fnmatch）。URL 判定関数の切り出し（R3）＋ `_shoot`/`_shoot_if_changed` の重複解消（R3b） | F-C2/B-5 | #— | 小〜中 |
| **4** | 保存先構造 | F-C3 セッションフォルダ。必要なら `LineageRegistry` 抽出（R4） | F-C3 | #— | 小〜中 |
| **5** | UI 便利機能 | F-D4 フォルダを開く / F-D2 セレクタ履歴 | F-D4/F-D2 | #— | 小 |
| **6** | 大物（最後） | F-A3 無害化HTML（新規サニタイズ器）→ F-D1 セレクタピッカー。E-2（history 復元）・E-1（a11y）も badge を触るついでに相乗り | F-A3/F-D1, E-1/E-2 | #— | 大 |

> **フェーズ 0（CI）は最初に実施する**（確定）。CI を立ててから機能実装へ入り、大量改修の間ずっと
> 自動で緑を守る。CI は起票済みの GitHub Issue への対応として取り組む（表の Issue 欄に番号を記入）。

---

## 2. 基盤リファクタの中身（フェーズ 1・3・4 で使う）

機能から独立して定義できる「下地」。ここを先に入れると以降の機能が薄く載る。

### R1. `CaptureRequest` データクラス化 ★最優先

**問題**: 撮影要求が `(url, config, selector, group_id)` の**位置引数タプル**として
`spawn → _pending → _worker → _capture` を貫通している。
- `capture.py` … `CaptureRunner.spawn`（現 ~L161）／`_pending` の型（~L150）／`_worker`（~L184）／`_capture`（~L202）
- ROADMAP F-A1 は「撮影契機を spawn→_pending→_worker→_capture と 3 経路に通す必要があり
  『引数 1 個』では閉じない」と明記。F-D3 も同様。**この配線を毎機能で繰り返すのを止める。**

**対策**: 撮影 1 回分の要求を 1 オブジェクトに集約する。
```python
@dataclass
class CaptureRequest:
    page: Page
    url: str
    config: Config
    selector: str = ""
    group_id: str = ""
    # 将来フィールド（フェーズ 2 で追加）:
    #   trigger: str = ""   # "manual" / "url" / "spa" … F-A1 索引の「撮影契機」/ F-D3
```
- `spawn` の引数と `_pending` の値型を `CaptureRequest` に置換。
- `_worker` / `_capture` はタプル分解をやめてフィールド参照に。
- 呼び出し側（`edge_auto_capture.py` の `_shoot` ~L306、`_shoot_if_changed` ~L562、
  `runner.spawn(...)` 全箇所）を `CaptureRequest(...)` 生成に差し替え。
- **振る舞い不変**。フィールド追加はフェーズ 2 で行い、ここでは器だけ用意する。

**効果**: F-A1／F-D3／F-A2／F-A3 が「フィールドを 1 個足す」で全経路へ伝わる。

### R2. `_capture` を保存ステップ単位に分割

**問題**: `capture.py` の `_capture`（~L202-283、約 80 行）に png / txt / part の 3 保存が直列で並ぶ。
F-A2（`_part.png`）・F-A3（HTML）・F-A1（索引 CSV）はここへ保存物を足す。

**対策**: 各ステップを private メソッドへ切り出す。
- `_save_screenshot(page, save_dir, stem, url, config, done)` … 現 png ステップ（`captureStart`/`captureEnd`
  の合図と `finally` はそのまま内包。写り込み防止のコメントを必ず運ぶ）
- `_save_text(page, save_dir, stem, url, config, done)` … 現 txt ステップ
- `_save_part(page, save_dir, stem, url, selector, done)` … 現 part ステップ
- `_capture` は「ts 確定 → load 待ち → title 確定 → save_dir 用意 → 各 `_save_*` 呼び出し →
  `[saved]`/`[保存できず]` ログ」の司会だけにする。
- **`done: list[str]` の受け渡し規約（A-3）は維持**。全滅時に `[saved]` と嘘をつかない判定材料。
- **振る舞い不変**。保存順・ファイル名・ログ文言は変えない。

**効果**: F-A2 は `_save_part_png` を 1 本足すだけ。F-A3 も同様に 1 メソッド追加で済む。

### R3. URL 判定関数の切り出し（フェーズ 3 で実施）

**問題**: skip 判定が 2 箇所に完全一致でベタ書き（`url in self.config.skip_urls`）。
- `edge_auto_capture.py` `_shoot`（~L317）／`_shoot_if_changed`（~L557）
- 現状は完全一致のみ。クエリが付くと効かない（B-5）。F-C2 allow_urls もここに絡む。

**対策**: `should_capture(url, config) -> bool` を 1 関数に切り出し、両所から呼ぶ。
前方一致 or `fnmatch` 化（B-5）と allow_urls（指定時はそれ以外を全スキップ、F-C2）を
この 1 関数に閉じ込める。判定ロジックが 1 箇所になるので単体テストも足しやすい。

### R3b. `_shoot` と `_shoot_if_changed` の重複解消（R3 と同時）

両者とも「url 取得 → skip 判定 → spawn」の定型。`_shoot_if_changed` が `_shoot` を使わず
並行して書いている。R3 の判定関数を挟んで共通部を寄せる（記録状態ゲートは呼び出し側責務のまま）。

### R4. `LineageRegistry` 抽出（フェーズ 4 で"必要なら"）

**問題**: `CaptureSession`（`edge_auto_capture.py` L189-593）が God class 化。系譜（グループ）解決が
その一系統（`groups`/`page_root`/`_find_root`/`_resolve_group`/`_make_group`/`_group_pages`/
`_on_page_closed` の刈り取り）。**最も追いにくく、ROADMAP が「テスト空白」と認める**箇所。

**対策（任意）**: 系譜解決だけを `LineageRegistry` クラス（新規 `lineage.py`）へ移す。
`opener` 連鎖の解決とページ消滅時の刈り取りが独立し、**ブラウザ無しで単体テスト可能**になる。
系譜命名ヘルパ（`capture.py` の `group_stamp`/`group_folder_name`/`group_subdir`、~L41-66）も
`lineage.py` へ同居させると自然。

**判断**: F-C3（セッションフォルダ）で系譜まわりを触るなら同時にやる価値がある。
単独では機能実装に必須ではない（テスト性向上が主目的）。**やらなくても他フェーズは進む。**

### R5. 小整理（相乗りで安く片付く）

- **R5a**: `badge.py` の `BADGE_SCRIPT = build_badge_script()`（~L114）は import 時 I/O。
  実運用では使わず（スモーク専用）、凍結環境の失敗経路を 1 つ減らせる。関数化して参照元
  （スモークテスト）を直すだけ。フェーズ 1 で一緒に。
- **R5b**: `config.py` `_build_config`（~L169-201）の output_dir 解決＋`set_log_dir` 副作用を
  関数抽出。任意・低優先。触るフェーズが来たときに。

---

## 3. ブラウザ起動の切り出し（任意・独立）

`edge_auto_capture.py` の `BROWSER_BY_KEY`/`AUTO_BROWSER_ORDER`/`_browser_candidates`/
`_browser_launch_kwargs`（L85-171、約 90 行）は凝集した「ブラウザ起動」関心事。
新規 `browser.py` へ移すと `main()` が薄くなり、起動オプションの単体テストも足せる。
**どのフェーズにも必須ではない**独立整理。エントリファイルの肥大が気になったら実施。

---

## 4. 新モジュール追加時に同時更新する設定（漏れると配布/CI が割れる）

`lineage.py` / `browser.py` を足すフェーズでは以下を**同時に**更新する:

- [`pyproject.toml`](../pyproject.toml) `[tool.setuptools] py-modules = [...]`（~L35）に追加
- [`pyproject.toml`](../pyproject.toml) `[tool.mypy] files = [...]`（~L57）に追加
- ruff は `extend-exclude` 方式なので**追記不要**
- [`build.ps1`](../build.ps1) は import 追従なので**変更不要**（~L73 のコメントが明言）

---

## 5. 検証（各フェーズ末で必ず 4 点）

```bash
pip install -e ".[dev]"
pytest                              # 速い純粋関数・token 照合・DL の回帰
python tests/smoke_badge.py --strict   # 実 Edge。バー構築・SPA検知・写り込み防止・JS エラー無し
ruff check .
mypy edge_auto_capture.py badge.py capture.py config.py infra.py   # 新モジュールは追加
```

- **smoke は実 Edge/Chrome 必須**（Windows で実行）。`--strict` を付けないと
  ブラウザ不在環境で「何も検証せず緑」になる。
- リファクタのフェーズでは、**新規テストを足せる場所は足す**（R1 は `CaptureRequest`、
  R3 は `should_capture`、R4 は `LineageRegistry` を単体で。ROADMAP「テスト空白」の穴埋めを兼ねる）。

---

## 6. 実装対象から外してよいもの（「全部」でも触らない）

ROADMAP 自身がコード作業不要と判断済み。実装計画に含めない:

- **log ローテート** … ROADMAP §1「費用対効果が合わず見送り」
- **D-D2 AV 誤検知** … 「技術改修でなく運用事項」
- **F-D5 多言語化** … 「配布先が日本語話者に限られる限り不要」（USAGE.txt が Shift-JIS 等、
  バーだけ差し替えても半端）

---

## 7. 着手順の要約（別セッション向けクイックスタート）

1. **フェーズ 0（最初）**: CI を立てる。対応する起票済み Issue を確認し、その対応として
   Actions 定義（`pytest`+`ruff`+`mypy`、smoke は `--strict` 手動）を追加する。
2. **フェーズ 1**: R1（`CaptureRequest`）→ R2（`_capture` 分割）→ R5a（`BADGE_SCRIPT` 遅延化）。
   振る舞い不変。4 点セット緑を確認してコミット。
3. **フェーズ 2**: F-A1+F-A4 → F-D3 → F-A2 → F-B1 → F-B2。各機能は別コミット。B-4 を相乗り。
4. **フェーズ 3**: R3/R3b（判定関数切り出し・重複解消）→ F-C2+B-5。
5. **フェーズ 4**: F-C3。系譜を触るなら R4（`LineageRegistry`）も。
6. **フェーズ 5**: F-D4 / F-D2。
7. **フェーズ 6**: F-A3（大）→ F-D1（大）。E-1/E-2 を相乗り。

> 各機能の詳細な実装メモ（地雷・受入基準）は [`ROADMAP.md`](ROADMAP.md) の該当行を必ず参照する
> （例: F-A1 は CSV を `utf-8-sig` で書く／時刻は ISO 8601 オフセット付き。F-A3 は meta CSP の
> 実機確認結果が ROADMAP 末尾にある）。
