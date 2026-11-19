"""Sequential bounded MOCK lifecycle. No live startup or autonomous deployment."""
import os


class MockService:
    def __init__(self,pipeline):
        pipeline.transport._test_lock()
        self.p=pipeline;self.controller=pipeline.controller;self._file=None;self.closed=False;self._owner_token=object()
        with self.controller.lock:
            self._acquire(pipeline)

    def _acquire(self,pipeline):
        if getattr(self.controller,'_service_owned',False):raise RuntimeError('Controller already has a service owner')
        if self.controller.database!=':memory:':
            path=os.path.realpath(self.controller.database)+'.owner.lock'
            self._file=open(path,'a+b')
            try:
                if os.name=='nt':
                    import msvcrt
                    self._file.seek(0);self._file.write(b'0');self._file.flush();self._file.seek(0)
                    msvcrt.locking(self._file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except Exception:
                self._file.close();self._file=None
                raise RuntimeError('Another process owns this controller database')
        try:
            with self.controller.lock:
                self.controller.reload()
                self.controller.engine().validate_distance_policy()
                if any(any(b.get(k)!=v for k,v in pipeline.scope.items())
                       for b in self.controller.data['bindings'].values()):
                    raise ValueError('Reloaded binding belongs to another account/symbol scope')
                if self.controller.data.get('strict_stop_evidence') and pipeline.stop_evidence is None:
                    raise ValueError('Persisted strict stop evidence cannot be disabled')
                if pipeline.stop_evidence is not None and not self.controller.data.get('strict_stop_evidence'):
                    self.controller.data['strict_stop_evidence']=True
                    self.controller._save()
                self.controller._service_owned=True
                self.controller._service_owner_token=self._owner_token
                if pipeline.stop_evidence is not None:
                    pipeline.stop_evidence.bind_owner(self._owner_token)
                self.startup_report=self.reconcile_startup()
        except Exception:
            self.close()
            raise

    def reconcile_startup(self):
        """Evidence-only startup pass. No order submission or policy guesses."""
        from .maintenance import Maintenance
        with self.controller.lock:
            if self.closed:raise RuntimeError('Service closed')
            self.controller._startup_ready=False
            keys=[r[0] for r in self.p.executor.db.execute(
                "SELECT id FROM recovery_outbox WHERE state IN ('SENT','UNKNOWN','ACKNOWLEDGED')")]
            for key in keys:
                self.p.executor.reconcile(key,refresh=True)
            report=Maintenance(self.p).inspect_startup()
            self.startup_report=report
            self.controller._startup_ready=report['safe_to_resume']
            return report

    def step(self,bid,ask,*,now=None):
        if self.closed:raise RuntimeError('Service closed')
        self.p.transport._test_lock()
        # API coordinator uses this same lock: no halfway cancellation/quote cycle.
        with self.controller.lock:
            if self.closed:raise RuntimeError('Service closed')
            if not self.controller._startup_ready:
                raise RuntimeError('Startup reconciliation unresolved; no automatic sends')
            self.p.poll_history(now)
            self.p.protect_partial()
            from .maintenance import Maintenance
            for ticket,binding in list(self.controller.data['bindings'].items()):
                if binding['phase']=='partial_entry' and binding['range_id'] not in self.controller.data['blocked']:
                    Maintenance(self.p).cancel_partial_remainder(int(ticket),'accept-partial:'+ticket)
            self.p.protect_quote(bid,ask)
            self.p.sync_pending()
            return self.controller.state()

    def run_steps(self,quotes):
        """Explicit finite iterable of test quotes, never starts a background daemon."""
        results=[]
        for bid,ask in quotes:results.append(self.step(bid,ask))
        return results

    def close(self):
        if self.closed:return
        with self.controller.lock:
            if self.closed:return
            self._release()

    def _release(self):
        self.closed=True
        if getattr(self.controller,"_service_owner_token",None) is self._owner_token:
            self.controller._service_owned=False
            self.controller._startup_ready=False
            self.controller._service_owner_token=None
        if self._file:
            if os.name=='nt':
                import msvcrt
                self._file.seek(0);msvcrt.locking(self._file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(),fcntl.LOCK_UN)
            self._file.close();self._file=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
