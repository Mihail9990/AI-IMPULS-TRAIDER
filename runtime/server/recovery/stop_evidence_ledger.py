"""Durable evidence binding for STRICT TEST lifecycle, not an authenticated EA bridge.

Only the local test driver supplies bytes. No filesystem polling, credentials or
live activation. Binary decimal mismatches are rejected rather than rounded.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4
from .ea_stop_evidence import ExitBinding, parse_stop_evidence, StopEvidenceUnavailable


class StopEvidenceLedger:
    def __init__(self, controller, scope):
        self.controller=controller
        self.scope=dict(scope)
        self._owner_token=None

    def _require_owner(self):
        if (self._owner_token is None or
                not getattr(self.controller,'_service_owned',False) or
                getattr(self.controller,'_service_owner_token',None) is not self._owner_token or
                self.controller.data.get('strict_stop_evidence') is not True):
            raise RuntimeError('Active strict-evidence service ownership required')

    def bind_owner(self, token):
        """Called by owner lifecycle only, after acquiring process/database lock."""
        with self.controller.lock:
            if (token is None or not getattr(self.controller,'_service_owned',False) or
                    getattr(self.controller,'_service_owner_token',None) is not token or
                    self.controller.data.get('strict_stop_evidence') is not True):
                raise RuntimeError('Cannot bind evidence ledger without service ownership')
            self._owner_token=token

    def _binding(self, data):
        return ExitBinding(**dict(data,volume=Decimal(data['volume'])))

    def request(self, deal, binding):
        """Called only after pipeline resolves a confirmed fake-history OUT binding."""
        with self.controller.lock:
            self._require_owner()
            if any(binding.get(k)!=v for k,v in self.scope.items()):
                raise StopEvidenceUnavailable('Evidence scope mismatch')
            if deal.position_id!=binding['position_identifier'] or deal.symbol!=self.scope['symbol']:
                raise StopEvidenceUnavailable('Evidence position/symbol mismatch')
            data=dict(account=self.scope['account'],server=self.scope['server'],
                deal=deal.ticket,order=deal.order,position=deal.position_id,
                symbol=deal.symbol,exit_side='SELL' if binding['side']=='BUY' else 'BUY',
                time_msc=deal.time_msc,volume=str(deal.volume))
            old=self.controller.data.get('stop_evidence',{}).get(str(deal.ticket))
            if old:
                if {k:v for k,v in old['binding'].items() if k!='request_id'}!=data:
                    raise StopEvidenceUnavailable('Historical evidence binding changed')
                return self._binding(old['binding'])
            data['request_id']=uuid4().hex
            before=deepcopy(self.controller.data)
            try:
                self.controller.data.setdefault('stop_evidence',{})[str(deal.ticket)]={'binding':data,'payload':None}
                self.controller._save()
            except Exception:
                self.controller.data=before;raise
            return self._binding(data)

    def supply(self, deal_id, payload):
        """Local-only test input. Validates bytes; does NOT unblock a failed range."""
        with self.controller.lock:
            self._require_owner()
            row=self.controller.data.get('stop_evidence',{}).get(str(deal_id))
            if row is None:raise StopEvidenceUnavailable('No durable evidence request')
            level=parse_stop_evidence(payload,self._binding(row['binding']))
            encoded=payload.decode('utf-8')
            if row['payload'] is not None:
                if row['payload']!=encoded:raise StopEvidenceUnavailable('Evidence replacement forbidden')
                return level
            before=deepcopy(self.controller.data)
            try:
                row['payload']=encoded
                self.controller._save()
            except Exception:
                self.controller.data=before;raise
            return level

    def resolve(self,deal,binding):
        with self.controller.lock:
            self._require_owner()
            expected=self.request(deal,binding)
            row=self.controller.data['stop_evidence'][str(deal.ticket)]
            if row['payload'] is None:raise StopEvidenceUnavailable('EA stop evidence unavailable')
            return parse_stop_evidence(row['payload'].encode('utf-8'),expected)
