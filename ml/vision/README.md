# 固定カメラの可視存在分類データ

**最新の80枚版Colab Notebook/学習ZIP:** [手順・検査結果](../../docs/vision_colab_80images_20261102.md)。

Vision V2へのカスタム分類の学習・配備確認は [Colab一式・実機事前確認](../../docs/vision_v2_deployment_smoke_20261102.md) を参照。
PC専用CNNとは別経路。Colabでの実学習・変換・実機配備はまだ未完了。

責務は画像上の可視存在だけ。`NO_VISIBLE_LARVA` は生体不在を意味しない。
人手ラベルをモデル出力に流用しない。画像から自然活動・移動・生死を認定しない。

## 使い方（リポジトリのルートから）

撮影は従来どおり `scripts/vision_collection.ps1 --open`。
新しいセッションにケース、装置、照明条件、撮影手順、連続収集グループを記録する。
[収集手順](../../docs/vision_collection_protocol.md)を参照。

```powershell
.\.venv\Scripts\python.exe -m pip install -r ml/vision/requirements.txt
New-Item -ItemType Directory -Force captures/vision_ml
.\.venv\Scripts\python.exe -B host/vision_capture/export_classification.py --data-dir captures --output captures/vision_ml/snapshot-v1.jsonl
.\.venv\Scripts\python.exe -B ml/vision/dataset.py validate --root captures --snapshot captures/vision_ml/snapshot-v1.jsonl --output captures/vision_ml/validation-v1.json
.\.venv\Scripts\python.exe -B ml/vision/dataset.py split --root captures --snapshot captures/vision_ml/snapshot-v1.jsonl --output captures/vision_ml/split-v1.json
```

Webの「分類監査用JSONL」も同じ全件スナップショット。実画像はroot内に必要。
SQLiteを読むCLIはread-only接続。元JPEG・メタデータ・注釈を変更しない。
出力は既存ファイルを上書きしない。初回READY前の収集不足を補う間は新しい版番号を使う。
**初回READYを凍結した後**のデータ追加は、必ず前回のREADYを`--previous-manifest`に渡す。
旧画像・注釈・条件の変更/削除は拒否し、旧割当を保持する。新しい独立グループはtrainだけに追加する。
新しい重複が既存splitを連結したらNO_GO。設定変更や再ラベルが必要なら自動解除せず、評価設計を再レビューする。
凍結したvalidation/testの成分に新たに属する画像は追加評価に混ぜず、理由付きで除外する。

```powershell
.\.venv\Scripts\python.exe -B ml/vision/dataset.py split --root captures --snapshot captures/vision_ml/snapshot-v2.jsonl --previous-manifest captures/vision_ml/split-v1.json --output captures/vision_ml/split-v2.json
```

CLIは別のデータセットを初回作成するかを自動判別できないため、凍結後に前回manifestを省略して分割し直す運用は禁止する。
出力先`captures/`はGit除外済み。実画像・モデル・重み・学習結果・秘密情報をGitに追加しない。

`validate`は構造、元JPEG240×240とSHA256、capture/session対応、UTC区間、人手履歴を検査する。
setup、合成/未検証由来、未確認、曖昧、使用不能、除外、必要条件欠落は学習候補に入らない。
収集に有効でも学習候補0枚なら学習可能という意味ではない。
古いラベルの既存JSONL v1出力は互換用に維持するが、新しい検査の代わりにはならない。

## スキーマ

正本: [image_label.schema.json](schemas/image_label.schema.json)。`schema=bll.vision_image_label.v1`。

| フィールド | 意味 |
|---|---|
| image_id / image / sha256 | 元JPEG識別子・rootからの相対パス・元JPEGのSHA256 |
| capture_session_id / case_id / larva_id | 撮影単位・ケース・分かる場合の個体ID。推測して埋めない |
| device_id / camera_pose | セッションで人が指定した装置名 / FIXED（固定カメラ）、または観測ループのアーム視点 FRONT・TOP・BACK（`host/vision_capture/observation_import.py` で取込み。観測ループのセッションは `ARM` で、視点は画像ごとのメタデータから入る）。ハードウェアIDの自動証明ではない。学習データは現在 FIXED のみ（他は `CAMERA_POSE_UNVERIFIED` で除外） |
| case_role / case_role_history | ケース単位の役割（UNASSIGNED / TRAINING / EVALUATION）と追記式の履歴。観察ノートの「ケースの表示名と役割」で設定。EVALUATION のケースの画像は `EVALUATION_CASE` で学習から除外。画像単位の評価専用（dataset_role）は学習用ケース内の例外 |
| illumination_profile / acquisition_protocol_id | 固定照明条件と手順の版。実条件はsessionのメモにも記録 |
| continuity_group | セッションを切り直しても同じ連続観測なら同じID |
| captured_at_utc / timestamp_quality | 露光時刻はnull / EXPOSURE_UNSYNCED。XIAOのUTC同期品質と別物 |
| pc_capture_bracket | request/receiveのPC UTC区間。露光時刻とは呼ばない |
| source_kind / purpose | REAL_CAMERA / UNVERIFIED / SYNTHETIC と dataset / setup |
| label / labeler / label_version | 最新人手ラベル・確認者・対応表の版。モデルの分類を含めない |
| annotation_history | SQLiteの注釈ID順の履歴。元ラベル・確認時刻・メモも保持 |
| capture / session | 原メタデータとセッション条件の写し。照合対象 |

確認者名は任意。空欄でも人がラベルを保存した注釈履歴を確認の根拠とする。
空欄を架空の確認者名で補完しない。履歴のない未確認画像を確認済みにする変更ではない。

| 既存ラベル | 正規化ラベル | 二値学習候補 |
|---|---|---|
| larva_visible | VISIBLE_LARVA | 条件を満たす実機データのみ |
| background | NO_VISIBLE_LARVA | 同上。幼虫の不在ではない |
| uncertain | AMBIGUOUS | 除外・保留評価用 |
| unusable（追加） | UNUSABLE | 除外・画質評価用 |
| unreviewed | UNREVIEWED | 除外 |
| exclude | EXCLUDED | 除外 |

`label_version=human-visible-presence-v1` はこの正規化契約の版。
過去の注釈当時にこの版が存在したと意味付けない。original_labelと元履歴は維持する。
古い条件欠落を今回の条件で補完しない。シミュレーションをREAL_CAMERAに変更しない。
REAL_CAMERAは既存取得経路の記録に基づく由来区分であり、暗号学的な装置証明ではない。

## 分割契約

[dataset_config.json](dataset_config.json)でseed、クラス順、近重複判定、割合を固定する。
初期クラス順は `0=NO_VISIBLE_LARVA, 1=VISIBLE_LARVA`。Notebook・データローダー・量子化モデル・SSCMA出力の実順と一致することは後続工程で必ず検証する。
この順序の宣言を実モデルの順序検証と取り違えない。

session/case/larva/continuity_group/SHA256が共通、またはdHash64のHamming距離4以下の画像を連結成分にまとめる。
未確認・除外画像も連結計算に含め、橋渡しによる漏洩を防ぐ。近重複は白飛び・暗所でもまとまる保守的検査。
同一JPEGの矛盾する二値ラベルはエラー。近重複のラベル相違はmanifestに残し、目視再確認する。
全体で5000枚以下の初期収集向け。近重複の総数と先頭1000組を記録する。距離判定は全組へ適用。
画像ごとのランダム分割はしない。

成分をseedで安定ソートし、グループ数比で概ね70/15/15に分ける（画像枚数比ではない）。
validation/test各最低1成分。独立した学習候補成分が3未満、またはいずれかのsplitに二値の片方がなければ `NO_GO`（終了コード2）。
同じケースだけの大量画像では独立評価が成立しない。割合を変えて好成績になる分割を探さない。
個体IDが不明ならケースを越えた個体同一性を検証できない。グループ数を生物学的な独立個体数と呼ばない。
閾値選定はvalidationだけ。凍結したtestを評価後、調整に使ったら新しい独立testが必要。
別日・別条件の最終確認はこの初期分割だけで達成したと扱わない。

`split_manifest`には設定、設定/全入力のSHA256、画像別SHA256、除外理由、成分ID、割当、クラス枚数を保存。
注釈や画像が変われば入力SHA256が変わる。古いmanifest自体を編集・継ぎ足しせず、上の凍結継承で別版を作る。
学習用ファイルのコピー/ZIP化・学習はこのPhaseでは実装しない。まずmanifestを人が監査する。
人手監査・サンプル数・独立性・画質確認なしにREADYをモデル精度や配備Goと読まない。

評価: `evaluate_tflite.py --role evaluation` は、評価専用の画像と評価用ケースの全画像（人手ラベル済み）を使う。観測ループの画像は `jpeg_240x240_TOP` のように視点ごとに集計する。`--role evaluation_only` は従来どおり画像単位の指定だけ。
学習済みの印: [model_training_sets.json](model_training_sets.json) に、モデルごとの学習画像一覧（`captures/vision_ml/trial-*/dataset_manifest.json`）を登録する。観察ノートはこれを読み、学習に使った画像とそれを含むケースを評価用にできないようにする。

## 試験

### 全30枚によるPC専用の短時間試験学習

`prepare_trial.py` / `train_trial.py` は通常の学習・評価分割と別の、全件train専用の動作確認。
実行手順・実測結果・制約は [試験学習記録](../../docs/vision_trial_training_20261102.md) を参照。
ユーザー確認済み条件を補足ファイルへ保存し、必要条件不足を元DBへ捏造しない。
本番候補のNo-Go条件はそのまま。PC用小型CNNの重みはVision V2用モデルでも公式パイプラインの再現でもない。
学習に使った画像の再判定を独立精度として報告しない。

### INT8 TFLiteのPCベンチ評価

`evaluate_tflite.py` は、ColabのINT8（Vela変換前）`.tflite`を、撮影DBの最新人手ラベル（`background`=0、`larva_visible`=1。その他は除外）でPC評価する。DBは読み取り専用で開く。
入力は実機と同じRGB順が既定（2026-11-03、実機記録scoreとPC推論の照合で確認）。学習画像は`--exclude-manifest`で除外する。

```powershell
python -m venv <scratch>\litert_env
<scratch>\litert_env\Scripts\python.exe -m pip install -r ml/vision/requirements-eval.txt
<scratch>\litert_env\Scripts\python.exe ml/vision/evaluate_tflite.py `
  --model v9=<results>/training/epoch_100_int8.tflite `
  --exclude-manifest captures/vision_ml/trial-20261102-80images-04/dataset_manifest.json `
  --out captures/vision_ml/pc-eval-<date>/pc_eval.json
```

入力サイズ（96/192）はモデルの入力形状から自動で決まる。Colabジョブは`train-export --input-size 96|192`で同じデータ・同じ設定のモデルを作り、`output/input96`・`output/input192`へ分けて保存する（レシピv10）。trialの凍結は240×240 JPEGのみ（480×480は理由付きで除外）、上限300枚（2026-11-04 v12でbox-02追加により200→300）。

出力（正解数・Wilson 95%区間・balanced accuracy・AUC・混同行列・出力固定化）は同一ケース/時期の連続フレームを含み得るため、独立評価や配備承認として扱わない。Colabジョブは同じモジュールで、変換後INT8モデルを学習画像にRGB順で通す`export_int8_check.json`を保存する（in-sample）。

### ホスト検証

`.\.venv\Scripts\python.exe -B -m unittest discover -s ml/vision -p 'test_*.py'`
合成ローカルfixtureだけを使う。実機・生体の精度を測る試験ではない。
全体は `scripts/run_tests.ps1`。研究解析・FW・Evidence・Policyは変更しない。
