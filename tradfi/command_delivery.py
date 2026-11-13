"""Display-only delivery stages; never execution permission or durable command state."""


def trigger_delivery(controller):
    state = controller.state
    cycle_id = (state.get('cycle') or {}).get('id')
    counts = {}
    for cid, command in state.get('commands', {}).items():
        if (not cycle_id or command.get('cycle_id') != cycle_id
                or command.get('kind') != 'STOP' or command.get('edit_superseded')
                or command.get('status') not in {'READY', 'UNKNOWN', 'ACCEPTED'}):
            continue
        if getattr(controller, '_submitting_command_id', None) == cid:
            stage = 'SENDING'
        elif command.get('status') == 'READY':
            stage = 'QUEUED'
        else:
            # result() reads the local durable journal; no MT5 request is made.
            try:
                result = controller.broker.result(cid)
            except (OSError, TimeoutError, ValueError):
                result = {}
            if result.get('status') == 'ACCEPTED':
                stage = 'ACKNOWLEDGED_AWAITING_READBACK'
            elif result.get('status') == 'REJECTED':
                stage = 'REJECTED_AWAITING_RECONCILIATION'
            elif result.get('reason') == 'No order_send result':
                stage = 'NO_RESPONSE_RECONCILING'
            else:
                stage = 'OUTCOME_UNCONFIRMED'
        counts[stage] = counts.get(stage, 0) + 1
    return {'counts': counts}
