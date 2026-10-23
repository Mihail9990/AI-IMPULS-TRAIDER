from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation

D = Decimal


def number(value):
    try:
        result = D(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid decimal') from exc
    if not result.is_finite():
        raise ValueError('Non-finite decimal')
    return result


@dataclass(frozen=True)
class Settings:
    INITIAL_SIZE: D = D('0.09')
    BRANCH1_RANGE: D = D('3.00')
    BRANCH1_SL_DISTANCE: D = D('2.00')
    BASE_TP_DISTANCE: D = D('0.20')
    STEPPED_TP_ACTIVATION: D = D('0.70')
    STEPPED_TP_STEP: D = D('0.10')
    RANGE_AFTER_PARTIAL_TP: D = D('2.00')
    SL_DISTANCE_AFTER_PARTIAL_TP: D = D('1.50')
    RANGE_AFTER_SL: D = D('2.00')
    SL_DISTANCE_AFTER_SL: D = D('1.50')
    CYCLE_COOLDOWN_SECONDS: D = D('60')

    def dump(self):
        return {k: str(v) for k, v in asdict(self).items()}

    @classmethod
    def load(cls, data):
        if set(data) != set(cls.__dataclass_fields__):
            raise ValueError('Settings require every key exactly once; unknown keys forbidden')
        obj = cls(**{k: number(v) for k, v in data.items()})
        if any(v <= 0 for v in asdict(obj).values()):
            raise ValueError('All settings must be positive')
        if obj.CYCLE_COOLDOWN_SECONDS % 1:
            raise ValueError('Cooldown must be an integer number of seconds')
        return obj

    def template(self):
        return '/SETTINGS:\n' + '\n'.join(f'{k}={v}' for k, v in self.dump().items())


def parse_settings(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines or lines[0].upper() != '/SETTINGS:' or len(text) > 4096:
        raise ValueError('Use the complete /SETTINGS: block')
    data = {}
    for line in lines[1:]:
        if line.count('=') != 1:
            raise ValueError('Expected KEY=value')
        key, value = (x.strip() for x in line.split('='))
        key = key.upper()
        if key in data:
            raise ValueError('Duplicate setting: ' + key)
        data[key] = value
    return Settings.load(data)
