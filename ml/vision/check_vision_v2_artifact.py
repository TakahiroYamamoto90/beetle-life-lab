"""Inspect TFLite tensors and actual Ethos-U operator use. Does not flash or prove hardware operation."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


SUPPORTED_INPUT_SIZES = (96, 192)


def validate_description(info, input_size=96):
    if input_size not in SUPPORTED_INPUT_SIZES:
        raise ValueError(f"input size must be one of {SUPPORTED_INPUT_SIZES}")
    if (info.get("input_shape") != [1, input_size, input_size, 3] or
            info.get("output_shape") != [1, 2]):
        raise ValueError(f"expected NHWC{input_size} RGB input and two classification outputs")
    for prefix in ("input", "output"):
        if info.get(prefix + "_dtype") not in {"INT8", "UINT8"}:
            raise ValueError("quantized integer input/output required")
        scales = info.get(prefix + "_scales", [])
        zero_points = info.get(prefix + "_zero_points", [])
        if not scales or not zero_points or any(not 0 < value < float("inf") for value in scales):
            raise ValueError("valid quantization parameters required")
        low, high = (-128, 127) if info[prefix + "_dtype"] == "INT8" else (0, 255)
        if any(not low <= value <= high for value in zero_points):
            raise ValueError("quantization zero point outside tensor type range")
    if "ethos-u" not in info.get("used_custom_operators", []):
        raise ValueError("Vela Ethos-U custom operator must actually be used")


def inspect(path, input_size=96):
    path = Path(path)
    data = path.read_bytes()
    if (len(data) < 16 or data[4:8] != b"TFL3" or
            not 8 <= struct.unpack_from("<I", data)[0] < len(data)):
        raise ValueError("not a TFLite FlatBuffer; renaming a file is insufficient")
    try:
        from tensorflow.lite.python import schema_py_generated as schema
    except ImportError:
        try:
            import tflite as schema
        except ImportError as exc:
            raise RuntimeError("TFLite schema parser unavailable; artifact remains UNVERIFIED") from exc
    model = schema.Model.GetRootAsModel(data, 0)
    graph = model.Subgraphs(0)
    if graph.InputsLength() != 1 or graph.OutputsLength() != 1:
        raise ValueError("one input and one output required")
    info = {}
    for prefix, index in (("input", graph.Inputs(0)), ("output", graph.Outputs(0))):
        tensor = graph.Tensors(index)
        quantization = tensor.Quantization()
        info[prefix + "_shape"] = [int(tensor.Shape(i)) for i in range(tensor.ShapeLength())]
        info[prefix + "_dtype"] = {9: "INT8", 3: "UINT8"}.get(tensor.Type(), "OTHER")
        info[prefix + "_scales"] = ([float(quantization.Scale(i)) for i in range(quantization.ScaleLength())]
                                     if quantization is not None else [])
        info[prefix + "_zero_points"] = ([int(quantization.ZeroPoint(i)) for i in range(quantization.ZeroPointLength())]
                                          if quantization is not None else [])
    used = set()
    for i in range(graph.OperatorsLength()):
        code = model.OperatorCodes(graph.Operators(i).OpcodeIndex())
        custom = code.CustomCode()
        if code.BuiltinCode() == 32 and custom:
            used.add(custom.decode("utf-8"))
    info["used_custom_operators"] = sorted(used)
    validate_description(info, input_size)
    return {"schema": "bll.vision_v2_artifact_inspection.v1", "sha256": hashlib.sha256(data).hexdigest(),
            "file": path.name, **info, "status": "BENCH_FLASH_CANDIDATE",
            "hardware_operation_verified": False, "production_eligible": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-size", type=int, choices=SUPPORTED_INPUT_SIZES, default=96)
    args = parser.parse_args()
    result = inspect(args.model, args.input_size)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))
