# First actual-fill correction

`InitialStopProof(control).validate` is an owner-bound callback for native
preflight. It is not granted by a request flag. The default validator has no
callback and rejects widening. The strategy connects the callback and passes
`initial_stop_key` referencing an already durable stage-0 checkpoint.

For widening only, each validation independently reads the owner checkpoint,
validated saved settings, verified original order journal, exact entry history,
and current position. It requires no previously confirmed protection (including
confirmed stage 0), terminal entry without pending remainder or exits, an
unchanged saved entry ledger, exact original attached SL still on the position,
preserved TP, and the target calculated from the actual weighted fill and saved
initial distance. Generic position ownership, price-grid, freeze/stops, account
and symbol validations still run. Support or manual changes never grant this
exception. No API initialization or sending occurs in this helper.

The normalizer sees broker numeric values; no approximate stop matching is used.
All checks are non-atomic, as is the underlying MT5 Python API. A manual stop
changed and restored to exactly the original value between observations cannot
be detected by snapshots. A final external change between validation and send
also cannot be ruled out. Do not advertise this as atomic protection or proven
absence of all external edits. A dedicated account/terminal writer policy and
Windows/broker testing remain necessary. No live validation was performed.
