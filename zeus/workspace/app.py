"""Separate multiuser API. It does not expose the legacy administrator routes."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address
from fastapi import FastAPI, Request, Response, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

from zeus import PROJECT_ROOT
from zeus.safety import safe_error
from .storage import Store
from .accounts import HyperliquidAccounts
from .runtime import Runtimes


@dataclass
class Settings:
    root: Path
    origin: str = 'http://127.0.0.1:8081'
    database_url: str = ''
    encryption_key: str = ''
    walletconnect_id: str = ''
    production: bool = False
    warm_catalogs: bool = True

    @classmethod
    def environment(cls):
        return cls(Path(os.getenv('ZEUS_WORKSPACE_STATE', PROJECT_ROOT/'.zeus-workspace')),
                   os.getenv('ZEUS_WORKSPACE_ORIGIN', 'http://127.0.0.1:8081').rstrip('/'),
                   os.getenv('ZEUS_WORKSPACE_DATABASE_URL', ''), os.getenv('ZEUS_WORKSPACE_KEY', ''),
                   os.getenv('ZEUS_WALLETCONNECT_PROJECT_ID', ''), os.getenv('ZEUS_WORKSPACE_PRODUCTION') == '1')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Challenge(Input):
    address: str = Field(min_length=42, max_length=42)
    chain_id: int = Field(default=1, gt=0, le=2**48)


class Signature(Input):
    signature: str = Field(min_length=132, max_length=132, pattern=r'^0x[0-9a-fA-F]{130}$')


class Launch(Input):
    strategy: Literal['outcome', 'bbo', 'mid']
    order_usd: float = Field(ge=10.5, le=1000000, allow_inf_nan=False)
    position_cap: float = Field(ge=10.5, le=10000000, allow_inf_nan=False)
    market: str = Field(default='', max_length=64)
    side: Literal['yes', 'no'] = 'no'
    spread_bps: float = Field(default=10, gt=0, le=100, allow_inf_nan=False)
    offset_bps: float = Field(default=1, ge=0, lt=10000, allow_inf_nan=False)
    duration_sec: float | None = Field(default=None, ge=60, le=2592000, allow_inf_nan=False)
    target_volume_usd: float | None = Field(default=None, ge=10, le=1000000000, allow_inf_nan=False)
    stop_loss_usd: float = Field(default=50, gt=0, le=1000000, allow_inf_nan=False)


def create_app(settings=None, *, accounts_factory=HyperliquidAccounts, manager_factory=None):
    settings = settings or Settings.environment()
    settings.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if settings.production and (not settings.origin.startswith('https://') or not settings.encryption_key
                               or not settings.database_url.startswith(('postgresql://', 'postgresql+psycopg://'))):
        raise RuntimeError('Production requires HTTPS origin, PostgreSQL and an external encryption key')
    if not settings.encryption_key:
        keyfile = settings.root/'agent-encryption.key'
        try:
            fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream: stream.write(Fernet.generate_key())
        settings.encryption_key = keyfile.read_text().strip()
    url = settings.database_url or 'sqlite:///'+str((settings.root/'workspace.db').resolve()).replace('\\', '/')
    if url.startswith('postgresql://'): url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    store = Store(url, settings.encryption_key)
    accounts = accounts_factory(store)
    runtimes = Runtimes(settings.root, store, accounts, manager_factory)
    allowed_origins = {settings.origin}
    if not settings.production: allowed_origins.update({'http://127.0.0.1:5173', 'http://localhost:5173'})
    secure = settings.origin.startswith('https://')
    cookie = 'zeus_workspace'

    @asynccontextmanager
    async def lifespan(app):
        # Scanner/catalog lifetime belongs to this process, not the old server.
        from zeus.server import _warm_public_catalogs, public_catalogs
        warming = asyncio.create_task(_warm_public_catalogs()) if settings.warm_catalogs else None
        try: yield
        finally:
            if warming:
                warming.cancel()
                await asyncio.gather(warming, return_exceptions=True)
            await asyncio.to_thread(runtimes.close)
            from zeus.outcome_flow import scanner
            await asyncio.to_thread(scanner.close)
            await public_catalogs.close()
            store.engine.dispose()

    app = FastAPI(title='Zeus Wallet Workspace', lifespan=lifespan)
    app.state.store, app.state.accounts, app.state.runtimes = store, accounts, runtimes
    app.state.settings = settings

    def identity(request: Request):
        result = store.identity(request.cookies.get(cookie))
        if not result: raise HTTPException(401, 'Connect your wallet to continue')
        return result

    @app.middleware('http')
    async def boundaries(request, call_next):
        if request.url.path.startswith('/api/') and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if request.headers.get('origin') not in allowed_origins:
                return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    def mutation(request: Request, who=Depends(identity)):
        if not secrets.compare_digest(request.headers.get('x-zeus-csrf', ''), who['csrf']):
            raise HTTPException(403, 'Session verification required')
        return who

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': safe_error(exc)}, status_code=400)

    async def exchange_unavailable(request, exc):
        return JSONResponse({'detail': 'Hyperliquid temporarily unavailable. Verify the last operation before retrying.'}, status_code=503)
    for error_type in requests_error_types():
        app.add_exception_handler(error_type, exchange_unavailable)

    @app.get('/api/v2/config')
    def config():
        return {'walletconnect_project_id': settings.walletconnect_id, 'origin': settings.origin,
                'features': {'pause': False, 'session_targets': True}, 'version': 'workspace-1'}

    attempts = {}
    attempts_lock = threading.Lock()

    @app.post('/api/v2/auth/challenge')
    def challenge(body: Challenge, request: Request, response: Response):
        if not is_address(body.address): raise HTTPException(422, 'Invalid wallet address')
        ip = request.client.host if request.client else 'unknown'
        with attempts_lock:
            now = time.time()
            expired = [k for k, v in attempts.items() if now-v[0] > 60]
            for k in expired: attempts.pop(k, None)
            first, count = attempts.get(ip, (now, 0))
            if count >= 15 or len(attempts) > 10000: raise HTTPException(429, 'Try again in a minute')
            attempts[ip] = (first, count+1)
        address = body.address.lower()
        issued = datetime.now(timezone.utc)
        message = (f"{urlparse(settings.origin).netloc} wants you to sign in with your Ethereum account:\n"
                   f"{to_checksum_address(address)}\n\n"
                   'Sign in to Zeus. This does not authorize trading.\n\n'
                   f"URI: {settings.origin}\nVersion: 1\nChain ID: {body.chain_id}\n"
                   f"Nonce: {secrets.token_hex(16)}\nIssued At: {issued.isoformat().replace('+00:00', 'Z')}\n"
                   f"Expiration Time: {(issued+timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')}")
        token = store.challenge(address, message)
        response.set_cookie('zeus_challenge', token, httponly=True, secure=secure, samesite='strict', max_age=300,
                            path='/api/v2/auth')
        return {'message': message}

    @app.post('/api/v2/auth/verify')
    def verify(body: Signature, request: Request, response: Response):
        token = request.cookies.get('zeus_challenge')
        if not token: raise HTTPException(401, 'Request a new login message')
        challenge = store.consume_challenge(token)
        try: wallet = Account.recover_message(encode_defunct(text=challenge['message']), signature=body.signature).lower()
        except Exception: raise HTTPException(401, 'Invalid signature')
        if wallet != challenge['wallet']: raise HTTPException(401, 'Signature does not match the wallet')
        old = request.cookies.get(cookie)
        if old: store.logout(old)
        response.set_cookie(cookie, store.create_session(wallet), httponly=True, secure=secure,
                            samesite='strict', max_age=43200, path='/')
        response.delete_cookie('zeus_challenge', path='/api/v2/auth')
        return {'wallet': wallet}

    @app.get('/api/v2/me')
    def me(who=Depends(identity)):
        agent = store.agent(who['wallet'])
        return {'wallet': who['wallet'], 'account': who['wallet'], 'csrf': who['csrf'],
                'agent': {k: agent[k] for k in ('address', 'status', 'expires')} if agent else None}

    @app.post('/api/v2/auth/logout')
    def logout(request: Request, response: Response, who=Depends(mutation)):
        store.logout(request.cookies[cookie]); response.delete_cookie(cookie, path='/')
        return {'ok': True}

    @app.get('/api/v2/account')
    def account(who=Depends(identity)):
        try:
            balances = accounts.balances(who['wallet'])
            agent = accounts.verify(who['wallet'])
        except requests_error_types() as exc:
            raise HTTPException(503, 'Hyperliquid is unavailable. Your last session state is preserved.')
        return {'account': who['wallet'], **balances,
                'agent': {k: agent[k] for k in ('address', 'status', 'expires')} if agent else None}

    @app.post('/api/v2/agent/prepare')
    def prepare(who=Depends(mutation)):
        if any(r['running'] or not r['cleanup_confirmed'] for r in runtimes.snapshot(who['wallet'])['sessions']):
            old = store.agent(who['wallet'])
            if not old: raise HTTPException(409, 'Verify the running sessions first')
        return accounts.prepare(who['wallet'])

    @app.post('/api/v2/agent/approve')
    def approve(body: Signature, who=Depends(mutation)):
        return accounts.approve(who['wallet'], body.signature)

    @app.get('/api/v2/workspace')
    def workspace(who=Depends(identity)):
        return runtimes.snapshot(who['wallet'])

    @app.get('/api/v2/markets')
    async def markets(strategy: Literal['outcome', 'bbo', 'mid'] = 'outcome', who=Depends(identity)):
        from zeus.server import public_catalogs, _grid_token_payload
        catalog = await public_catalogs.snapshot('hip4' if strategy == 'outcome' else 'hip3')
        if strategy == 'outcome': return catalog
        tokens = [_grid_token_payload(key, cfg) for key, cfg in catalog['token_map'].items()]
        return {'tokens': sorted(tokens, key=lambda t: -float(t.get('day_volume') or 0))}

    @app.get('/api/v2/scanner')
    async def scanner(side: Literal['no', 'yes'] = 'no', order_usd: float = 25,
                      spread_bps: float = 10, who=Depends(identity)):
        if not 10.5 <= order_usd <= 1000000 or not 0 < spread_bps <= 100: raise HTTPException(422)
        from zeus.server import public_catalogs
        from zeus.outcome_flow import scanner
        catalog = await public_catalogs.snapshot('hip4')
        await asyncio.to_thread(scanner.configure, catalog['outcomes'])
        result = await asyncio.to_thread(scanner.snapshot, side, order_usd, spread_bps)
        state = await asyncio.to_thread(runtimes.snapshot, who['wallet'])
        busy = {s['market'] for s in state['sessions'] if s['running'] or not s['cleanup_confirmed']}
        result['candidates'] = [m for m in result.get('candidates', []) if m['market'] not in busy]
        result['ready'] = bool(result['candidates'])
        return result

    @app.post('/api/v2/sessions/preview')
    def preview(body: Launch, who=Depends(mutation)):
        if body.order_usd > body.position_cap: raise ValueError('Position cap must cover one order')
        if body.strategy != 'outcome' and not body.market: raise ValueError('Choose a market')
        if body.strategy != 'outcome' and (body.duration_sec or body.target_volume_usd):
            raise ValueError('Duration and volume targets are currently available for Auto Outcome')
        balance = accounts.balances(who['wallet'])
        if body.strategy == 'outcome' and balance['usdc_available'] < body.order_usd:
            raise ValueError('Available spot USDC is below the order size')
        return {'config': body.model_dump(), 'account': who['wallet'], **balance,
                'notice': 'Outcome quantities are rounded to whole tokens. Final funding is checked before every order.'}

    @app.post('/api/v2/sessions')
    def start(body: Launch, request: Request, who=Depends(mutation)):
        key = request.headers.get('idempotency-key', '')
        if not 16 <= len(key) <= 128: raise HTTPException(422, 'An idempotency key is required')
        if body.order_usd > body.position_cap: raise ValueError('Position cap must cover one order')
        if body.strategy != 'outcome' and (body.duration_sec or body.target_volume_usd):
            raise ValueError('Duration and volume targets are currently available for Auto Outcome')
        command, created = store.reserve_command(who['wallet'], key, body.model_dump())
        if created:
            before = runtimes.execution_revision(who['wallet'])
            try:
                result = runtimes.launch(who['wallet'], body.model_dump())
                store.finish_command(who['wallet'], key, 'confirmed', result)
            except Exception as exc:
                # A spawn/ack can race a transport failure. Keep the command
                # consumed; resolving this operation never retries the order.
                rejected = isinstance(exc, ValueError) and before == runtimes.execution_revision(who['wallet'])
                store.finish_command(who['wallet'], key, 'rejected' if rejected else 'unconfirmed', {'message': safe_error(exc)})
        command = store.command(who['wallet'], key)
        return {'command': key, 'phase': command['phase'], 'result': command['result']}

    @app.get('/api/v2/commands/{key}')
    def command(key: str, who=Depends(identity)):
        row = store.command(who['wallet'], key)
        if not row: raise HTTPException(404)
        return {'command': key, 'phase': row['phase'], 'result': row['result']}

    @app.post('/api/v2/sessions/{sid}/stop')
    def stop(sid: str, who=Depends(mutation)):
        try: return runtimes.stop(who['wallet'], sid)
        except KeyError: raise HTTPException(404, 'Session not found')

    @app.post('/api/v2/reconcile')
    def reconcile(who=Depends(mutation)):
        return runtimes.reconcile(who['wallet'])

    @app.websocket('/api/v2/ws')
    async def live(ws: WebSocket):
        if ws.headers.get('origin') not in allowed_origins:
            await ws.close(code=1008); return
        token = ws.cookies.get(cookie)
        who = await asyncio.to_thread(store.identity, token)
        if not who: await ws.close(code=1008); return
        await ws.accept()
        try:
            while await asyncio.to_thread(store.identity, token):
                try: await ws.send_json(await asyncio.to_thread(runtimes.snapshot, who['wallet']))
                except (OSError, ValueError): await ws.send_json({'type': 'delayed'})
                await asyncio.sleep(1)
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError): pass

    dist = PROJECT_ROOT/'frontend'/'workspace'/'dist'
    if (dist/'assets').exists(): app.mount('/assets', StaticFiles(directory=dist/'assets'), name='workspace-assets')

    @app.get('/{path:path}')
    def frontend(path: str):
        if path.startswith('api/') or path.startswith('assets/'): raise HTTPException(404)
        if not (dist/'index.html').exists():
            return JSONResponse({'detail': 'Build the workspace frontend with npm run build in frontend/workspace'}, status_code=503)
        return FileResponse(dist/'index.html')

    return app


def requests_error_types():
    import requests
    return (requests.RequestException, TimeoutError)
