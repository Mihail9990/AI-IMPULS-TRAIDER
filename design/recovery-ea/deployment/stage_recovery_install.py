"""Prepare a fresh local staging folder from verified RECOVERY build artifacts.

This is not an installer. It never starts MT5 or writes into a terminal data
directory, and it does not copy native logs, configuration, or credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile

from source_build import CANDIDATE, ROLE, MAX_FILE, check_candidate_results

STAGE_SCHEMA = 1
STAGE_STATUS = "staged_only_not_installed_or_activated"
STAGE_MANIFEST = "stage-manifest.json"
STAGE_FILES = {name: PurePosixPath(name).with_suffix(".ex5").name for name in CANDIDATE}
MAX_EX5 = 64 * MAX_FILE


def local_temp_parent():
    """There is deliberately no caller-supplied destination path."""
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    # On Windows avoid network/removable temp locations; these artifacts
    # stay on the local fixed volume. No installed terminal paths are read.
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        volumes = ctypes.WinDLL("kernel32.dll", winmode=0x800).GetDriveTypeW
        volumes.argtypes = (wintypes.LPCWSTR,)
        volumes.restype = wintypes.UINT
        if not parent.anchor or volumes(parent.anchor) != 3:  # DRIVE_FIXED
            raise ValueError("Staging requires a local fixed-drive temporary folder")
    return parent


def copy_verified_artifact(source, dest, digest):
    if source.is_symlink() or not source.is_file():
        raise ValueError("Verified EX5 is no longer a regular file")
    n = 0
    sha = hashlib.sha256()
    with source.open("rb") as in_file, dest.open("xb") as out_file:
        for block in iter(lambda: in_file.read(1 << 20), b""):
            n += len(block)
            if n > MAX_EX5:
                raise ValueError("EX5 exceeds staging size limit")
            sha.update(block)
            out_file.write(block)
    if n == 0 or sha.hexdigest() != digest:
        raise ValueError("EX5 changed after build verification")
    with dest.open("rb") as copied:
        if hashlib.file_digest(copied, "sha256").hexdigest() != digest:
            raise ValueError("Staged EX5 read-back hash mismatch")
    return n


def prepare_stage(archive, commit, archive_hash, results):
    """Build a new exact staging set; validate before writing anything there.

    Returns the new path and a SHA-256 for its provenance manifest. Build
    consistency is not a code signature or proof of live trading readiness.
    """
    report = check_candidate_results(archive, commit, archive_hash, results)
    stage = Path(tempfile.mkdtemp(prefix="recovery-ea-stage-", dir=local_temp_parent()))
    try:
        roles = []
        for name, item in zip(CANDIDATE, report["targets"], strict=True):
            filename = STAGE_FILES[name]
            count = copy_verified_artifact(results / item["ex5"], stage / filename, item["ex5_sha256"])
            roles.append({
                "role": ROLE[name],
                "file": filename,
                "bytes": count,
                "sha256": item["ex5_sha256"],
                "source": name,
                "source_sha256": item["source_sha256"],
                "compile_log_sha256": item["log_sha256"],
                "compiler_errors": item["errors"],
                "compiler_warnings": item["warnings"],
            })
        manifest = {
            "schema": STAGE_SCHEMA,
            "status": STAGE_STATUS,
            "commit": commit,
            "source_archive_sha256": archive_hash,
            "compiler": {k: report["compiler"][k] for k in ("sha256", "file_version", "product_version")},
            "roles": roles,
        }
        wire = (json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("ascii")
        manifest_path = stage / STAGE_MANIFEST
        with manifest_path.open("xb") as out:
            out.write(wire)
        if (manifest_path.read_bytes() != wire or
            {p.name for p in stage.iterdir()} != {STAGE_MANIFEST, *STAGE_FILES.values()}):
            raise ValueError("Staging folder did not verify after writing")
        return stage, hashlib.sha256(wire).hexdigest()
    except BaseException:
        # Only this function's new directory; input files are never changed.
        shutil.rmtree(stage)
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--archive-sha256", required=True)
    p.add_argument("--results", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        stage, digest = prepare_stage(args.archive, args.commit, args.archive_sha256, args.results)
    except (ValueError, OSError) as exc:
        p.exit(1, f"Staging failed: {exc}\n")
    print(json.dumps({"status": STAGE_STATUS, "stage": str(stage), "manifest_sha256": digest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
