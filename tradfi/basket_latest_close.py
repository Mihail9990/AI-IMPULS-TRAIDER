"""Prepared v2 range controller; runtime wiring waits for remaining TP/UI rules.

Uses the actual basket controller's ledger, ownership, OCO and intent gateway.
No broker calls outside that gateway. Never converts existing v1 active cycles.
"""
from .basket import BasketController
from .basket_math import last_closed_sl_price,recovery_range_after_sl

RANGE_POLICY='last_actual_close_v2'

class LatestCloseBasketController(BasketController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        c=self.state.get('cycle')
        if c and c.get('range_policy')!=RANGE_POLICY:
            self.state['migration_block']='Активный цикл использует прежние диапазоны. Автоматического переноса нет; завершите сверку прежней версией.'
            self.state['phase']='MIGRATION_BLOCK';self.state['stop']=True
            self.save('latest_close_range_migration_block')
    def _new(self,snap):
        super()._new(snap)
        self.state['cycle']['range_policy']=RANGE_POLICY
        self.save('latest_close_range_policy')
    def _initial_position_recovery(self,snap,initial):
        # No reversal at the initial protective SL. Wait for actual closing proof.
        return
    def _recovery_levels(self,initial):
        c=self.state['cycle']
        if c['expanded'] is None:
            price=last_closed_sl_price([initial])
            c['expanded']=recovery_range_after_sl(initial['side'],initial['entry'],price,c['cfg']['EXPANSION_DISTANCE'],initial=True)
            c['range_anchor_exit']=price
            self.save('latest_close_initial_range_fixed')
        return c['expanded']
    def _after_stopped_group(self,snap,g,tickets):
        c=self.state['cycle']
        try:
            price=last_closed_sl_price(tickets)
            c['expanded']=recovery_range_after_sl(g['activated'],tickets[0]['entry'],price,c['cfg']['EXPANSION_DISTANCE'],initial=False)
        except (ValueError,ArithmeticError):
            self._halt(snap,'LAST_RECOVERY_EXIT_UNCONFIRMED_OR_AMBIGUOUS');return False
        c['range_anchor_exit']=price
        self.save('latest_close_recovery_range_fixed')
        return True
