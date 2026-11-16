"""Read-only, allowlisted native chain capture. Output is test data, never an ownership journal."""
from decimal import Decimal
from .evidence import plain, ident, EvidenceConflict
from .settings import number


def capture_chain(broker, ticket):
    """Target existing order and position only; no discovery, checks, sends or state writes."""
    if broker.enable_trading or broker.store is not None:
        raise ValueError('Fixture capture requires a stateless read-only adapter')
    ticket=ident(ticket)
    def read(fn,**kwargs):
        broker._context(stage='fixture_read')
        result=fn(**kwargs)
        broker._context(stage='fixture_readback')
        if result is None: raise OSError('Fixture evidence unavailable')
        return plain(result)
    rows=read(broker.api.history_orders_get,ticket=int(ticket))
    if len(rows)!=1 or ident(rows[0]['ticket'])!=ticket or rows[0]['symbol']!=broker.symbol:
        raise EvidenceConflict('EXACT_EXISTING_ORDER_REQUIRED')
    entry=rows[0]; pid=ident(entry['position_id'])
    if entry['state'] not in {broker.api.ORDER_STATE_FILLED,broker.api.ORDER_STATE_CANCELED,broker.api.ORDER_STATE_EXPIRED}:
        raise EvidenceConflict('TERMINAL_ENTRY_REQUIRED')
    deals=read(broker.api.history_deals_get,position=int(pid))
    if not deals or any(ident(d['position_id'])!=pid or d['symbol']!=broker.symbol for d in deals):
        raise EvidenceConflict('EXACT_POSITION_CHAIN_REQUIRED')
    if len({ident(d['ticket']) for d in deals})!=len(deals):
        raise EvidenceConflict('DUPLICATE_DEAL')
    ins=[d for d in deals if d['entry']==broker.api.DEAL_ENTRY_IN]
    outs=[d for d in deals if d['entry']==broker.api.DEAL_ENTRY_OUT]
    if (not ins or not outs or len(ins)+len(outs)!=len(deals)
            or any(ident(d['order'])!=ticket for d in ins)
            or sum((number(d['volume']) for d in ins),Decimal(0))!=sum((number(d['volume']) for d in outs),Decimal(0))):
        raise EvidenceConflict('COMPLETE_SINGLE_ENTRY_CHAIN_REQUIRED')
    positions=read(broker.api.positions_get,symbol=broker.symbol)
    if any(ident(p['identifier'])==pid for p in positions):
        raise EvidenceConflict('POSITION_STILL_LIVE')
    orders={ticket:entry}
    for order_id in {ident(d['order']) for d in deals}-{ticket}:
        found=read(broker.api.history_orders_get,ticket=int(order_id))
        if (len(found)!=1 or ident(found[0]['ticket'])!=order_id
                or ident(found[0]['position_id'])!=pid or found[0]['symbol']!=broker.symbol):
            raise EvidenceConflict('EXIT_ORDER_CHAIN_REQUIRED')
        orders[order_id]=found[0]
    # Keep correlation, volume ratios, price deltas and PnL per normalized size.
    # Drop account/path/comments, real IDs, absolute prices, sizes and calendar dates.
    order_ids={key:1001+i for i,key in enumerate(sorted(orders,key=int))}
    deal_ids={ident(d['ticket']):8001+i for i,d in enumerate(sorted(deals,key=lambda d:int(d['ticket'])))}
    scale=Decimal('.01')/number(entry['volume_initial'])
    anchor=number(entry['price_open'])
    stamps=[number(r[k]) for r in list(orders.values())+deals
            for k in ('time_msc','time_setup_msc','time_done_msc') if r.get(k)]
    if not stamps or any(t<=0 for t in stamps): raise EvidenceConflict('INVALID_RAW_TIMES')
    origin=min(stamps)
    def project(row,kind):
        result={}
        for key in ('type','state','entry','reason'):
            if key in row: result[key]=int(row[key])
        result['ticket']=(order_ids if kind=='order' else deal_ids)[ident(row['ticket'])]
        result.update(symbol=broker.symbol,magic=25101102 if row.get('magic')==entry.get('magic') else 0,position_id=5001)
        if kind=='deal': result['order']=order_ids[ident(row['order'])]
        else: result['comment']='a'*24 if ident(row['ticket'])==ticket else 'fixture_exit'
        for key in ('volume','volume_initial','volume_current','profit','commission','swap','fee'):
            if key in row: result[key]=float(number(row[key])*scale)
        for key in ('price','price_open','sl','tp'):
            if key in row: result[key]=float(number(row[key])-anchor+4000) if number(row[key]) else 0.
        for key in ('time_msc','time_setup_msc','time_done_msc'):
            if key in row:
                stamp=int(number(row[key])-origin+1791800000000) if row[key] else 0
                result[key]=stamp
                result[key.removesuffix('_msc')]=stamp//1000
        return result
    return dict(schema='pseudonymized_native_chain_v1',not_an_ownership_journal=True,
        history_time_status='UNVERIFIED',calendar_times_shifted=True,financial_values_normalized=True,
        source='existing_native_order_position_deals',orders=[project(r,'order') for r in orders.values()],
        deals=[project(d,'deal') for d in deals],entry_order=order_ids[ticket],position_id=5001,
        purpose='Native field/correlation regression; not UTC/filter, ownership or live-execution certification')
