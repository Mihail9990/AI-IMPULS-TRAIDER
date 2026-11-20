"""Pure startup inputs for the read-only RECOVERY roles; never starts MT5."""
from dataclasses import dataclass
import hashlib
import re
import secrets
from uuid import UUID

_HEX_SEED = re.compile(r"[0-9a-f]{64}")
_MAX_SIGNED_LONG = 2**63 - 1


def _scope(value):
    if not isinstance(value, str):
        raise ValueError("expected canonical profile scope UUID")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("expected canonical profile scope UUID") from None
    if str(parsed) != value:
        raise ValueError("expected canonical profile scope UUID")
    return value


def _text(value, field):
    if (not isinstance(value, str) or not value
            or any(ord(c) < 32 or ord(c) in (127, 0xfffd) or 0xd800 <= ord(c) <= 0xdfff
                   for c in value)
            or len(value.encode("utf-16-le")) > 200):
        raise ValueError(f"invalid {field}")
    return value


def _positive(value, field):
    if type(value) is not int or not 0 < value <= _MAX_SIGNED_LONG:
        raise ValueError(f"invalid {field}")
    return value


def _id_hex(category, seed):
    wire = f"RECOVERY_LIVE_SESSION_V1\t{category}\t{seed}"
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()[:32]


def executor_epoch(profile_scope: str, instance_seed: str) -> str:
    """Match RecoveryLiveUuid('boot', ...) then RecoveryPanelBootEpoch."""
    scope = _scope(profile_scope)
    if not isinstance(instance_seed, str) or not _HEX_SEED.fullmatch(instance_seed):
        raise ValueError("expected a fresh launch seed of 64 lowercase hex digits")
    return _id_hex("boot", f"{scope}:{instance_seed}")


@dataclass(frozen=True)
class RoleLaunch:
    profile_scope: str
    instance_seed: str
    expected_login: int
    expected_server: str
    expected_symbol: str
    strategy_magic: int

    def __post_init__(self):
        _scope(self.profile_scope)
        _positive(self.expected_login, "login")
        _positive(self.strategy_magic, "magic")
        _text(self.expected_server, "server")
        _text(self.expected_symbol, "symbol")
        if len({self.executor_epoch, self.verifier_epoch, self.watcher_epoch}) != 3:
            raise ValueError("role IDs collided; prepare a new plan")

    @property
    def executor_epoch(self):
        return executor_epoch(self.profile_scope, self.instance_seed)

    @property
    def verifier_epoch(self):
        return _id_hex("verifier", f"{self.profile_scope}:{self.instance_seed}")

    @property
    def watcher_epoch(self):
        return _id_hex("watcher", f"{self.profile_scope}:{self.instance_seed}")

    def _common_inputs(self):
        return {"ExpectedLogin": self.expected_login, "ExpectedServer": self.expected_server,
                "ExpectedSymbol": self.expected_symbol, "ExecutorEpoch": self.executor_epoch}

    def verifier_inputs(self):
        return {**self._common_inputs(), "StrategyMagic": self.strategy_magic,
                "VerifierEpoch": self.verifier_epoch}

    def watcher_inputs(self):
        return {**self._common_inputs(), "WatcherEpoch": self.watcher_epoch}

    def owner_binding(self, *, owner_chart, verifier_chart, watcher_chart):
        """Return argument names for a future caller; never starts that caller."""
        charts = [_positive(v, "chart id") for v in (owner_chart, verifier_chart, watcher_chart)]
        if len(set(charts)) != 3:
            raise ValueError("the three roles must be attached to different charts")
        return {"profile_scope": self.profile_scope, "instance_seed": self.instance_seed,
                "verifier_epoch": self.verifier_epoch, "verifier_chart": verifier_chart,
                "watcher_epoch": self.watcher_epoch, "watcher_chart": watcher_chart}


def prepare_role_launch(profile_scope, *, login, server, symbol, magic) -> RoleLaunch:
    """Always start from a fresh seed; no files, time testing, or restart state."""
    return RoleLaunch(profile_scope, secrets.token_hex(32), login, server, symbol, magic)
