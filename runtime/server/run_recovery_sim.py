"""Run simulation-only API. WIDGET_LIVE never enables execution here."""
import os
import uvicorn
from recovery.api import create_app

if __name__ == '__main__':
    if os.getenv('WIDGET_LIVE', '0') != '0':
        raise SystemExit('RECOVERY prototype is simulation-only; unset WIDGET_LIVE')
    app = create_app(os.environ.get('WIDGET_TOKEN', ''),
                     os.getenv('RECOVERY_SIM_JOURNAL', 'recovery-simulation.sqlite3'))
    uvicorn.run(app, host='127.0.0.1', port=8767, access_log=False)
