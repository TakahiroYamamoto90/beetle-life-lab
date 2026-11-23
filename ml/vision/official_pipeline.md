# Phase 2: Seeed公式分類パイプライン再現の実行票

状態: **未実行**。このファイルは参照と実行ゲート。学習Notebookや変換の成功記録ではない。

2026-11-02追記: [Vision V2用Colabパッケージと実機事前確認](../../docs/vision_v2_deployment_smoke_20261102.md) を作成。
実機の元モデル情報・撮影・XIAOログ退避まで確認。Colab学習・Vela変換・配備は未実施のまま。

2026-11-01に公式Web本文を確認:

- [Seeed Quick Start / Image Classification](https://wiki.seeedstudio.com/ModelAssistant_Introduce_Quick_Start/)
- [公式分類モデル説明（MobileNetV2 0.35 Rep 96）](https://github.com/Seeed-Studio/sscma-model-zoo/blob/main/docs/en/Person_Classification_MobileNetV2_0.35_Rep_96.md)
- [公式NotebookをColabで開く](https://colab.research.google.com/github/seeed-studio/sscma-model-zoo/blob/main/notebooks/en/Person_Classification_MobileNetV2_0.35_Rep_96.ipynb)
- [Notebookの原本](https://github.com/Seeed-Studio/sscma-model-zoo/blob/main/notebooks/en/Person_Classification_MobileNetV2_0.35_Rep_96.ipynb)
- [Vision V2への配備手順](https://wiki.seeedstudio.com/grove_vision_ai_v2_sscma/)

Object Detection/YOLOのチュートリアルを二値分類として流用しない。
公式の人分類は再現確認専用であり、幼虫モデルではない。
公式NotebookはModelAssistant 2.0.0、96×96、GPUと特定のColab版を前提にする。
実行環境の可用性と現在の依存互換性は未確認。ブラウズで参照先は確認できたが、git接続が環境のネットワーク制限で失敗したため、最新commitと原本bytesのSHA256は未取得。
動くmainのURLを固定済みソースと見なさない。

## 実行順序と保存物

1. ColabのGPU割当と互換ランタイムを確認する。GPUがなければ記録して停止し、長時間CPU学習に置き換えない。
2. 公式Notebook原本をダウンロードしてSHA256とリポジトリcommitを記録。以後commit固定URLを使う。原本を残し、差分は別ファイルにする。
3. 公式セットアップを順に実行。実際のModelAssistant commit、Python、GPU、CUDA、pip freeze、乱数seed、開始/終了時刻、全セルの出力を保存する。上流の未固定pip依存を未確認の版で全面変更しない。
4. 公式サンプルのデータ取得・学習・変換を完走する。データ配布条件と必要な認証を確認し、APIキーはColab Secretsへ置く。キーやデータをリポジトリへ保存しない。
5. float/INT8/Vela成果物、設定、前処理、モデル入力dtype/shape/量子化パラメータ、出力の実クラス順、ログとSHA256を明示的にダウンロードする。
6. Vision側の現在のFW/モデルの復旧原本・書戻し手順を確保する。XIAOのLittleFSバックアップだけではVisionモデルを復旧できない。復旧不能なら交換しない。
7. 別テスト機または復旧可能な機器で公式サンプル配備とSSCMAの撮影・推論を確認する。現行閉ループの画像取得・ハッシュ対応の回帰を確認する。学習だけ成功しても配備成功に数えない。
8. 公式パイプラインが再現できてから、監査済み幼虫データの固定manifestを入力へ接続する。元testを学習/データ拡張/量子化校正/閾値調整へ入れない。量子化校正はtrainからのみ。

保存先例: Git除外済み `captures/vision_ml/runs/<run-id>/`。
下のtemplateは記入用。未記入値を推測で埋めず、NOT_RUNのまま成功扱いしない。

## 次工程の判定

- Phase 2: 公式サンプル再現ログ、環境固定、Vela成果物、クラス順、復旧可能な配備確認がそろってから完了。
- Phase 3: 幼虫の実画像・人手ラベル・独立評価群が不足するならNo-Go。評価値を合成画像で代替しない。
- Phase 4: `SAMPLE`と`INVOKE`が同じ画像か、SSCMAのクラスID/score/件数/欠落/異常応答を実機で確認。未一致はEvidence画像に対応する分類として扱わない。
- Phase 5: 分類スコアは校正済み確率と呼ばない。モデル情報、品質、閾値版、画像対応とUNKNOWN理由を追跡する。既存Policy判断をPCへ移さない。

実行票の空欄や未確認事項は [open_questions](../../docs/open_questions.md) に残す。
