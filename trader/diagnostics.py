from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import time


_HISTORY_SUFFIX = ".history"
_MAX_COMPLETED = 20
_TELEGRAM_PART_BYTES = 45 * 1024 * 1024


class CycleFileHandler(logging.FileHandler):
    """Unbounded per-segment logging with cycle boundaries and atomic snapshots."""

    def __init__(self, path: str):
        self.legacy_path = Path(path).resolve()
        self.history_dir = self.legacy_path.with_name(self.legacy_path.name + _HISTORY_SUFFIX)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.history_dir / "index.json"
        self._index = self._load_index()
        self._cycle = 0
        self._kind = "between"
        segment = self._new_segment("between", 1)
        super().__init__(segment, mode="a", encoding="utf-8", delay=False)

    def _load_index(self) -> dict:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
        return {"next_segment": 1, "segments": []}

    def _save_index(self) -> None:
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)

    def _new_segment(self, kind: str, cycle: int, completed_cycles: int = 0) -> Path:
        ordinal = int(self._index.get("next_segment", 1))
        name = f"{ordinal:012d}-{kind}-cycle-{cycle:09d}.log"
        self._index["next_segment"] = ordinal + 1
        self._index.setdefault("segments", []).append(
            {"name": name, "kind": kind, "cycle": cycle, "ordinal": ordinal,
             "completed_cycles": completed_cycles}
        )
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
            marker = f"===== {'ТОРГОВАЯ ПОПЫТКА' if kind == 'cycle' else 'МЕЖДУ ПОПЫТКАМИ; СЛЕДУЮЩАЯ ПОПЫТКА'} {cycle} =====\n"
            self.stream.write(marker)
            self.flush()
        finally:
            self.release()

    def begin_cycle(self, cycle: int, completed_cycles: int = 0) -> None:
        self.switch("cycle", cycle, completed_cycles)
        # Pruning is deliberately performed only when the next cycle begins. Keep cycles
        # cycle-20..cycle-1 plus the current one, and their between-cycle diagnostics.
        cutoff = completed_cycles - _MAX_COMPLETED
        self.acquire()
        try:
            kept = []
            for item in self._index.get("segments", []):
                completed_anchor = int(item.get("completed_cycles", completed_cycles))
                if completed_anchor < cutoff:
                    (self.history_dir / str(item.get("name", ""))).unlink(missing_ok=True)
                else:
                    kept.append(item)
            self._index["segments"] = kept
            self._save_index()
        finally:
            self.release()

    def end_cycle(self, next_cycle: int, completed_cycles: int = 0) -> None:
        self.switch("between", next_cycle, completed_cycles)

    def _legacy_sources(self) -> list[Path]:
        rotated = []
        for candidate in self.legacy_path.parent.glob(self.legacy_path.name + ".*"):
            suffix = candidate.name[len(self.legacy_path.name) + 1:]
            if suffix.isdigit():
                rotated.append((int(suffix), candidate))
        # RotatingFileHandler uses the largest suffix for the oldest content.
        paths = [path for _, path in sorted(rotated, reverse=True)]
        if self.legacy_path.is_file():
            paths.append(self.legacy_path)
        return paths

    def snapshot(self, max_part_bytes: int = _TELEGRAM_PART_BYTES) -> list[str]:
        """Create one immutable chronological .log snapshot, or at most two parts."""
        self.acquire()
        try:
            self.flush()
            indexed = [self.history_dir / str(item["name"])
                       for item in sorted(self._index.get("segments", []),
                                          key=lambda value: int(value.get("ordinal", 0)))]
            sources = [path for path in self._legacy_sources() + indexed if path.is_file()]
            sizes = [path.stat().st_size for path in sources]
            total = sum(sizes)
            if total == 0:
                raise RuntimeError("Диагностическая история пока пуста")
            if total > max_part_bytes * 2:
                raise RuntimeError(
                    f"История {total} байт превышает предел двух Telegram-файлов "
                    f"({max_part_bytes * 2} байт); исходные журналы сохранены"
                )
            groups: list[list[Path]] = [[]]
            group_size = 0
            for source, size in zip(sources, sizes):
                if group_size and group_size + size > max_part_bytes and len(groups) == 1:
                    groups.append([])
                    group_size = 0
                groups[-1].append(source)
                group_size += size
            # A single long cycle cannot be split at a boundary; split its bytes only if required.
            if len(groups) == 1 and total > max_part_bytes:
                groups = [sources]
            outputs = self._write_snapshot(groups, max_part_bytes)
            stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
            renamed = []
            total_parts = len(outputs)
            for number, path in enumerate(outputs, 1):
                destination = path.with_name(
                    f"bot-diagnostics-{stamp}-part-{number}-of-{total_parts}.log"
                )
                path.replace(destination)
                renamed.append(destination)
            return [str(path) for path in renamed]
        finally:
            self.release()

    def _write_snapshot(self, groups: list[list[Path]], limit: int) -> list[Path]:
        outputs: list[Path] = []
        part = 1
        current = None
        current_size = 0
        try:
            for group in groups:
                for source in group:
                    with source.open("rb") as incoming:
                        while True:
                            if current is None:
                                fd, name = tempfile.mkstemp(
                                    prefix="telegram-diagnostics-", suffix=f"-part-{part}.log",
                                    dir=self.history_dir,
                                )
                                current = open(fd, "wb", closefd=True)
                                outputs.append(Path(name))
                            allowance = limit - current_size
                            chunk = incoming.read(min(256 * 1024, allowance))
                            if not chunk:
                                break
                            current.write(chunk)
                            current_size += len(chunk)
                            if current_size == limit:
                                current.close()
                                current = None
                                current_size = 0
                                part += 1
                    # Prefer a source/cycle boundary when another source would overflow.
                if current is not None and group is not groups[-1]:
                    current.close()
                    current = None
                    current_size = 0
                    part += 1
            if current is not None:
                current.close()
            if len(outputs) > 2:
                raise RuntimeError("Диагностическую историю невозможно разделить максимум на две части")
            return outputs
        except Exception:
            if current is not None and not current.closed:
                current.close()
            for path in outputs:
                path.unlink(missing_ok=True)
            raise


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
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def begin_diagnostic_cycle(path: str, cycle: int, completed_cycles: int = 0) -> None:
    handler = _handler(path)
    if handler:
        handler.begin_cycle(cycle, completed_cycles)


def end_diagnostic_cycle(path: str, next_cycle: int, completed_cycles: int = 0) -> None:
    handler = _handler(path)
    if handler:
        handler.end_cycle(next_cycle, completed_cycles)


def snapshot_diagnostics(path: str, max_part_bytes: int = _TELEGRAM_PART_BYTES) -> list[str]:
    handler = _handler(path)
    if handler is None:
        raise RuntimeError("Диагностический обработчик не настроен")
    return handler.snapshot(max_part_bytes)
