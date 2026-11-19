"""Offline readiness policy. This module cannot arm a terminal or submit trades."""
from uuid import UUID


def uuid_ok(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except ValueError:
        return False


def pairing_status(reports, *, terminal_id, context_id, challenge):
    """One installer-issued challenge binds two separately declared chart instances.

    A missing/replaced peer is not a command to close, cancel or reset anything.
    This result only describes readiness for new operations requiring both roles;
    account/order reconciliation and installer ACL checks remain separate.
    """
    if not all(uuid_ok(value) for value in (terminal_id, context_id, challenge)):
        return "invalid_probe"
    if not isinstance(reports, (tuple, list)):
        return "invalid_report"
    roles = {}
    for report in reports:
        if type(report) is not dict:
            return "invalid_report"
        if report.get("challenge") != challenge:
            continue  # previous probe, never evidence for this one
        role = report.get("role")
        if type(role) is not str or role not in ("trader", "verifier"):
            return "invalid_role"
        if role in roles:
            return "duplicate_role"
        if (report.get("terminal_id"), report.get("context_id")) != (terminal_id, context_id):
            return "scope_mismatch"
        if type(report.get("chart_id")) is not int or not 0 < report["chart_id"] < 2**63:
            return "invalid_chart"
        if not uuid_ok(report.get("instance_id")):
            return "invalid_instance"
        if report.get("can_submit_orders") is not (role == "trader"):
            return "wrong_role_permission"
        if report.get("healthy") is not True:
            return "unhealthy_peer"
        roles[role] = report
    if len(roles) != 2:
        return "missing_peer"
    trader, verifier = roles["trader"], roles["verifier"]
    if trader["chart_id"] == verifier["chart_id"]:
        return "same_chart"
    if trader["instance_id"] == verifier["instance_id"]:
        return "same_instance"
    return "ready"
