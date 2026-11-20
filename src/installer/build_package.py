"""Build the offline desktop and reproducible-source ZIPs from explicit inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from package_spec import BOOTSTRAP, HELPERS, RUNTIME, WHEELS, digest


def fetch_missing_wheels(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for filename, expected in WHEELS.items():
        target = directory / filename
        if target.is_file():
            if digest(target) != expected:
                raise ValueError("Existing wheel differs: " + filename)
            continue
        project, version = filename.split("-")[:2]
        with urlopen("https://pypi.org/pypi/" + project + "/" + version + "/json", timeout=25) as response:
            records = json.load(response)["urls"]
        hits = [r for r in records if r["filename"] == filename and r["digests"]["sha256"] == expected]
        if len(hits) != 1:
            raise ValueError("Pinned wheel missing from PyPI: " + filename)
        url = hits[0]["url"]
        origin = urlsplit(url)
        if origin.scheme != "https" or origin.netloc != "files.pythonhosted.org":
            raise ValueError("Unexpected distribution URL")
        fd, temp = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as out, urlopen(url, timeout=60) as src:
                while block := src.read(1024 * 1024):
                    out.write(block)
            if digest(temp) != expected:
                raise ValueError("Downloaded wheel differs: " + filename)
            os.link(temp, target)
        finally:
            os.unlink(temp)


def archive(path, mapping):
    with ZipFile(path, "w") as z:
        for name, src in sorted(mapping.items()):
            data = src if isinstance(src, bytes) else src.read_bytes()
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED if name.endswith(".whl") else ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)


def build(source, binaries, wheels, out):
    installer = Path(__file__).resolve().parent
    payload = {"app/" + n: source / n for n in RUNTIME}
    for stem in HELPERS:
        exe = binaries / (stem + ".ex5")
        if exe.read_bytes()[:3] != b"EX5" or exe.stat().st_size < 4096:
            raise ValueError("Missing compiled helper " + stem)
        payload["helpers/" + exe.name] = exe
    for name, expected in WHEELS.items():
        wheel = wheels / name
        if digest(wheel) != expected:
            raise ValueError("Wheel checksum differs: " + name)
        payload["wheels/" + name] = wheel
    payload = {name: path.read_bytes() for name, path in payload.items()}
    bootstrap_files = {name: (installer / name).read_bytes() for name in BOOTSTRAP}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}
    bootstrap = {name: hashlib.sha256(data).hexdigest() for name, data in bootstrap_files.items()}
    fingerprint = hashlib.sha256(json.dumps([hashes, bootstrap], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    build_id = fingerprint[:16]
    manifest = dict(format=1, build=build_id, files=hashes, bootstrap=bootstrap)
    files = dict(payload)
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files.update(bootstrap_files)
    out.mkdir(exist_ok=True, parents=True)
    setup = out / ("RECOVERY-Python-Windows-" + build_id + ".zip")
    sources = out / ("RECOVERY-Python-Source-" + build_id + ".zip")
    source_files = {name: payload["app/"+name] for name in RUNTIME}
    for path in sorted(source.glob("test_*.py")):
        source_files[path.name] = path
    mql = {p.name: p.read_bytes() for p in (source / "mql").iterdir() if p.suffix in (".mq5", ".mqh")}
    for stem in HELPERS:
        if stem + ".mq5" not in mql:
            raise ValueError("Missing MQL helper source: " + stem)
    for name, data in mql.items():
        for include in re.findall(r'^\s*#\s*include\s*"([^"\r\n]+)"', data.decode("utf-8-sig"), re.MULTILINE):
            if include not in mql:
                raise ValueError(f"Missing packaged MQL include in {name}: {include}")
        source_files["mql/" + name] = data
    for path in sorted(installer.iterdir()):
        if path.is_file() and path.suffix.lower() in (".py", ".cmd", ".txt"):
            source_files["installer/" + path.name] = bootstrap_files.get(path.name, path)
    source_files["binary-manifest.json"] = files["manifest.json"]
    archive(setup, files)
    archive(sources, source_files)
    sums = out / ("RECOVERY-Python-" + build_id + "-SHA256.txt")
    sums.write_text("".join(digest(p) + "  " + p.name + "\n" for p in (setup, sources)), encoding="ascii")
    return setup, sources, sums


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for arg in ("source", "helpers", "wheels", "out"):
        ap.add_argument("--" + arg, type=Path, required=True)
    ap.add_argument("--download-missing-wheels", action="store_true",
                    help="Fetch missing pinned wheels from PyPI only during a rebuild")
    a = ap.parse_args()
    if a.download_missing_wheels:
        fetch_missing_wheels(a.wheels)
    for path in build(a.source, a.helpers, a.wheels, a.out):
        print(path)
