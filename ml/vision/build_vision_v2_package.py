"""Build a local, train-only Colab deployment-smoke bundle. Never uploads or flashes."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

from dataset import digest
from train_trial import validate_trial

HERE = Path(__file__).resolve().parent


def notebook(bundle_sha256):
    cells = []
    def markdown(text):
        cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)})
    def code(text):
        cells.append({"cell_type": "code", "metadata": {}, "execution_count": None,
                      "outputs": [], "source": text.splitlines(keepends=True)})
    markdown("# Grove Vision AI V2 — 幼虫2クラス分類・配備経路のスモーク試験\n"
             "未実行の準備用Notebook。公式ModelAssistant 2.0.0 / MobileNetV2 Rep（事前学習重みに合わせwiden 1.0）を使用。入力96と192を比較用に作成。\n"
             "全件train、独立評価なし。モデルの作成・INT8/Vela変換を確認し、実用精度・自然活動検出を主張しません。\n"
             "公式入口: https://colab.research.google.com/github/seeed-studio/sscma-model-zoo/blob/main/notebooks/en/Person_Classification_MobileNetV2_0.35_Rep_96.ipynb\n"
             "T4 GPUを選択してください。ColabのPythonとは分離したPython 3.11.14環境で公式セットアップを実行します。\n"
             "既存セル出力はありません。失敗時は最後のセルでログを保存してください。")
    code("import subprocess, sys, os\n"
         "from pathlib import Path\n"
         "print(sys.version)\n"
         "subprocess.run(['nvidia-smi'], check=True)\n"
         "subprocess.run([sys.executable,'-m','pip','install','uv==0.9.11'],check=True)\n"
         "subprocess.run([sys.executable,'-m','uv','python','install','3.11.14'],check=True)\n"
         "VENV = Path('/content/beetle_vision_v2_env')\n"
         "if VENV.exists(): raise RuntimeError('専用環境が既に存在します。新しいColabランタイムで実行してください。')\n"
         "subprocess.run([sys.executable,'-m','uv','venv','--python','3.11.14','--seed',str(VENV)],check=True)\n"
         "PYTHON = str(VENV/'bin/python')\n"
         "ENV = dict(os.environ, PATH=str(VENV/'bin')+os.pathsep+os.environ['PATH'], MPLBACKEND='Agg', CUBLAS_WORKSPACE_CONFIG=':4096:8')\n"
         "subprocess.run([PYTHON,'--version'],check=True,env=ENV)\n")
    markdown("## ローカルで作成したvision_v2_smoke_bundle.zipをアップロード\n"
             "この操作で学習用画像をColabへ送信します。元画像のSHA256とラベル順を検査します。")
    code("from google.colab import files\n"
         "from pathlib import Path, PurePosixPath\n"
         "import hashlib, io, zipfile\n"
         f"EXPECTED_BUNDLE_SHA256 = {bundle_sha256!r}\n"
         "uploaded = files.upload()\n"
         "if len(uploaded) != 1: raise ValueError('ZIPを1個だけ選択してください')\n"
         "payload = next(iter(uploaded.values()))\n"
         "if hashlib.sha256(payload).hexdigest() != EXPECTED_BUNDLE_SHA256: raise ValueError('bundle SHA256 mismatch')\n"
         "ROOT = Path('/content/beetle_vision_v2_smoke')\n"
         "ROOT.mkdir(exist_ok=False)\n"
         "with zipfile.ZipFile(io.BytesIO(payload)) as archive:\n"
         "    if sum(item.file_size for item in archive.infolist()) > 64*1024*1024: raise ValueError('bundle too large')\n"
         "    for item in archive.infolist():\n"
         "        relative = PurePosixPath(item.filename)\n"
         "        if relative.is_absolute() or '..' in relative.parts or '\\\\' in item.filename: raise ValueError('unsafe ZIP entry')\n"
         "    archive.extractall(ROOT)\n"
         "print('Verified bundle SHA256; extracted to', ROOT)\n")
    markdown("## 公式セットアップ\n上流セットアップの依存関係をそのまま導入します。成功を保証するものではありません。バージョンとgit commitを保存します。")
    code("def run_stage(stage, *extra):\n"
         "    command = [PYTHON, '-u', str(ROOT/'colab_vision_v2_job.py'), stage, *extra]\n"
         "    with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',env=ENV,bufsize=1) as process:\n"
         "        for line in process.stdout: print(line,end='',flush=True)\n"
         "        returncode = process.wait()\n"
         "    if returncode:\n"
         "        outs = [ROOT/'output'] + sorted((ROOT/'output').glob('input*'))\n"
         "        for path in [o/n for o in outs for n in (stage+'_status.json','clone.log','official-setup.log','official-export-vela.log')]:\n"
         "            if path.is_file():\n"
         "                print('\\n===',name,'===')\n"
         "                print('\\n'.join(path.read_text(encoding='utf-8',errors='replace').splitlines()[-50:]))\n"
         "        raise RuntimeError(f'{stage} failed, exit={returncode}; 上の原因ログを保存してください')\n"
         "run_stage('setup')\n")
    markdown("## カスタム学習 → INT8 → Vela → 内容検査（入力96×96）\n"
             "レシピv12: v11の学習設定（widen 1.0、100 epoch、クラス重み付き損失、RGB入力、head lr 0.005・backbone 1/10・勾配クリップ）に、学習時だけの拡張（ランダム切り出し・JPEG再圧縮・センサーノイズ）を追加。校正・評価には拡張を使いません。出力が一定ならtraining_resultがCOLLAPSED_DO_NOT_DEPLOYになります。重みが構成に合わない場合は学習前に停止します。\n"
             "入力サイズだけを変えた2モデルを順に作ります。結果は output/input96 と output/input192 に分かれます。\n"
             "変換後のINT8モデルを学習画像にRGB順で通すチェック（export_int8_check.json）も保存します。\n"
             "独立validation/testはありません。学習後の同一画像チェック（in_sample_sanity.json）は出力が一定でないかを見るだけで、精度ではありません。exporterの校正入力にはtrainだけを使います。\n"
             "終了後もハードウェア動作は未確認です。")
    code("run_stage('train-export', '--input-size', '96')\n")
    markdown("## 同じデータ・同じ設定で入力192×192\n"
             "96の結果は上のセルで保存済みです。こちらが失敗しても96の成果物は残ります。"
             "Velaの推定SRAM使用量はvela_summary.jsonに記録します（WE2実測ではありません）。")
    code("run_stage('train-export', '--input-size', '192')\n")
    markdown("## 成果物・ログを必ずPCへ保存\n前のセルが失敗しても、このセルで取得できるログを保存します。")
    code("import shutil\n"
         "result = shutil.make_archive('/content/vision_v2_results', 'zip', ROOT/'output')\n"
         "files.download(result)\n")
    return {"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                         "colab": {"name": "vision_v2_smoke_colab.ipynb"}, "accelerator": "GPU"}, "cells": cells}


def build(dataset_root, preflight, output):
    dataset_root, preflight, output = map(Path, (dataset_root, preflight, output))
    manifest = json.loads((dataset_root / "dataset_manifest.json").read_text(encoding="utf-8"))
    validate_trial(manifest)
    summary = json.loads((preflight / "summary.json").read_text(encoding="utf-8"))
    import base64
    info = next(row["data"]["info"] for row in summary["vision_replies"] if row["name"] == "INFO?")
    model_info = json.loads(base64.b64decode(info, validate=True))
    version = next(row["data"] for row in summary["vision_replies"] if row["name"] == "VER?")
    if not model_info["url"].startswith("https://files.seeedstudio.com/"):
        raise ValueError("unexpected rollback model source")
    entries = {}
    images = []
    for row in manifest["images"]:
        relative = Path(row["image"])
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("unsafe image path")
        path = (dataset_root / relative).resolve(strict=True)
        path.relative_to(dataset_root.resolve())
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("JPEG SHA256 mismatch")
        filename = "data/" + relative.as_posix()
        if filename in entries:
            raise ValueError("duplicate archive image path")
        entries[filename] = content
        images.append({"file": filename, "image_id": row["image_id"], "sha256": row["sha256"],
                       "class_id": row["class_id"], "label": row["label"],
                       "capture_session_id": row["capture_session_id"], "case_id": row["case_id"]})
    source_files = []
    for filename in ("colab_vision_v2_job.py", "check_vision_v2_artifact.py", "evaluate_tflite.py", "bll_augment.py"):
        entries[filename] = (HERE / filename).read_bytes()
        source_files.append({"file": filename, "sha256": hashlib.sha256(entries[filename]).hexdigest()})
    rollback = {"reported_version": version, "model_name": model_info["name"],
                "model_version": model_info["version"], "model_url": model_info["url"],
                "original_model_bytes_backed_up": False, "firmware_binary_backed_up": False,
                "note": "metadata/distribution reference only; verify download and firmware recovery before any flash"}
    entries["rollback_reference.json"] = (json.dumps(rollback, indent=2) + "\n").encode()
    source_files.append({"file": "rollback_reference.json", "sha256": hashlib.sha256(entries["rollback_reference.json"]).hexdigest()})
    smoke = {"schema": "bll.vision_v2_smoke_dataset.v1", "purpose": "VISION_V2_DEPLOYMENT_SMOKE_ONLY",
             "class_order": manifest["class_order"], "counts": manifest["counts"], "images": images,
             "source_dataset_sha256": digest(manifest), "source_files": source_files,
             "independent_evaluation_available": False, "production_eligible": False,
             "image_conditions": manifest["conditions"],
             "note": "not a performance evaluation; previous CPU model weights are not included"}
    entries["smoke_manifest.json"] = (json.dumps(smoke, ensure_ascii=False, indent=2) + "\n").encode()
    entries["labels.txt"] = ("\n".join(manifest["class_order"]) + "\n").encode()
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "vision_v2_smoke_bundle.zip"
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in sorted(entries.items()):
            archive.writestr(filename, content)
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (output / "vision_v2_smoke_colab.ipynb").write_text(json.dumps(notebook(sha), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "package_summary.json").write_text(json.dumps({"bundle_sha256": sha, "counts": manifest["counts"],
        "hardware_operation_verified": False, "training_performed": False,
        "class_order": manifest["class_order"], "rollback": rollback}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"bundle": str(bundle), "sha256": sha, "counts": manifest["counts"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.dataset, args.preflight, args.output), ensure_ascii=False))
