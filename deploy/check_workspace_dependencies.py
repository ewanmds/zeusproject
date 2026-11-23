"""Validate the HTTP test stack without importing Zeus or opening a socket."""
from importlib.metadata import version
import sys


def main():
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        @app.get('/dependency-check')
        def ready():
            return {'ready': True}
        with TestClient(app) as client:
            response = client.get('/dependency-check')
            if response.status_code != 200 or response.json() != {'ready': True}:
                raise RuntimeError('TestClient smoke check failed')
        print(f'Deployment test stack ready: FastAPI {version("fastapi")}, '
              f'Starlette {version("starlette")}, HTTPX {version("httpx")}')
        return 0
    except Exception as exc:
        print(f'Deployment dependency check failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        print('Install requirements.txt with .venv-workspace/bin/python -m pip install -r requirements.txt', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
