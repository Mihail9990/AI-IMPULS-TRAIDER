"""Approved lot table, integrated with Engine full-SL counter.

Initial entry is slot 1; next current slot is full_sl_count + 1. Support and
manual exits do not advance the counter. Already prearmed reverse orders keep
their original lot after manual/support exits by owner choice; only newly
created orders use the counter. Position 24 has no prearmed opposite entry.
"""
from decimal import Decimal


VOLUMES = tuple(Decimal(v) for v in (
    '.01','.01','.01','.01','.01',
    '.02','.02','.02',
    '.03','.03','.04','.04','.05','.05','.06','.06',
    '.07','.07','.08','.08','.09','.09','.10','.10',
))


def volume_for_slot(ordinal: int) -> Decimal:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= 24:
        raise ValueError('Explicit schedule slot must be an integer from 1 to 24; no automatic slot 25')
    return VOLUMES[ordinal-1]
