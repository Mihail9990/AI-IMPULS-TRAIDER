from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


_HISTORY_SUFFIX = ".history"
_TELEGRAM_PART_BYTES = 45 * 1024 * 1024


class CycleFileHandler(logging.FileHandler):
    """Process-run diagnostic segments with durable, acknowledged snapshot boundaries."""

    def __init__(self, path: str):
        self.legacy_path = Path(path).resolve()
        self.history_dir = self.legacy_path.with_name(self.legacy_path.name + _HISTORY_SUFFIX)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.history_dir / "index.json"
        self._index_lock = threading.RLock()
        self._index = self._load_index()
        self._migrate_index_and_legacy()
        self.run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
        self._index["run_counter"] = int(self._index.get("run_counter", 0)) + 1
        self.run_number = self._index["run_counter"]
        self._cycle = 0
        self._kind = "process"
        segment = self._new_segment("process", 0)
        super().__init__(segment, mode="a", encoding="utf-8", delay=False)
        self.stream.write(
            f"===== ЗАПУСК ПРОЦЕССА №{self.run_number} id={self.run_id} "
            f"начало={time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        )
        self.flush()
        self._cleanup_delivered_previous_runs()

    def _load_index(self) -> dict:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
        return {"version": 2, "next_segment": 1, "run_counter": 0,
                "segments": [], "snapshots": []}

    def _save_index(self) -> None:
        with self._index_lock:
            temporary = self.index_path.with_name(
                f"{self.index_path.name}.tmp-{threading.get_ident()}"
            )
            temporary.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.index_path)

    def _migrate_index_and_legacy(self) -> None:
        self._index.setdefault("segments", [])
        self._index.setdefault("snapshots", [])
        self._index.setdefault("next_segment", 1)
        self._index.setdefault("run_counter", 0)
        for item in self._index["segments"]:
            item.setdefault("run_id", "legacy-index")
            item.setdefault("run_number", 0)
            item.setdefault("delivered_bytes", 0)
        known = {str(item.get("name")) for item in self._index["segments"]}
        legacy = []
        for candidate in self.legacy_path.parent.glob(self.legacy_path.name + ".*"):
            suffix = candidate.name[len(self.legacy_path.name) + 1:]
            if suffix.isdigit():
                legacy.append((int(suffix), candidate))
        if self.legacy_path.is_file():
            legacy.append((0, self.legacy_path))
        for _, source in sorted(legacy, reverse=True):
            marker = f"legacy-{source.name}"
            if marker in known:
                continue
            destination = self.history_dir / marker
            if not destination.exists():
                # Move rather than copy: no duplicate large log and no loss of old unacked data.
                os.replace(source, destination)
            self._index["segments"].append({
                "name": marker, "kind": "legacy", "cycle": 0,
                "ordinal": int(self._index["next_segment"]), "run_id": "legacy",
                "run_number": 0, "delivered_bytes": 0,
            })
            self._index["next_segment"] = int(self._index["next_segment"]) + 1
        self._index["version"] = 2
        self._save_index()

    def _new_segment(self, kind: str, cycle: int, completed_cycles: int = 0) -> Path:
        ordinal = int(self._index.get("next_segment", 1))
        name = f"{ordinal:012d}-run-{self.run_number:06d}-{kind}-cycle-{cycle:09d}.log"
        self._index["next_segment"] = ordinal + 1
        self._index["segments"].append({
            "name": name, "kind": kind, "cycle": cycle, "ordinal": ordinal,
            "completed_cycles": completed_cycles, "run_id": self.run_id,
            "run_number": self.run_number, "delivered_bytes": 0,
        })
        self._save_index()
        return self.history_dir / name

    def switch(self, kind: str, cycle: int, completed_cycles: int = 0) -> None:
        self.acquire()
        try:
            if self._kind == kind and self._cycle == cycle:
                return
            self.flush()
            if self.stream:
                self.stream.close()
            self.baseFilename = os.fspath(self._new_segment(kind, cycle, completed_cycles))
            self.stream = self._open()
            self._kind, self._cycle = kind, cycle
            self.stream.write(
                f"===== {'ТОРГОВАЯ ПОПЫТКА' if kind == 'cycle' else 'МЕЖДУ ПОПЫТКАМИ; СЛЕДУЮЩАЯ ПОПЫТКА'} {cycle} "
                f"(запуск №{self.run_number}, id={self.run_id}) =====\n"
            )
            self.flush()
        finally:
            self.release()

    def begin_cycle(self, cycle: int, completed_cycles: int = 0) -> None:
        self.switch("cycle", cycle, completed_cycles)

    def end_cycle(self, next_cycle: int, completed_cycles: int = 0) -> None:
        self.switch("between", next_cycle, completed_cycles)

    def _pending_segment_names(self) -> set[str]:
        return {str(r["name"]) for snap in self._index.get("snapshots", [])
                if snap.get("status") != "delivered" for r in snap.get("ranges", [])}

    def _cleanup_delivered_previous_runs(self) -> None:
        pending = self._pending_segment_names()
        kept = []
        for item in self._index["segments"]:
            path = self.history_dir / str(item["name"])
            size = path.stat().st_size if path.exists() else 0
            if (item.get("run_id") != self.run_id and size
                    and int(item.get("delivered_bytes", 0)) >= size
                    and item["name"] not in pending):
                path.unlink(missing_ok=True)
            else:
                kept.append(item)
        self._index["segments"] = kept
        self._save_index()

    def snapshot(self, max_part_bytes: int = _TELEGRAM_PART_BYTES) -> list[str]:
        """Capture exact offsets under the handler lock, copy outside it, persist the job."""
        if max_part_bytes <= 0:
            raise ValueError("max_part_bytes must be positive")
        self.acquire()
        try:
            self.flush()
            ranges = []
            for item in sorted(self._index["segments"], key=lambda v: int(v.get("ordinal", 0))):
                path = self.history_dir / str(item["name"])
                if not path.is_file():
                    continue
                end = path.stat().st_size
                # Every /sendlog repeats the full current process. Previous runs contribute only
                # their not-yet-acknowledged tail.
                start = 0 if item.get("run_id") == self.run_id else int(item.get("delivered_bytes", 0))
                if end > start:
                    ranges.append({"name": item["name"], "start": start, "end": end,
                                   "run_id": item.get("run_id"),
                                   "run_number": item.get("run_number", 0)})
        finally:
            self.release()
        if not ranges:
            raise RuntimeError("Диагностическая история пока пуста")

        snapshot_id = f"log-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        outputs: list[Path] = []
        current = None
        current_size = 0
        try:
            for boundary in ranges:
                source = self.history_dir / str(boundary["name"])
                header = (f"\n===== ДАННЫЕ ЗАПУСКА №{boundary['run_number']} id={boundary['run_id']} "
                          f"файл={boundary['name']} bytes={boundary['start']}..{boundary['end']} =====\n").encode()
                pieces = [header]
                with source.open("rb") as incoming:
                    incoming.seek(int(boundary["start"]))
                    remaining = int(boundary["end"]) - int(boundary["start"])
                    while remaining:
                        pieces.append(incoming.read(min(256 * 1024, remaining)))
                        remaining -= len(pieces[-1])
                for block in pieces:
                    offset = 0
                    while offset < len(block):
                        if current is None:
                            number = len(outputs) + 1
                            path = self.history_dir / f"{snapshot_id}-part-{number:04d}.log"
                            current = path.open("wb")
                            outputs.append(path)
                            current_size = 0
                        amount = min(max_part_bytes - current_size, len(block) - offset)
                        current.write(block[offset:offset + amount])
                        offset += amount
                        current_size += amount
                        if current_size == max_part_bytes:
                            current.close(); current = None
            if current is not None:
                current.close(); current = None
            total = len(outputs)
            renamed = []
            for number, path in enumerate(outputs, 1):
                dest = path.with_name(f"{snapshot_id}-part-{number}-of-{total}.log")
                path.replace(dest); renamed.append(dest)
            record = {"id": snapshot_id, "created_at": time.time(), "status": "pending",
                      "ranges": ranges, "parts": [
                          {"number": i, "path": str(path), "status": "pending"}
                          for i, path in enumerate(renamed, 1)]}
            with self._index_lock:
                self._index["snapshots"].append(record)
                self._save_index()
            return [str(path) for path in renamed]
        except Exception:
            if current is not None:
                current.close()
            for path in outputs:
                path.unlink(missing_ok=True)
            raise

    def pending_snapshots(self) -> list[dict]:
        return [json.loads(json.dumps(item)) for item in self._index.get("snapshots", [])
                if item.get("status") != "delivered"]

    def acknowledge(self, snapshot_id: str, part: int, status: str) -> None:
        """Persist a document acknowledgement before deleting any covered material."""
        with self._index_lock:
            snapshot = next((s for s in self._index.get("snapshots", [])
                             if s.get("id") == snapshot_id), None)
            if snapshot is None:
                return
            target = next((p for p in snapshot["parts"] if int(p["number"]) == int(part)), None)
            if target is None:
                return
            target["status"] = status
            if all(p.get("status") == "delivered" for p in snapshot["parts"]):
                snapshot["status"] = "delivered"
                for boundary in snapshot["ranges"]:
                    segment = next((s for s in self._index["segments"]
                                    if s["name"] == boundary["name"]), None)
                    if segment is not None:
                        segment["delivered_bytes"] = max(
                            int(segment.get("delivered_bytes", 0)), int(boundary["end"])
                        )
                # Snapshot files are disposable only after the acknowledgement metadata is fsynced
                # by atomic index replacement. Source segments stay until a later process launch.
                self._save_index()
                for item in snapshot["parts"]:
                    Path(item["path"]).unlink(missing_ok=True)
                return
            self._save_index()


def _handler(path: str) -> CycleFileHandler | None:
    destination = Path(path).resolve()
    return next((item for item in logging.getLogger().handlers
                 if isinstance(item, CycleFileHandler) and item.legacy_path == destination), None)


def configure_diagnostics(path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if _handler(path) is None:
        file_handler = CycleFileHandler(path)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if not any(type(handler) is logging.StreamHandler for handler in root.handlers):
        console = logging.StreamHandler(); console.setFormatter(formatter); root.addHandler(console)


def begin_diagnostic_cycle(path: str, cycle: int, completed_cycles: int = 0) -> None:
    handler = _handler(path)
    if handler: handler.begin_cycle(cycle, completed_cycles)


def end_diagnostic_cycle(path: str, next_cycle: int, completed_cycles: int = 0) -> None:
    handler = _handler(path)
    if handler: handler.end_cycle(next_cycle, completed_cycles)


def snapshot_diagnostics(path: str, max_part_bytes: int = _TELEGRAM_PART_BYTES) -> list[str]:
    handler = _handler(path)
    if handler is None: raise RuntimeError("Диагностический обработчик не настроен")
    return handler.snapshot(max_part_bytes)


def pending_diagnostic_snapshots(path: str) -> list[dict]:
    handler = _handler(path)
    return handler.pending_snapshots() if handler else []


def acknowledge_diagnostic_snapshot(path: str, snapshot_id: str, part: int, status: str) -> None:
    handler = _handler(path)
    if handler: handler.acknowledge(snapshot_id, part, status)
