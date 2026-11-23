"""Fetch fixed upstream sources and verify every file against its Git blob SHA1."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1] / "firmware/xiao_esp32s3/lib"
SOURCES = [
    ("Seeed_Arduino_SSCMA", "Seeed-Studio/Seeed_Arduino_SSCMA",
     "65a1b2061e80f1bd3814f688451a66646782fe1f"),
    ("ArduinoJson", "bblanchon/ArduinoJson",
     "40ee05c065ce248192c47eb37af7a70a6935dfa6"),
]


def get(url):
    with urlopen(Request(url, headers={"User-Agent": "Beetle-Life-Lab-setup"}), timeout=30) as response:
        return response.read()


def fetch_library(source):
    name, repo, commit = source
    tree = json.loads(get(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1"))
    if tree.get("truncated"):
        raise RuntimeError("Incomplete upstream tree")
    entries = [e for e in tree["tree"] if e["type"] == "blob" and
               (e["path"].startswith("src/") or e["path"] in
                ("library.json", "library.properties", "LICENSE", "LICENSE.txt"))]

    def download(entry):
        relative = entry["path"]
        path = ROOT / name / relative
        content = path.read_bytes() if path.exists() else b""
        def blob_hash(data):
            return sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if blob_hash(content) != entry["sha"]:
            content = get(f"https://raw.githubusercontent.com/{repo}/{commit}/{relative}")
        if blob_hash(content) != entry["sha"]:
            raise RuntimeError(f"Upstream hash mismatch: {name}/{relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(download, entries))
    (ROOT / name / "UPSTREAM.txt").write_text(
        f"https://github.com/{repo}\ncommit: {commit}\n"
        "Source files unchanged. Restored by scripts/fetch_vision_libraries.py.\n",
        encoding="utf-8")
    print(f"Verified {name}: {len(entries)} files at {commit}", flush=True)


if __name__ == "__main__":
    for source in SOURCES:
        fetch_library(source)
