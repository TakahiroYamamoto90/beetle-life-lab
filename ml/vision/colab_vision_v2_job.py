"""Colab runner around Seeed ModelAssistant 2.0.0. Prepared, not yet GPU/board verified."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
BASE_OUT = ROOT / "output"
# Shared setup outputs live in BASE_OUT; each train-export run redirects OUT to
# BASE_OUT / f"input{size}" so the 96 and 192 runs never overwrite each other.
OUT = BASE_OUT
REPO = ROOT / "ModelAssistant"
REPOSITORY = "https://github.com/Seeed-Studio/ModelAssistant.git"
UPSTREAM_REF = "2.0.0"
CONFIG_PATH = "configs/classification/mobnetv2_0.35_rep_1bx16_300e_custom.py"
PRETRAIN = "https://files.seeedstudio.com/sscma/model_zoo/classification/person/mobilenetv2_0.35rep_vww96_float32_sha1_0b47deccb4ffab4d8f970ea6379b838163e5bd8f.pth"
PRETRAIN_SHA1 = "0b47deccb4ffab4d8f970ea6379b838163e5bd8f"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args, name, cwd=ROOT):
    print("RUN", name, flush=True)
    status = {"command": list(args), "status": "RUNNING",
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    status_path = OUT / (name + ".status.json")
    write_json(status_path, status)
    try:
        with (OUT / (name + ".log")).open("w", encoding="utf-8") as log:
            with subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1) as process:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                code = process.wait()
        status.update(status="COMPLETED" if code == 0 else "FAILED", exit_code=code)
        if code:
            raise RuntimeError(f"{name} failed, exit={code}; see output/{name}.log")
    except Exception as exc:
        status.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        status["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(status_path, status)


def download(url, path, sha1=None):
    with urlopen(url, timeout=120) as response:
        content = response.read()
    if sha1 is not None:
        actual_sha1 = hashlib.sha1(content).hexdigest()
        if actual_sha1 != sha1:
            raise ValueError(
                f"upstream model SHA1 mismatch: url={url}, expected={sha1}, "
                f"actual={actual_sha1}, bytes={len(content)}")
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def configure_noninteractive_matplotlib():
    # The Colab parent kernel exports its inline backend into child environments.
    # The isolated runner has no IPython inline backend; use a headless backend.
    os.environ["MPLBACKEND"] = "Agg"
    # PyTorch deterministic CUDA algorithms require this before torch initializes.
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"


def disable_training_validation(cfg):
    """Keep export calibration data in its config, but omit MMEngine's val loop."""
    training_cfg = cfg.copy()
    training_cfg["val_dataloader"] = None
    training_cfg["val_cfg"] = None
    training_cfg["val_evaluator"] = None
    return training_cfg


# Recipe v8. The "mobilenetv2_0.35rep_vww96" checkpoint actually stores a
# widen_factor=1.0 backbone (its embedded cfg and tensor shapes say so) with a
# 128-channel head input. The upstream 0.35 custom config made 1230 of 1470
# tensors shape-incompatible, so v6/v7 trained an almost random backbone.
PRETRAIN_ARCHITECTURE = {
    "backbone": {"type": "MobileNetv2", "widen_factor": 1.0, "rep": True, "gray_input": False},
    "head_in_channels": 128,
}
MAX_EPOCHS = 100
BATCH_SIZE = 8
WARMUP_ITERS = 100
LR_MILESTONES = [70, 90]
# Recipe v9: the Vision V2 feeds the model RGB (2026-11-03: recorded device
# scores match PC RGB inference, not BGR). mmcv LoadImageFromFile yields BGR,
# so the data preprocessor swaps to RGB for training and float checks.
# Recipe v10: same training settings as v9, square model input selectable per
# run (96 = previous, 192 = higher-resolution comparison). Camera JPEGs are 240.
# Recipe v11 (2026-11-04): v10 (161 images, SGD lr 0.01 on every layer) drifted the
# pretrained backbone and its pooled features collapsed to ~0 (mean |x| 0.0005 vs
# 0.085 in v9), so every image scored ~50. Fine-tune gently instead: lower head
# LR, backbone at 1/10 of it, gradient clipping, longer warmup.
RECIPE = "v11"
# Recipe v12 (2026-11-04): v11 optimizer plus train-only sensor augmentation
# (crop, JPEG re-encode, Gaussian noise; ml/vision/bll_augment.py) because the
# device infers on noisy pre-JPEG frames and missed larvae cut by the frame edge.
RECIPE = "v12"
AUGMENT_TYPE = "mmcls.BllSensorAugment"
BASE_LR = 0.005
BACKBONE_LR_MULT = 0.1
CLIP_GRAD_MAX_NORM = 5.0
INPUT_COLOR_ORDER = "RGB (mmcv BGR load + data_preprocessor bgr_to_rgb=True); matches Vision V2 device order"
INPUT_SIZES = (96, 192)


def set_input_size(cfg, size):
    """Point every Resize transform (train, calibration, test) and imgsz at size x size."""
    if size not in INPUT_SIZES:
        raise ValueError(f"input size must be one of {INPUT_SIZES}")
    count = 0

    def walk(node):
        nonlocal count
        if isinstance(node, dict):
            if node.get("type") in ("mmengine.Resize", "Resize") and "scale" in node:
                node["scale"] = (size, size)
                count += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    for key in ("train_dataloader", "val_dataloader", "test_dataloader", "train_pipeline", "test_pipeline"):
        if key in cfg and cfg[key] is not None:
            walk(cfg[key])
    for key in ("imgsz",):
        if key in cfg:
            cfg[key] = (size, size)
    for key in ("height", "width"):
        if key in cfg:
            cfg[key] = size
    if count == 0:
        raise ValueError("no Resize transform found; refusing to train at an unknown input size")
    return count


def add_sensor_augmentation(cfg):
    """Insert the augmentation right after image loading in the TRAIN pipeline only.

    Calibration (val_dataloader) and any test pipeline stay untouched, so the
    exported INT8 model is calibrated on unmodified training JPEGs.
    """
    from bll_augment import DEFAULTS, SEED
    pipeline = cfg["train_dataloader"]["dataset"]["pipeline"]
    if any(step.get("type") == AUGMENT_TYPE for step in pipeline):
        raise ValueError("sensor augmentation already present")
    loads = [i for i, step in enumerate(pipeline) if step.get("type") in ("LoadImageFromFile", "mmcv.LoadImageFromFile")]
    if len(loads) != 1:
        raise ValueError("expected exactly one LoadImageFromFile in the train pipeline")
    options = {key: list(value) if isinstance(value, tuple) else value for key, value in DEFAULTS.items()}
    pipeline.insert(loads[0] + 1, dict(type=AUGMENT_TYPE, seed=SEED, **options))
    return {"type": AUGMENT_TYPE, "seed": SEED, **options}


def vela_summary(log_text):
    """Key Vela 'Network summary' figures (estimates for Vela's default system config)."""
    keys = {"Total SRAM used": "sram_used", "Total Off-chip Flash used": "flash_used",
            "Neural network macs": "macs_per_inference", "System configuration": "system_configuration",
            "Memory mode": "memory_mode", "NPU operators": "npu_operators", "CPU operators": "cpu_operators"}
    found = {}
    for line in log_text.splitlines():
        for label, name in keys.items():
            if line.startswith(label) and name not in found:
                found[name] = line[len(label):].strip()
    found["note"] = "Vela default system/memory config, not a measured Himax WE2 figure"
    return found


def class_weights(counts_by_id, num_classes):
    """Inverse-frequency weights, mean 1 over classes: total / (classes * count)."""
    total = sum(counts_by_id.get(i, 0) for i in range(num_classes))
    if any(counts_by_id.get(i, 0) <= 0 for i in range(num_classes)):
        raise ValueError("every class needs at least one training image")
    return [round(total / (num_classes * counts_by_id[i]), 6) for i in range(num_classes)]


def apply_training_recipe(cfg, counts_by_id, num_classes):
    """Match the pretrained architecture, train long enough, weight rare classes, RGB input."""
    cfg["model"]["backbone"] = dict(PRETRAIN_ARCHITECTURE["backbone"])
    cfg["model"]["data_preprocessor"]["bgr_to_rgb"] = True
    head = cfg["model"]["head"]
    head["in_channels"] = PRETRAIN_ARCHITECTURE["head_in_channels"]
    head["num_classes"] = num_classes
    head["topk"] = 1
    weights = class_weights(counts_by_id, num_classes)
    head["loss"] = dict(type="mmcls.CrossEntropyLoss", loss_weight=1.0, class_weight=weights)
    optim = cfg["optim_wrapper"]
    optim["optimizer"]["lr"] = BASE_LR
    optim["paramwise_cfg"] = dict(custom_keys={"backbone": dict(lr_mult=BACKBONE_LR_MULT)})
    optim["clip_grad"] = dict(max_norm=CLIP_GRAD_MAX_NORM)
    cfg["train_cfg"]["max_epochs"] = MAX_EPOCHS
    cfg["train_cfg"]["val_interval"] = MAX_EPOCHS + 1
    cfg["param_scheduler"] = [
        dict(type="LinearLR", begin=0, end=WARMUP_ITERS, by_epoch=False, start_factor=0.001),
        dict(type="MultiStepLR", begin=1, end=MAX_EPOCHS, by_epoch=True,
             milestones=list(LR_MILESTONES), gamma=0.1),
    ]
    checkpoint = cfg["default_hooks"]["checkpoint"]
    checkpoint["save_best"] = None
    checkpoint["interval"] = MAX_EPOCHS
    checkpoint["save_last"] = True
    return weights


def checkpoint_compatibility(model_shapes, checkpoint_shapes):
    """Compare {name: shape} maps; every model tensor must load from the checkpoint."""
    missing = sorted(k for k in model_shapes if k not in checkpoint_shapes)
    mismatched = sorted(k for k in model_shapes
                        if k in checkpoint_shapes and tuple(model_shapes[k]) != tuple(checkpoint_shapes[k]))
    unexpected = sorted(k for k in checkpoint_shapes if k not in model_shapes)
    return {"model_tensors": len(model_shapes), "loaded": len(model_shapes) - len(missing) - len(mismatched),
            "missing": missing, "shape_mismatch": mismatched, "unexpected": unexpected,
            "compatible": not missing and not mismatched}


def prediction_collapse(predicted_ids, num_classes):
    """True when the model predicts a single class for every image (constant output)."""
    return len(set(predicted_ids)) < min(2, num_classes)


def in_sample_sanity(runner, dataset, classes, size=96):
    """Float model on its own TRAINING images. Detects collapse only; not accuracy."""
    try:
        import mmcv
        import torch
        model = runner.model.eval()
        device = next(model.parameters()).device
        rows = []
        with torch.no_grad():
            for index in range(len(dataset)):
                info = dataset.get_data_info(index)
                # Same as training: BGR load, bilinear size x size, swap to RGB, x/255.
                image = mmcv.imresize(mmcv.imread(info["img_path"]), (size, size))[:, :, ::-1].copy()
                tensor = torch.from_numpy(image).permute(2, 0, 1).float().unsqueeze(0).to(device) / 255.0
                logits = model(tensor, mode="tensor")
                if isinstance(logits, (list, tuple)):
                    logits = logits[-1]
                probs = torch.softmax(logits.float(), dim=-1)[0].tolist()
                rows.append({"image": Path(info["img_path"]).name, "label_id": int(info["gt_label"]),
                             "predicted_id": int(max(range(len(probs)), key=probs.__getitem__)),
                             "probabilities": [round(p, 4) for p in probs]})
    except Exception as exc:
        return {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}", "scope": "IN_SAMPLE_TRAINING_IMAGES"}
    confusion = [[sum(1 for r in rows if r["label_id"] == t and r["predicted_id"] == p)
                  for p in range(len(classes))] for t in range(len(classes))]
    collapsed = prediction_collapse([r["predicted_id"] for r in rows], len(classes))
    return {"status": "COLLAPSED" if collapsed else "NOT_COLLAPSED",
            "scope": "IN_SAMPLE_TRAINING_IMAGES", "not_an_accuracy_estimate": True,
            "classes": list(classes), "confusion_rows_true_cols_pred": confusion, "rows": rows}


def int8_export_check(work_dir, dataset):
    """Run the exported INT8 (non-Vela) TFLite on the training images in device (RGB) order.

    Confirms export and calibration kept the RGB contract. In-sample: not an accuracy estimate.
    """
    try:
        from evaluate_tflite import DEVICE_INPUT_ORDER, evaluate, load_interpreter
        candidates = [p for p in Path(work_dir).glob("*_int8.tflite") if not p.name.endswith("_vela.tflite")]
        if len(candidates) != 1:
            raise ValueError(f"expected one non-Vela INT8 TFLite, found {len(candidates)}")
        items = [{"id": Path(info["img_path"]).name, "path": info["img_path"], "label": int(info["gt_label"]),
                  "group": "train"}
                 for info in (dataset.get_data_info(i) for i in range(len(dataset)))]
        result = evaluate(load_interpreter(candidates[0]), items, DEVICE_INPUT_ORDER)
    except Exception as exc:
        return {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}", "scope": "IN_SAMPLE_TRAINING_IMAGES"}
    return {"status": "COLLAPSED" if result["all"]["collapsed"] else "NOT_COLLAPSED",
            "scope": "IN_SAMPLE_TRAINING_IMAGES", "not_an_accuracy_estimate": True,
            "model": candidates[0].name, **result}


def download_rollback_reference(rollback):
    result = {"url": rollback.get("model_url"), "flash_readback": False,
              "firmware_backup": False}
    url = result["url"]
    if not url:
        result.update(status="NOT_AVAILABLE", note="device reported no model URL")
    else:
        try:
            filename = url.rsplit("/", 1)[-1]
            sha1 = filename.split("_sha1_", 1)[1].split("_", 1)[0]
            result["expected_sha1_from_filename"] = sha1
            result["sha256"] = download(url, OUT / "original_face_model_int8_vela.tflite", sha1)
        except Exception as exc:
            # This reference download is independent of training. Keep strict hash
            # verification, record the missing rollback artifact, and continue.
            result.update(status="UNVERIFIED", error=f"{type(exc).__name__}: {exc}")
            print("WARNING: original Vision model reference was not verified; "
                  "no rollback artifact was saved. " + result["error"], flush=True)
        else:
            result["status"] = "VERIFIED_DISTRIBUTION_COPY"
    write_json(OUT / "original_model_download.json", result)
    return result


def verify_package():
    manifest = json.loads((ROOT / "smoke_manifest.json").read_text(encoding="utf-8"))
    if (manifest["purpose"] != "VISION_V2_DEPLOYMENT_SMOKE_ONLY" or
            manifest["class_order"] != ["NO_VISIBLE_LARVA", "VISIBLE_LARVA"] or
            manifest["independent_evaluation_available"] is not False):
        raise ValueError("wrong smoke package contract")
    for item in manifest["images"] + manifest["source_files"]:
        path = (ROOT / item["file"]).resolve(strict=True)
        path.relative_to(ROOT)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("package hash mismatch: " + item["file"])
    return manifest


def setup():
    verify_package()
    if sys.version_info[:2] not in {(3, 10), (3, 11)}:
        raise RuntimeError("official Torch 2.0.0 wheels require a compatible Python runtime; use Python 3.10/3.11, do not silently replace the pipeline")
    run(["nvidia-smi"], "gpu-before-setup")
    if REPO.exists():
        raise RuntimeError("ModelAssistant already exists; use a fresh runtime or inspect prior logs")
    run(["git", "clone", "--depth", "1", "--branch", UPSTREAM_REF, REPOSITORY, str(REPO)], "clone")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    write_json(OUT / "source_lock.json", {"repository": REPOSITORY, "requested_ref": UPSTREAM_REF,
                                          "resolved_commit": revision,
                                          "config_sha256": hashlib.sha256((REPO / CONFIG_PATH).read_bytes()).hexdigest()})
    # Official setup is retained. Its loose transitive dependencies are recorded, not claimed tested.
    run(["bash", "scripts/setup_colab.sh"], "official-setup", REPO)
    run([sys.executable, "-m", "pip", "freeze"], "pip-freeze")
    weight_hash = download(PRETRAIN, OUT / "pretrain.pth", PRETRAIN_SHA1)
    write_json(OUT / "pretrain_source.json", {"url": PRETRAIN, "sha1": PRETRAIN_SHA1, "sha256": weight_hash})
    # Attempt to retain a verified distribution copy; this is not a flash readback.
    rollback = json.loads((ROOT / "rollback_reference.json").read_text(encoding="utf-8"))
    download_rollback_reference(rollback)


def train_export(size=96):
    configure_noninteractive_matplotlib()
    manifest = verify_package()
    import torch
    if not torch.cuda.is_available() or not torch.__version__.startswith("2.0.0"):
        raise RuntimeError("official setup must provide Torch 2.0.0 with GPU; no CPU fallback")
    import sscma.datasets
    import sscma.engine
    import sscma.evaluation
    import sscma.models
    import sscma.visualization
    from mmengine.config import Config
    from mmengine.registry import init_default_scope
    from mmengine.runner import Runner
    cfg = Config.fromfile(str(REPO / CONFIG_PATH))
    classes = tuple(manifest["class_order"])
    manifest_counts = Counter(item["class_id"] for item in manifest["images"])
    weights = apply_training_recipe(cfg, dict(manifest_counts), len(classes))
    resize_transforms = set_input_size(cfg, size)
    for loader in (cfg.train_dataloader, cfg.val_dataloader):
        loader.batch_size = BATCH_SIZE
        loader.num_workers = 0
        loader.persistent_workers = False
        loader.dataset.data_root = str(ROOT / "data")
        loader.dataset.data_prefix = "train/"
        loader.dataset.metainfo = dict(classes=classes)
    # The export tool calibrates through val_dataloader: point it to TRAIN only.
    # It is NOT an evaluation set. Skip all validation loops/metrics during training.
    cfg.test_dataloader = cfg.test_evaluator = cfg.test_cfg = None
    cfg.randomness = dict(seed=20261102, deterministic=True)
    cfg.load_from = str(BASE_OUT / "pretrain.pth")
    cfg.work_dir = str(OUT / "training")
    # The export tool reads this config (model + calibration loader). It never
    # sees the custom train transform, so export does not need it registered.
    config = OUT / "larva_mobilenetv2_smoke_resolved.py"
    cfg.dump(str(config))
    import bll_augment
    bll_augment.register()
    augmentation = add_sensor_augmentation(cfg)
    cfg.dump(str(OUT / "larva_mobilenetv2_training_resolved.py"))
    init_default_scope(cfg.default_scope)
    os.chdir(REPO)
    # MMEngine runs a final validation at max_epochs even when val_interval is
    # greater than max_epochs. The saved export config retains val_dataloader
    # for calibration, but the training Runner must not evaluate on train data.
    runner = Runner.from_cfg(disable_training_validation(cfg))
    dataset = runner.train_dataloader.dataset
    if tuple(dataset.metainfo["classes"]) != classes or len(dataset) != len(manifest["images"]):
        raise ValueError("dataset loader class order/count mismatch")
    observed = Counter(int(dataset.get_data_info(i)["gt_label"]) for i in range(len(dataset)))
    expected = Counter(item["class_id"] for item in manifest["images"])
    if observed != expected:
        raise ValueError("dataset loader label IDs mismatch")
    for index in range(len(dataset)):
        item = dataset.get_data_info(index)
        if Path(item["img_path"]).parent.name != classes[int(item["gt_label"])]:
            raise ValueError("loader image folder disagrees with the actual class ID")
    write_json(OUT / "verified_loader.json", {"classes": classes, "counts_by_id": dict(observed),
        "model_input_size": [size, size], "resize_transforms_set": resize_transforms,
        "training_validation_enabled": False, "calibration_source": "train",
        "independent_evaluation": None})
    # Refuse to train when the pretrained weights do not fit the model (v6/v7 defect).
    pretrained = torch.load(str(BASE_OUT / "pretrain.pth"), map_location="cpu")
    pretrained = pretrained.get("state_dict", pretrained)
    compatibility = checkpoint_compatibility(
        {k: tuple(v.shape) for k, v in runner.model.state_dict().items()},
        {k: tuple(v.shape) for k, v in pretrained.items()})
    write_json(OUT / "pretrain_compatibility.json", compatibility)
    if not compatibility["compatible"]:
        raise ValueError(f"pretrained weights do not fit the model: {len(compatibility['missing'])} missing, "
                         f"{len(compatibility['shape_mismatch'])} shape mismatches; see pretrain_compatibility.json")
    runner.train()
    sanity = in_sample_sanity(runner, dataset, classes, size)
    write_json(OUT / "in_sample_sanity.json", sanity)
    last_file = Path(cfg.work_dir) / "last_checkpoint"
    checkpoint = Path(last_file.read_text(encoding="utf-8").strip())
    if not checkpoint.is_file() or checkpoint.suffix != ".pth":
        raise ValueError("last checkpoint missing")
    del runner
    torch.cuda.empty_cache()
    run([sys.executable, "tools/export.py", str(config), str(checkpoint),
         "--targets", "tflite", "vela", "--precisions", "int8", "--device", "cpu",
         "--input-shape", "1", "3", str(size), str(size), "--calibration-epochs", str(len(dataset)),
         "--vela", "accelerator-config=ethos-u55-64"], "official-export-vela", REPO)
    files = list(Path(cfg.work_dir).glob("*_int8_vela.tflite"))
    if len(files) != 1:
        raise ValueError("expected exactly one Vela INT8 artifact")
    from check_vision_v2_artifact import inspect
    report = inspect(files[0], size)  # refuses an input shape other than [1, size, size, 3]
    vela = vela_summary((OUT / "official-export-vela.log").read_text(encoding="utf-8", errors="replace"))
    write_json(OUT / "vela_summary.json", vela)
    write_json(OUT / "artifact_inspection.json", report)
    export_check = int8_export_check(Path(cfg.work_dir), dataset)
    write_json(OUT / "export_int8_check.json", export_check)
    (OUT / "labels.txt").write_text("\n".join(classes) + "\n", encoding="utf-8")
    collapsed = "COLLAPSED" in (sanity["status"], export_check.get("status"))
    write_json(OUT / "training_result.json", {
        "status": "COLLAPSED_DO_NOT_DEPLOY" if collapsed else "EXPORTED_BENCH_CANDIDATE", "artifact": report,
        "optimizer": {"base_lr": BASE_LR, "backbone_lr_mult": BACKBONE_LR_MULT,
                      "clip_grad_max_norm": CLIP_GRAD_MAX_NORM, "warmup_iters": WARMUP_ITERS},
        "production_eligible": False, "independent_evaluation": None, "hardware_operation_verified": False,
        "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__,
        "source_dataset_sha256": manifest["source_dataset_sha256"], "seed": 20261102,
        "classification_scope": "VISIBLE_PRESENCE_ONLY", "camera_jpeg_size": [240, 240],
        "model_input_size": [size, size], "calibration_source": "train", "vela_summary": vela,
        "training_validation_performed": False, "independent_evaluation": None,
        "recipe": RECIPE, "augmentation": augmentation,
        "architecture": PRETRAIN_ARCHITECTURE, "max_epochs": MAX_EPOCHS,
        "batch_size": BATCH_SIZE, "class_weight": weights, "pretrain_compatible": True,
        "input_color_order": INPUT_COLOR_ORDER, "in_sample_sanity": sanity["status"],
        "export_int8_check": export_check.get("status")})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("setup", "train-export"))
    parser.add_argument("--input-size", type=int, choices=INPUT_SIZES, default=96)
    args = parser.parse_args()
    BASE_OUT.mkdir(exist_ok=True)
    if args.stage == "train-export":
        OUT = BASE_OUT / f"input{args.input_size}"
        OUT.mkdir(exist_ok=False)
    state = {"stage": args.stage, "input_size": args.input_size, "started_at_utc": datetime.now(timezone.utc).isoformat(),
             "status": "RUNNING", "hardware_operation_verified": False}
    try:
        setup() if args.stage == "setup" else train_export(args.input_size)
        state["status"] = "COMPLETED"
    except Exception as exc:
        state.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        state["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(OUT / (args.stage + "_status.json"), state)
