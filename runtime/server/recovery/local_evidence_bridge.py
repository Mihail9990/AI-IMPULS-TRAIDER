"""Local, read-only EA evidence mailbox. Directory ACLs are a deployment prerequisite.

No authenticity claim against another process able to write this directory.
The durable caller owns request IDs and checks broker history before/after use.
"""
from pathlib import Path
import os
import tempfile
from .ea_stop_evidence import ExitBinding, parse_stop_evidence, StopEvidenceUnavailable


class LocalEvidenceBridge:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or not self.directory.is_dir():
            raise ValueError('Existing absolute trusted RECOVERY directory required')
        if self.directory.resolve() != self.directory:
            raise ValueError('Symlink mailbox paths not accepted')

    def _paths(self, binding):
        # Reuse the strict wire validator for binding validation.
        fields = ['RECOVERY_DEAL_SL_V1', binding.request_id, str(binding.account), binding.server,
                  str(binding.deal), str(binding.order), str(binding.position), binding.symbol,
                  binding.exit_side, str(binding.time_msc), format(binding.volume, '.16f'),
                  '1.0000000000000000', 'END']
        parse_stop_evidence(('\t'.join(fields)+'\n').encode(), binding)
        return tuple(self.directory / (binding.request_id + suffix) for suffix in ('.request', '.response'))

    def publish(self, binding: ExitBinding):
        request, response = self._paths(binding)
        payload = '\t'.join(['RECOVERY_REQUEST_V1', binding.request_id, str(binding.account),
            binding.server, str(binding.deal), str(binding.order), str(binding.position),
            binding.symbol, binding.exit_side, str(binding.time_msc), format(binding.volume, '.16f'), 'END']) + '\r\n'
        data = payload.encode('utf-8')
        if len(data) > 4096: raise ValueError('Request too large')
        if request.is_symlink() or response.is_symlink(): raise ValueError('Symlink mailbox file')
        if request.exists():
            if self._read(request, 4096) != data: raise ValueError('Request ID reused with different binding')
            return request
        # Publish without overwriting an existing nonce: hard link exposes only complete bytes.
        fd, name = tempfile.mkstemp(prefix='.publish-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            try: os.link(name, request)
            except FileExistsError:
                if request.is_symlink() or self._read(request,4096) != data:
                    raise ValueError('Request collision')
        finally:
            os.unlink(name)
        return request

    def _read(self, path, limit):
        if path.is_symlink(): raise StopEvidenceUnavailable('Symlink mailbox file')
        with path.open('rb') as f:
            data = f.read(limit + 1)
        if len(data) > limit: raise StopEvidenceUnavailable('Oversized mailbox record')
        return data

    def collect(self, binding: ExitBinding):
        request, response = self._paths(binding)
        # Only consume a response to a request published by this durable binding.
        if not request.exists(): raise StopEvidenceUnavailable('Request not published')
        self.publish(binding)
        try: payload = self._read(response, 8192)
        except FileNotFoundError as exc: raise StopEvidenceUnavailable('EA response pending') from exc
        return parse_stop_evidence(payload, binding)
