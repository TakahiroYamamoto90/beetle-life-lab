# Passive detection Track B

人工掘削PICKセッションとQUIETセッションの探索比較。CH1=ADXL1002電圧、CH2=ピエゾ電圧。幼虫活動の検出器ではなく、FW・Evidence・Policy・SQLiteを変更しない。

Python 3.12以降を想定。実行版は成果物manifest.json参照。既存解析で用いたNumPy標準偏差、SciPy SOS/Welch、Matplotlibの処理方法を踏襲したが、旧コードの全ファイル一括読み込みは使わず、結合CSVのsource_file境界を保ってストリーム処理する。

```powershell
python -m pip install -r analysis/passive_detection/requirements.txt
python -m unittest discover -s analysis/passive_detection -p 'test_*.py' -v
python analysis/passive_detection/pipeline.py --input-dir docs/experiment --output docs/analysis/passive_detection_20261031
python analysis/passive_detection/finalize.py --results docs/analysis/passive_detection_20261031
```

依存が既に揃っていればインストール不要。`--config <config.json>`で設定を切り替え可能。既存成果物を保持する場合は別の`--output`を指定する。中断時の途中出力を完成結果として使わず、CLIが成功終了した結果を使用する。

入力は`*dry_290_AXCL_6cm.csv`〜293の結合CSVを再帰検索。各1本を要求。12番号試行とno_signalを検証し、原試行CSVが隣に存在する場合は数値対応も確認する。原本と派生データの両方を重複試行として数えない。各CSVをchunksize=50,000で読み、波形は1試行ずつ処理。図用の代表4試行以外の生波形を長期保持せず、出力に原波形コピーは作らない。欠損・時刻不整合は例外で停止する。

`config.json`に比較範囲を固定。300–1500 Hzを主要検出帯域とし、他の3帯域は診断特徴量のみ。Detector Cはspectral fluxではなく短窓RMSの正の増分。Dの一致はイベントのピーク窓中心時刻で1対1対応し、±10/20 ms。SOFTは単独候補を残して一致を重み2とする診断用の証拠重みで、確率ではない。ANDをイベント発生の必要条件にはしない。

`zero_phase`は試行平均除去＋sosfiltfilt、`causal`は先頭値除去＋sosfilt。両方で初末100 msを評価対象外とする。causalはフィルタと検出計算の未来参照を避けるが、実システムの遅延・8 kHz化を検証したものではない。零位相と因果では振幅応答・端点過渡も異なる。

Bは学習静音の中央値/MADを初期背景とし、過去30窓へ更新。候補開始閾値の80%を超える窓も背景へ取り込まない。MADの下限は中央値の5%。初期背景が10窓未満ならNOT_CALIBRATED。1秒のPICK冒頭を静音と仮定して初期化しない。別の計測品質指標なしに環境雑音と機械振動を識別できないため、急な雑音増加は候補になり得る。この制約をレポートで評価する。

クリッピングの既知レールは与えられていない。最大最小の反復数・電圧を監査し、クリッピングを確認済みとはしない。センサー感度を未校正のままgに変換しない。Wavegenヘッダーにかかわらず本実験はユーザー指定の受動条件として扱う。

## 新規未見ペアCへの固定適用

`finalize.py`は両方向の結果を見て選んだ探索候補を`frozen_unseen_C.json`へ書く。学習パラメータはペアBで得たものを保持し、A/Bを再度未知評価に使わない。前処理設定とパラメータを含むため、新しいペアCでは再fitしない。

```powershell
python analysis/passive_detection/apply_frozen.py --quiet-csv '<新規静音結合CSV>' --pick-csv '<新規人工掘削結合CSV>' --frozen docs/analysis/passive_detection_20261031/frozen_unseen_C.json --output captures/track_b_unseen_C
```

新CSVもsource_file列を持つ結合形式。100 kHz AD3の2列電圧を要求し、異なる機器やfsへ黙って変換しない。no_signalは再びauditのみ。新規幼虫データに適用しても人工掘削用パラメータが生体検出を保証することはない。候補Cを見る前にファイルのSHA256とconfigを凍結する。

固定適用後の監査・図・集計は `python analysis/passive_detection/review_frozen.py --results <固定適用の出力先>`。同名の元試行CSVを結合CSVの隣から読み、対応を照合する。検出パラメータは変更せず、各条件の先頭例とPICKの最初のB候補陽性／陰性例を表示する。これらは診断用の代表選択であり、新しい正解ラベルにはしない。

## 出力

### 296を用いた連続検出改善（新しい開発設定）

`improved.py`と`improved_config.json`は旧A/Bとは別のA2/B2。相対背景、候補中・前兆中の背景更新停止、ピーク低下解除、再準備、持続候補の保護停止を実装。初めの5秒を刺激なし背景と仮定するため、1秒独立試行にはそのまま適用しない。296で開発した設定であり、未知精度を保証しない。

```powershell
python analysis/passive_detection/improved.py --features docs/analysis/passive_detection_trial296_20261031/features_20ms.csv --source-manifest docs/analysis/passive_detection_trial296_20261031/manifest.json --annotations docs/analysis/passive_detection_trial296_20261031/operator_annotations.json --output docs/analysis/passive_detection_trial296_improved_20261031
```

次の新規連続raw CSVには`--input <CSV> --preprocessing-frozen <旧凍結JSON> --config <新開発設定JSON> --output <別保存先>`で適用する。コードは新入力から閾値を再選択しない。任意の観察注釈は`--annotations`で重ねるが検出器へ渡さず、注釈がないときはUNANNOTATED。終了時刻不明の注釈から厳密なイベント精度を作らない。

旧A/Bの連続取得296の初回評価は `continuous.py --input <raw CSV> --frozen <固定JSON> --output <保存先>`。source_file列のないWaveFormsの2CH raw CSVをストリーム処理し、チャンクを跨いでフィルタと窓を保持する。当時の予定表（0〜30静音、30〜35掘削、35〜60静音、60〜65掘削、65〜90静音）が再現用にコード内に残るが、実操作ラベルとしては撤回済み。実操作はユーザー確認の約18/36/54/72秒の4回であり、`operator_annotations.json`を参照する。別の計測へ旧予定表を無断転用しない。旧ラベル付き集計は初回仮説の履歴にとどめる。90秒以降は未注釈とし、実測終端までの全期間について固定A/Bの挙動も記録する。候補が長く持続する失敗を隠さないため、候補開始数と占有時間を併記する。

- `manifest.json`, `config.json`, `thresholds.json`：監査・版・学習パラメータ。
- `features_per_trial.csv`：104行（52試行×2CH）。no_signalはprimary=false。
- `candidate_events.csv`：由来runと時刻を持つ候補。人工刺激の正解時刻ではない。
- `evaluation_per_trial.csv`, `evaluation.csv`：試行別とセッション別の集計。誤報率の分母は端点除外後の秒数。
- `01_`〜`07_*.png`：比較図・過渡監査。
- `reference_filter_audit.csv`：参考帯域値のフィルタ定義依存性。
- `frozen_unseen_C.json`：未見ペアで試す候補。実装採用の承認ではない。

元研究データは`.gitignore`の`docs/experiment/`で除外。解析成果物には要約・窓統計・図のみを含める。単体テストはすべて合成fixtureであり、実測の成績表へ混ぜない。
