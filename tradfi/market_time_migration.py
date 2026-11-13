"""Explicit, flat-only market domain transition. No broker mutations or P/L edits."""
from copy import deepcopy
import json

from .control import ActionRejected
from .engine import TERMINAL
from .raw_market_time import closed_pair
from .storage import DurabilityError
from .time_profile import RAW_MARKET_MODE

AUTHORITY = 'panel_explicit_v1'


def _reject():
    raise ActionRejected('Смена времени недоступна: завершите цикл и сверку всех операций; состояние сохранено.',
                         public_message='Смена времени недоступна: завершите цикл и сверку всех операций; состояние сохранено.')


def _encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _eligible(state, active):
    if (state.get('cycle') is not None or state.get('migration_block')
            or state.get('phase') not in {'WAIT_START', 'WAIT_CANDLE', 'PAUSED', 'IDLE'}
            or any(c.get('status') not in TERMINAL for c in state.get('commands', {}).values())):
        _reject()
    for rid, receipt in state.get('control_receipts', {}).items():
        if rid == active and receipt.get('status') == 'UNKNOWN':
            continue
        if receipt.get('status') not in {'APPLIED', 'REJECTED'}:
            _reject()



def _pristine_unbound(state):
    """No history or financial ownership may be inferred for an unbound account."""
    from .settings import number
    if state.get('account') is not None or any(state.get(k) for k in (
            'cycle', 'commands', 'completed', 'seen_deals', 'recovery_writeoffs')):
        return False
    try:
        return all(number(value) == 0 for key, value in state.items()
                   if 'carry' in key or key in {'recovery', 'realized', 'recovery_waived'})
    except (ValueError, TypeError, ArithmeticError):
        return False


def migration_availability(controller, snapshot=None):
    """Cached UI hint only. Never reads API/SQLite or authorizes a transition."""
    broker = getattr(controller, 'broker', None)
    config = getattr(broker, 'config', None)
    if (not isinstance(config, dict) or config.get('backend') != 'mt5_direct_v1'
            or getattr(controller, 'store', None) is None):
        return False, 'Переключение доступно только для прямого подключения MT5.'
    if getattr(broker, 'market_time_mode', None) == RAW_MARKET_MODE:
        return False, 'Режим времени MT5 уже включён.'
    try:
        _eligible(controller.state, None)
    except ActionRejected:
        return False, 'Завершите цикл и дождитесь сверки всех операций.'
    if snapshot is not None:
        if (snapshot.get('connected') is not True or snapshot.get('observations_usable') is not True
                or (snapshot.get('account') != controller.state.get('account')
                    and not _pristine_unbound(controller.state))
                or snapshot.get('orders') or snapshot.get('positions')):
            return False, 'Нужен доступный снимок брокера без позиций и ордеров.'
    return True, 'При переключении будут заново проверены счёт, операции и данные MT5.'

def _proofs(state, audit, rows):
    # collect() validates durable journal identity and ownership against the
    # entire retained controller state. Missing/unresolved obligations never
    # become acceptable just because a completed-cycle label exists.
    if (not audit.get('source_status') or any(v != 'RETURNED' for v in audit['source_status'].values())
            or audit.get('foreign_orders') or audit.get('foreign_positions')
            or audit.get('current_orders') or audit.get('current_positions')):
        _reject()
    allowed = {'FILLED': {'CLOSED_EVIDENCE'},
               'DONE': {'CANCEL_TERMINAL_READBACK', 'CLOSE_TERMINAL_READBACK'},
               'CANCELLED': {'TERMINAL_UNFILLED_EVIDENCE', 'NO_SEND_EVIDENCE'},
               'REJECTED': {'TERMINAL_UNFILLED_EVIDENCE', 'REJECTED_RESPONSE', 'NO_SEND_EVIDENCE'},
               'OBSOLETE': {'NO_SEND_EVIDENCE'}}
    proofs = audit.get('obligations', {})
    for cid, cmd in state.get('commands', {}).items():
        obligation = proofs.get(cid, {})
        status = obligation.get('status')
        if (obligation.get('manual_archive_ledger_verified') is True
                and status == 'EXTERNAL_EXIT' and cmd.get('kind') in {'STOP', 'MARKET'}
                and cmd['status'] in {'FILLED', 'OBSOLETE'}):
            continue
        # An obsolete never-sent intent legitimately has no journal entry.
        if cmd['status'] == 'OBSOLETE' and cmd.get('never_sent') is True and cid not in proofs:
            continue
        closed_modify = (cmd.get('kind') == 'MODIFY' and cmd['status'] == 'DONE'
                         and any(p.get('position_id') == cmd.get('position_id')
                                 and (p.get('status') == 'CLOSED_EVIDENCE'
                                      or p.get('manual_archive_ledger_verified') is True)
                                 for p in proofs.values()))
        if (closed_modify and rows.get(cid, {}).get('result', {}).get('status') == 'DONE'
                and status not in {'MISSING_DURABLE_JOURNAL', 'API_EVIDENCE_UNAVAILABLE'}):
            continue
        if status not in allowed.get(cmd['status'], set()):
            _reject()
        if status == 'NO_SEND_EVIDENCE' and cmd.get('never_sent') is not True:
            _reject()
    if set(proofs) - set(state.get('commands', {})):
        _reject()


class _TargetedArchiveAPI:
    """collect adapter: archived proofs use cached facts and exact IDs only.

    Discovery ranges are intentionally skipped, not claimed as returned broker
    history. Their synthetic source labels are removed before validation.
    """
    def __init__(self, api): self.api = api
    def __getattr__(self, name): return getattr(self.api, name)
    def history_orders_get(self, *args, **kwargs):
        return self.api.history_orders_get(**kwargs) if 'ticket' in kwargs else ()
    def history_deals_get(self, *args, **kwargs):
        return self.api.history_deals_get(**kwargs) if 'position' in kwargs else ()


def _include_archived_proofs(state, audit, broker):
    from .evidence import collect
    result = deepcopy(audit)
    completed = {item['id'] for item in state.get('completed', [])}
    missing = {cmd.get('cycle_id') for cid, cmd in state.get('commands', {}).items()
               if cid not in audit.get('obligations', {})
               and not (cmd.get('status') == 'OBSOLETE' and cmd.get('never_sent') is True)}
    if not missing:
        return result
    if state.get('scope_contract') != 'cycle_scope_v1' or not missing <= completed:
        _reject()
    for cycle_id in sorted(missing):
        # Only the ephemeral evidence view selects the archived cycle. All
        # durable commands and cycle ownership checks remain intact.
        view = deepcopy(state)
        summaries = [item for item in state['completed'] if item.get('id') == cycle_id]
        if len(summaries) != 1:
            _reject()
        manual_archive = summaries[0].get('completion') in {'MANUAL_RESET', 'MANUAL_RESET_RETAINED'}
        view['cycle'] = dict(id=cycle_id, manual=manual_archive)
        proof = collect(_TargetedArchiveAPI(broker.api), rows=broker.rows, state=view,
                        account=broker.account, symbol=broker.symbol, magic=broker.magic,
                        now=broker.clock(), cached=broker.facts,
                        check_context=lambda: broker._context(stage='market_time_archive_evidence'))
        proof['source_status'].pop('order_discovery', None)
        proof['source_status'].pop('deal_discovery', None)
        if (any(v != 'RETURNED' for v in proof['source_status'].values())
                or proof['current_orders'] or proof['current_positions']
                or proof['foreign_orders'] or proof['foreign_positions']):
            _reject()
        for cid, obligation in proof['obligations'].items():
            manual_closed = (manual_archive and obligation.get('status') == 'EXTERNAL_EXIT'
                             and obligation.get('reset_closed') is True
                             and broker.rows.get(cid, {}).get('send_started') is True
                             and broker.rows.get(cid, {}).get('result', {}).get('status') in {'ACCEPTED', 'DONE'})
            if obligation.get('status') != 'CLOSED_EVIDENCE' and not manual_closed:
                continue
            if state.get('ledger_contract') != 'deal_ledger_v1':
                _reject()
            pid = obligation.get('position_id')
            exits = {key: raw for key, raw in proof['raw_facts']['deal'].items()
                     if str(raw.get('position_id')) == pid and raw.get('entry') == broker.api.DEAL_ENTRY_OUT}
            records = [json.loads(row[0]) for row in broker.store.db.execute(
                'SELECT payload FROM consumed_deals WHERE backend=? AND account=?',
                (state['backend'], state['account']))]
            retained = {r['ticket']: r for r in records if r.get('position_id') == pid}
            if (not exits or set(retained) != set(exits)
                    or any(r.get('cycle_id') != cycle_id or r.get('raw') != exits[key]
                           for key, r in retained.items())):
                _reject()
            if manual_closed:
                if set(obligation.get('reset_closing_deals', [])) != set(exits):
                    _reject()
                # This annotation proves archived manual settlement only. Do not
                # relabel EXTERNAL_EXIT as automatic strategy closure evidence.
                obligation['manual_archive_ledger_verified'] = True
        result['source_status'].update({str(cycle_id) + ':' + k: v for k, v in proof['source_status'].items()})
        for cid, obligation in proof['obligations'].items():
            if cid in result['obligations'] and result['obligations'][cid] != obligation:
                _reject()
            result['obligations'][cid] = obligation
    return result

def migrate_market_time(controller):
    """Commit binding, boundary and domain metadata together; return activation data.

    Must run on the serialized controller owner. Caller activates its broker's
    in-memory raw reader only AFTER this succeeds. Repeated completed migrations
    are no-ops, including after a process crash before receipt completion.
    """
    store, broker = controller.store, controller.broker
    expected = deepcopy(controller._durable_state)
    state = deepcopy(controller.state)
    if expected != state or expected != store.load():
        _reject()
    row = store.db.execute('SELECT payload FROM market_time_binding WHERE id=1').fetchone()
    if row is None:
        _reject()
    binding = json.loads(row[0])
    config = broker.config
    if any(binding.get(k) != config.get(k) for k in ('expected_server', 'expected_login')):
        _reject()
    if binding.get('migration_authority') == AUTHORITY and binding.get('market_time_mode') == RAW_MARKET_MODE:
        store.verify_deal_ledger(state)
        return dict(state=state, binding=binding, boundary=state.get('last_candle'), already_migrated=True)
    if binding.get('market_time_mode', 'fixed_utc_v1') != 'fixed_utc_v1':
        _reject()
    _eligible(state, getattr(controller, '_active_control_id', None))
    initial_binding = _pristine_unbound(state)
    native_row = store.db.execute('SELECT payload FROM mt5_binding WHERE id=1').fetchone()
    if not native_row or json.loads(native_row[0]) != broker._binding():
        _reject()
    if state.get('account') is None and not initial_binding:
        _reject()
    try:
        # Unlike snapshot(), this read does not save facts or update send results.
        if initial_binding:
            # A prospective identity is used only in the read-only evidence view.
            # It becomes durable together with the market binding, never before.
            if broker.rows or any(broker.facts.values()):
                _reject()
            if store.db.execute('SELECT 1 FROM consumed_deals LIMIT 1').fetchone():
                _reject()
            broker._context(stage='market_time_initial_identity')
            from .evidence import collect
            audit = collect(broker.api, rows=broker.rows,
                            state=dict(state, account=broker.account), account=broker.account,
                            symbol=broker.symbol, magic=broker.magic, now=broker.clock(),
                            cached=broker.facts,
                            check_context=lambda: broker._context(stage='market_time_initial_evidence'))
        else:
            audit = broker.reconciliation_evidence()
        audit = _include_archived_proofs(state, audit, broker)
        _proofs(state, audit, broker.rows)
        if ((state.get('account') != broker.account and not initial_binding)
                or state.get('backend') != broker.backend_id):
            _reject()
        _, account, _, tick, _ = broker._context(stage='market_time_migration')
        if account.login != binding['expected_login'] or account.server != binding['expected_server']:
            _reject()
        rates = broker.api.copy_rates_from_pos(broker.symbol, broker.api.TIMEFRAME_M1, 0, 2)
        _, boundary = closed_pair(tick, rates)
        # Whole-account checks include unowned/manual objects on other symbols.
        orders, positions = broker.api.orders_get(), broker.api.positions_get()
        if orders is None or positions is None or len(orders) or len(positions):
            _reject()
        broker._context(stage='market_time_migration_complete')
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, TimeoutError):
        _reject()
    new_binding = dict(version=2, market_time_mode=RAW_MARKET_MODE,
                       market_time_offset_seconds=0, timestamp_semantics='BROKER_RAW',
                       expected_server=binding['expected_server'], expected_login=binding['expected_login'],
                       migration_authority=AUTHORITY)
    transition = dict(previous_binding=binding, previous_last_candle=state.get('last_candle'),
                      previous_market_time_domain=state.get('market_time_domain'),
                      binding=new_binding, boundary=boundary, timestamp_semantics='BROKER_RAW')
    updated = deepcopy(state)
    active_id = getattr(controller, '_active_control_id', None)
    if active_id is not None:
        receipt = updated.get('control_receipts', {}).get(active_id)
        if receipt is None or receipt.get('status') != 'UNKNOWN':
            _reject()
        receipt['status'] = 'APPLIED'
        transition['control_action_id'] = active_id
    if initial_binding:
        updated['account'] = broker.account
        transition['previous_account'] = None
    updated.update(stop=True, stop_mode='', phase='WAIT_START', last_candle=boundary,
                   market_time_domain=dict(mode=RAW_MARKET_MODE, timestamp_semantics='BROKER_RAW',
                                           activation_boundary=boundary, migration_authority=AUTHORITY))
    # UTC safety latches describe the previous clock, retained in transition history.
    transition['previous_time_block'] = updated.pop('time_block', None)
    db = store.db
    try:
        db.execute('BEGIN IMMEDIATE')
        if store.load() != expected or db.execute('SELECT payload FROM market_time_binding WHERE id=1').fetchone() != row:
            raise DurabilityError('Concurrent/stale market-time transition')
        if db.execute('SELECT payload FROM mt5_binding WHERE id=1').fetchone() != native_row:
            raise DurabilityError('Concurrent/stale native identity binding')
        store.verify_deal_ledger(expected)
        db.execute('CREATE TABLE IF NOT EXISTS market_time_transitions (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('INSERT INTO market_time_transitions(payload) VALUES(?)', (_encode(transition),))
        db.execute('UPDATE market_time_binding SET payload=? WHERE id=1', (_encode(new_binding),))
        if store.fault: store.fault('market_time_binding_written')
        db.execute('UPDATE snapshot SET payload=? WHERE id=1', (_encode(updated),))
        store.verify_deal_ledger(updated)
        db.execute('INSERT INTO audit(event,payload) VALUES(?,?)', ('market_time_migrated', _encode(transition)))
        if store.fault: store.fault('market_time_before_commit')
        db.execute('COMMIT')
    except BaseException as exc:
        if db.in_transaction: db.execute('ROLLBACK')
        if isinstance(exc, DurabilityError): raise
        raise DurabilityError('Atomic market-time migration failed; reload durable state') from exc
    controller.state = updated
    controller._durable_state = deepcopy(updated)
    return dict(state=deepcopy(updated), binding=new_binding, boundary=boundary, already_migrated=False)
