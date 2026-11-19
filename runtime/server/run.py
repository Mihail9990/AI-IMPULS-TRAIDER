"""Backend is chosen locally on VPS, not remotely by the phone."""
import os
import uvicorn
from service import Settings, create_app


def make_backend(settings):
    backend = os.environ.get('WIDGET_BACKEND', 'python').lower()
    if backend == 'ea':
        from ea_adapter import EAAdapter
        directory = os.environ['WIDGET_EA_DIRECTORY']
        if not os.path.isabs(directory):
            raise ValueError('WIDGET_EA_DIRECTORY must be an absolute Common Files path')
        return EAAdapter(directory, settings.account, settings.server)
    if backend == 'python':
        if not settings.terminal_path:
            raise ValueError('MT5_TERMINAL_PATH is required for Python backend')
        import MetaTrader5
        return MetaTrader5
    raise ValueError('WIDGET_BACKEND must be python or ea')


def main():
    settings = Settings.from_env()
    mt5 = make_backend(settings)
    if not mt5.initialize(path=settings.terminal_path):
        raise RuntimeError('Cannot initialize chosen MT5 backend')
    try:
        app = create_app(mt5, settings)
        app.state.bridge.guard()
        uvicorn.run(app, host='127.0.0.1', port=8766, access_log=False)
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
