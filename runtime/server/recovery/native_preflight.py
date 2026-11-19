"""Production MT5 request validation, not a live execution service.

Uses an already initialized, caller-owned terminal session. This module never
initializes/logs in, sends orders, selects symbols, or changes terminal settings.
A successful validate/check is NOT permission to trade or a readiness certificate.
The existing mock execution locks remain unchanged.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

from .engine import number
from .mt5_plan import Symbol, Quote, pending, protect, cancel, close_position
from .mt5_execution_gateway import map_native_request
from .real_mt5_adapter import RealMT5ReadOnlyAdapter
from .gateway_transport import GatewayTransport


@dataclass(frozen=True)
class TerminalBinding:
    account: int
    server: str
    symbol: str
    magics: frozenset

    def __post_init__(self):
        if type(self.account) is not int or self.account <= 0:
            raise ValueError('Explicit positive account required')
        if not isinstance(self.server, str) or not self.server.strip():
            raise ValueError('Explicit server required')
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError('Explicit symbol required')
        if not isinstance(self.magics, frozenset) or not self.magics or any(type(m) is not int or m <= 0 for m in self.magics):
            raise ValueError('Explicit immutable owned magic set required')


class NativeMT5Preflight:
    """Bound validator; deliberately exposes no usable order_send method.

    Never infer ownership from a comment or same-symbol position. Only explicit
    ticket + position identifier + owned magic + symbol can authorize validation.
    """
    _execution_policy = GatewayTransport._execution_policy

    def __init__(self, api, binding):
        if not isinstance(binding, TerminalBinding):
            raise TypeError('TerminalBinding required')
        self.api = api
        self.binding = binding
        self.session = RealMT5ReadOnlyAdapter(api, account=binding.account, server=binding.server)
        self._lock = RLock()
        self.initial_stop_guard = None

    def send(self, *args, **kwargs):
        raise RuntimeError('Production execution unavailable: live lifecycle/reconciliation is not integrated')

    def _session(self):
        account, terminal = self.session._verify_session()
        if (getattr(account, 'trade_allowed', None) is not True
                or getattr(account, 'trade_expert', None) is not True
                or getattr(terminal, 'trade_allowed', None) is not True
                or getattr(terminal, 'tradeapi_disabled', None) is not False):
            raise ValueError('Terminal/account algorithmic trading permission unavailable')
        return account, terminal

    def _scope(self, expected):
        b = self.binding
        if (expected.get('account') != b.account or expected.get('server') != b.server
                or expected.get('symbol') != b.symbol or type(expected.get('magic')) is not int
                or expected['magic'] not in b.magics):
            raise ValueError('Request scope outside bound account/server/symbol/magics')

    def _flat_cancel(self, request, expected):
        """Late, repeated guard for cancel-all's no-open-position user rule.

        Ordinary explicit close+cancel of one range deliberately does not request
        this condition. The final broker transition remains non-atomic.
        """
        if 'require_no_owned_positions' not in expected:
            return
        if expected['require_no_owned_positions'] is not True or request['action'] != 'TRADE_ACTION_REMOVE':
            raise ValueError('Flat-owned guard only valid for explicit range cancellation')
        rows = self.api.positions_get()
        if rows is None:
            raise ValueError('Cannot verify absence of owned positions')
        b = self.binding
        if any(p.symbol == b.symbol and p.magic in b.magics for p in rows):
            raise ValueError('Range cancellation disabled while owned positions are open')

    def _metadata(self, request):
        info = self.api.symbol_info(self.binding.symbol)
        tick = self.api.symbol_info_tick(self.binding.symbol)
        if info is None or tick is None or getattr(info, 'select', None) is not True:
            raise ValueError('Selected symbol metadata/quote required')
        mode = getattr(info, 'trade_mode', None)
        names = ('DISABLED', 'LONGONLY', 'SHORTONLY', 'CLOSEONLY', 'FULL')
        modes = {getattr(self.api, 'SYMBOL_TRADE_MODE_' + n): n for n in names}
        if type(mode) is not int or mode not in modes or modes[mode] == 'DISABLED':
            raise ValueError('Symbol trade mode unavailable or disabled')
        flags = getattr(info, 'order_mode', None)
        if type(flags) is not int or flags < 0:
            raise ValueError('Unknown symbol order permissions')
        action = request['action']
        if action == 'TRADE_ACTION_PENDING':
            side = request['type']
            if (modes[mode] == 'CLOSEONLY'
                    or (modes[mode] == 'LONGONLY' and side != 'ORDER_TYPE_BUY_STOP')
                    or (modes[mode] == 'SHORTONLY' and side != 'ORDER_TYPE_SELL_STOP')):
                raise ValueError('Entry direction not permitted by symbol')
            for flag in ('SYMBOL_ORDER_STOP', 'SYMBOL_ORDER_SL'):
                bit = getattr(self.api, flag)
                if flags & bit != bit:
                    raise ValueError('Symbol does not permit stop entry with SL')
        elif action == 'TRADE_ACTION_SLTP':
            bit = self.api.SYMBOL_ORDER_SL
            if flags & bit != bit:
                raise ValueError('Symbol does not permit SL modification')
        elif action == 'TRADE_ACTION_DEAL':
            bit = self.api.SYMBOL_ORDER_MARKET
            if flags & bit != bit:
                raise ValueError('Symbol does not permit market close')
        symbol = Symbol(self.binding.symbol, str(info.trade_tick_size), str(info.point),
                        info.trade_stops_level, info.trade_freeze_level,
                        str(info.volume_min), str(info.volume_max), str(info.volume_step))
        quote = Quote(str(tick.bid), str(tick.ask))
        symbol.validate(); quote.validate()
        return symbol, quote

    def validate(self, request, expected):
        """Return native request after fresh validation; no broker check/send.

        Widening requires an owner-bound proof against durable initial-stage state
        and fresh exact entry history. Caller scope alone never grants permission.
        """
        with self._lock:
            request, expected = deepcopy(request), deepcopy(expected)
            self._scope(expected)
            self._session()
            # Validate shape/enums before any ticket lookup. A missing market price
            # is only permitted when the symbol execution policy says Market.
            market, _ = self._execution_policy(request)
            native = map_native_request(self.api, request, market_execution=market)
            for field in ('symbol', 'magic'):
                if field in request and request[field] != expected[field]:
                    raise ValueError('Request scope mismatch: ' + field)
            action = request['action']
            symbol, quote = self._metadata(request)
            if action == 'TRADE_ACTION_PENDING':
                if request.get('symbol') != expected['symbol'] or request.get('magic') != expected['magic']:
                    raise ValueError('Pending scope required')
                if number(request['volume']) > Decimal('0.10'):
                    raise ValueError('RECOVERY volume exceeds 0.10 lot schedule')
                side = 'BUY' if request['type'] == 'ORDER_TYPE_BUY_STOP' else 'SELL'
                pending(dict(request, id='preflight', side=side), symbol, quote, expected['magic'])
                if number(request['tp']) != 0:
                    raise ValueError('RECOVERY has no automatic TP')
            elif action in ('TRADE_ACTION_SLTP', 'TRADE_ACTION_DEAL'):
                rows = self.api.positions_get(ticket=request['position'])
                if rows is None or len(rows) != 1:
                    raise ValueError('Position absent or ambiguous')
                p = self.session._normalize_position(rows[0])
                if action == 'TRADE_ACTION_SLTP':
                    p['stop_stage'] = 1  # preserve protection unless owner proves first fill correction
                    direction = Decimal(1) if p['side'] == 'BUY' else Decimal(-1)
                    if (number(request['sl']) - number(p['sl'])) * direction < 0:
                        guard = self.initial_stop_guard
                        if not callable(guard) or guard(request, expected, p) is not True:
                            raise ValueError('Initial correction proof required to widen original SL')
                        p['stop_stage'] = 0
                    checked = protect(p, request['sl'], symbol, quote,
                                      expected_ticket=request['position'], expected_identifier=expected['identifier'],
                                      expected_magic=expected['magic'])
                    if number(checked['tp']) != number(request['tp']):
                        raise ValueError('Existing TP must be preserved')
                else:
                    if request.get('symbol') != expected['symbol'] or request.get('magic') != expected['magic']:
                        raise ValueError('Close scope required')
                    checked = close_position(p, symbol, quote, expected_ticket=request['position'],
                                             expected_identifier=expected['identifier'], expected_magic=expected['magic'],
                                             filling=request['type_filling'])
                    if checked['type'] != request['type'] or number(checked['volume']) != number(request['volume']):
                        raise ValueError('Close position side/volume changed')
                    if not market and number(checked['price']) != number(request['price']):
                        raise ValueError('Close quote changed')
            elif action == 'TRADE_ACTION_REMOVE':
                rows = self.api.orders_get(ticket=request['order'])
                if rows is None or len(rows) != 1:
                    raise ValueError('Order absent or ambiguous')
                cancel(self.session._normalize_order(rows[0]), expected_ticket=request['order'],
                       expected_magic=expected['magic'], expected_symbol=expected['symbol'])
            self._flat_cancel(request, expected)
            self._session()  # reject account switches during metadata/position/order reads
            return native

    def check(self, request, expected):
        """Broker validation only. Successful retcode is not an execution receipt."""
        with self._lock:
            native = self.validate(request, expected)
            result = self.api.order_check(native)
            self._session()
            if result is None or result.retcode != 0:
                raise ValueError('MT5 order_check rejected or unavailable')
            return {'validated': True, 'live_trading': False, 'safe_to_trade': False,
                    'retcode': result.retcode, 'request': native}
