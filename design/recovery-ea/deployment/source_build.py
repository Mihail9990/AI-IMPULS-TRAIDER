"""Deterministic source handoff and isolated MetaEditor compile, not an installer."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import tempfile
import zipfile

BASE = "design/recovery-ea/"
DIAGNOSTIC = ("verifier/RecoveryVerifier.mq5", "probe/RecoveryPairProbe.mq5")
PATH_RX = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(?:mq5|mqh)")
RESULT_RX = re.compile(r"^\s*Result:\s*(\d+)\s+errors?,\s*(\d+)\s+warnings?\b", re.I | re.M)
INCLUDES = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
MAX_FILE = 1 << 20
MAX_FILES = 128
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)),
                   *(f"LPT{i}" for i in range(1,10))}


def validated_path(value):
    if not isinstance(value, str) or not PATH_RX.fullmatch(value) or any(
        part.split(".")[0].upper() in WINDOWS_DEVICES for part in value.split("/")
    ):
        raise ValueError(f"Invalid source path: {value!r}")
    return value


def validated_targets(mode, targets):
    if not isinstance(targets, list):
        raise ValueError("Invalid target list")
    if mode == "diagnostic" and targets == list(DIAGNOSTIC):
        return targets
    if mode == "candidate" and len(targets) == 2 and targets[0] == DIAGNOSTIC[0]:
        p = validated_path(targets[1])
        if p.endswith(".mq5") and p.startswith(("execution/", "runtime/")):
            return targets
    raise ValueError("Unexpected mode or entry points")


def git(repo, *cmd):
    return subprocess.check_output(["git", "-C", str(repo), *cmd], timeout=30)


def package(repo, revision, output, trader=None):
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Supply an immutable 40-character Git commit hash")
    if git(repo, "rev-parse", "--verify", revision+"^{commit}").decode().strip() != revision:
        raise ValueError("Not the requested commit")
    mode = "candidate" if trader else "diagnostic"
    targets = validated_targets(mode, [DIAGNOSTIC[0], trader] if trader else list(DIAGNOSTIC))
    queue = targets[:]
    sources = {}
    while queue:
        path = validated_path(queue.pop())
        if path in sources:
            continue
        if len(sources) >= MAX_FILES:
            raise ValueError("Too many source dependencies")
        entry = git(repo, "ls-tree", revision, "--", BASE+path).decode().split()
        if len(entry) != 4 or entry[0] != "100644" or entry[1] != "blob" or entry[3] != BASE+path:
            raise ValueError(f"Missing or non-regular pinned source: {path}")
        if int(git(repo, "cat-file", "-s", entry[2])) > MAX_FILE:
            raise ValueError("Source exceeds size limit")
        b = git(repo, "cat-file", "blob", entry[2])
        s = b.decode("utf-8-sig")
        if re.search(r"^\s*#\s*resource\b", s, re.M):
            raise ValueError(f"Explicit resource packaging not implemented: {path}")
        sources[path] = b
        for inc in INCLUDES.findall(s):
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(path), inc.replace("\\", "/")))
            queue.append(validated_path(normalized))
    manifest = {"schema": 1, "commit": revision, "mode": mode,
                "targets": targets, "sha256": {p: hashlib.sha256(b).hexdigest() for p,b in sources.items()}}
    # EX5 files, release secrets and working copy changes never enter the archive.
    with open(output, "xb") as f, zipfile.ZipFile(f, "w") as z:
        for p,b in sorted(sources.items()):
            z.writestr(zipfile.ZipInfo("src/"+p), b)
        z.writestr(zipfile.ZipInfo("manifest.json"), json.dumps(manifest, sort_keys=True)+"\n")
    return manifest


def read_package(path, expected_commit, expected_archive_sha256):
    if not re.fullmatch("[0-9a-f]{40}", expected_commit):
        raise ValueError("Invalid expected commit")
    if not re.fullmatch("[0-9a-f]{64}", expected_archive_sha256):
        raise ValueError("Invalid expected ZIP SHA-256")
    if path.stat().st_size > (MAX_FILE + 512) * MAX_FILES:
        raise ValueError("ZIP exceeds size limit")
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != expected_archive_sha256:
        raise ValueError("ZIP SHA-256 mismatch")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        infos=z.infolist()
        if not 1 < len(infos) <= MAX_FILES+1 or any(
            i.file_size > MAX_FILE or i.is_dir() or i.flag_bits & 1 for i in infos
        ):
            raise ValueError("Invalid archive member")
        names = {i.filename for i in infos}
        if len(names) != len(infos) or "manifest.json" not in names:
            raise ValueError("Duplicate paths or missing manifest")
        manifest=json.loads(z.read("manifest.json"))
        if set(manifest) != {"schema", "commit", "mode", "targets", "sha256"} or manifest["schema"] != 1 or manifest["commit"] != expected_commit:
            raise ValueError("Invalid manifest or commit")
        targets=validated_targets(manifest["mode"], manifest["targets"])
        hashes=manifest["sha256"]
        if not isinstance(hashes, dict) or not set(targets) <= hashes.keys() or names != {"manifest.json", *("src/"+p for p in hashes)}:
            raise ValueError("Unexpected ZIP members")
        sources={}
        for p,h in hashes.items():
            validated_path(p)
            if p.casefold() in (x.casefold() for x in sources):
                raise ValueError("Windows filename collision")
            b=z.read("src/"+p)
            if hashlib.sha256(b).hexdigest() != h:
                raise ValueError("Source hash mismatch")
            sources[p]=b
    return manifest,sources


def result_counts(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif b"\x00" in raw[:128]:
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    matches = RESULT_RX.findall(text)
    if len(matches) != 1:
        raise ValueError("Compiler log has no unique recognizable Result line")
    return tuple(map(int,matches[0]))


def executable_sha256(path):
    with path.open("rb") as binary:
        return hashlib.file_digest(binary, "sha256").hexdigest()


def numeric_version(ms, ls):
    return ".".join(str(x) for x in (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff))


def file_product_version(path):
    """Read the installed PE's language-neutral VS_FIXEDFILEINFO via Win32."""
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version.dll", winmode=0x800)  # System32 only
    size_of = version.GetFileVersionInfoSizeW
    size_of.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    size_of.restype = wintypes.DWORD
    get_info = version.GetFileVersionInfoW
    get_info.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    get_info.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR,
                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))
    query.restype = wintypes.BOOL
    size = size_of(str(path), None)
    if not 0 < size <= 4 * MAX_FILE:
        raise ValueError("MetaEditor version resource unavailable")
    data = ctypes.create_string_buffer(size)
    if not get_info(str(path), 0, size, data):
        raise ValueError("MetaEditor version resource unreadable")
    value, length = ctypes.c_void_p(), wintypes.UINT()
    info_type = ctypes.c_uint32 * 13
    if not query(data, "\\", ctypes.byref(value), ctypes.byref(length)):
        raise ValueError("MetaEditor fixed version unavailable")
    if not value.value or length.value != ctypes.sizeof(info_type):
        raise ValueError("Invalid MetaEditor fixed version length")
    if not ctypes.addressof(data) <= value.value <= ctypes.addressof(data)+size-length.value:
        raise ValueError("Invalid MetaEditor fixed version location")
    info = ctypes.cast(value, ctypes.POINTER(info_type)).contents
    if info[0] != 0xfeef04bd or not (info[2] or info[3]) or not (info[4] or info[5]):
        raise ValueError("Invalid MetaEditor fixed version resource")
    return numeric_version(info[2], info[3]), numeric_version(info[4], info[5])


def compiler_provenance(editor):
    digest = executable_sha256(editor)
    file_version, product_version = file_product_version(editor)
    if not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", v) for v in (file_version, product_version)):
        raise ValueError("Invalid MetaEditor numeric version")
    if executable_sha256(editor) != digest:
        raise ValueError("MetaEditor changed while reading version")
    return {"sha256": digest, "file_version": file_version, "product_version": product_version}


def compile_sources(archive, commit, archive_hash, editor, results, require_mode="diagnostic"):
    if sys.platform != "win32":
        raise RuntimeError("Native MetaEditor build requires Windows")
    editor=editor.resolve(strict=True)
    if not editor.is_file() or editor.name.lower() not in {"metaeditor.exe", "metaeditor64.exe"}:
        raise ValueError("Select the installed official MetaEditor executable")
    manifest,sources=read_package(archive,commit,archive_hash)
    if manifest["mode"] != require_mode:
        raise ValueError("Archive mode does not match requested compile")
    compiler = compiler_provenance(editor)
    results.mkdir(exist_ok=False)
    report={"commit":commit,"archive_sha256":archive_hash,"mode":manifest["mode"],
            "status":"compile_only_not_deployment","compiler":compiler,"targets":[]}
    with tempfile.TemporaryDirectory(prefix="recovery-ea-compile-") as d:
        root=Path(d)
        for p,b in sources.items():
            p=root/PurePosixPath(p)
            p.parent.mkdir(parents=True,exist_ok=True)
            with p.open("xb") as f:
                f.write(b)
        for name in manifest["targets"]:
            src = root/PurePosixPath(name)
            info={"source":name,"errors":None,"warnings":None,"status":"failed"}
            log, exe = src.with_suffix(".log"), src.with_suffix(".ex5")
            output_name = name.replace("/", "__")
            try:
                info["reason"] = "compiler_provenance_changed_or_unreadable"
                if executable_sha256(editor) != compiler["sha256"]:
                    raise ValueError("Compiler changed before invocation")
                info["reason"] = "compiler_process_failed"
                command = subprocess.list2cmdline([str(editor)]) + f' /compile:"{src}" /log'
                proc=subprocess.run(command, executable=str(editor),
                                    cwd=root,timeout=180,check=False)
                info["process_exit_code"]=proc.returncode
                info["reason"] = "compiler_provenance_changed_or_unreadable"
                if executable_sha256(editor) != compiler["sha256"]:
                    raise ValueError("Compiler changed during invocation")
                info["reason"] = "unverified_compiler_output"
                if not log.is_file():
                    raise ValueError("Missing fresh compilation log")
                raw=log.read_bytes()
                (results/(output_name+".log")).write_bytes(raw)
                info["errors"],info["warnings"]=result_counts(raw)
                if info["errors"] != 0 or not exe.is_file() or exe.stat().st_size == 0:
                    raise ValueError("Compilation did not produce an EX5 without errors")
                artifact = exe.read_bytes()
                (results/(output_name+".ex5")).write_bytes(artifact)
                info["ex5_sha256"]=hashlib.sha256(artifact).hexdigest()
                info["status"]="compiled"
                del info["reason"]
            except subprocess.TimeoutExpired:
                info["reason"] = "compiler_timeout"
            except (ValueError,OSError,subprocess.SubprocessError):
                # Native errors may embed personal or machine paths. Keep only
                # the stable reason above; raw compiler logs are separate files.
                pass
            report["targets"].append(info)
    (results/"compile-report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    s=p.add_subparsers(dest="command",required=True)
    pack=s.add_parser("package")
    pack.add_argument("--repo",type=Path,required=True)
    pack.add_argument("--commit",required=True)
    pack.add_argument("--out",type=Path,required=True)
    pack.add_argument("--trader",help="Opt-in candidate entry point relative to design/recovery-ea")
    comp=s.add_parser("compile")
    for arg in ("archive","editor","results"):
        comp.add_argument("--"+arg,type=Path,required=True)
    for arg in ("commit","archive-sha256"):
        comp.add_argument("--"+arg,required=True)
    comp.add_argument("--mode",choices=("diagnostic","candidate"),default="diagnostic")
    a=p.parse_args()
    if a.command=="package":
        m=package(a.repo,a.commit,a.out,a.trader)
        print(json.dumps({"commit":m["commit"],"mode":m["mode"],
                          "zip_sha256":hashlib.sha256(a.out.read_bytes()).hexdigest()}))
        return 0
    report=compile_sources(a.archive,a.commit,a.archive_sha256,a.editor,a.results,a.mode)
    print(json.dumps(report))
    return 0 if all(x["status"]=="compiled" for x in report["targets"]) else 1

if __name__ == "__main__":
    sys.exit(main())
