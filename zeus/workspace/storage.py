"""Transactional identities, encrypted agents and idempotent commands."""
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text, Float,
                        Boolean, select, insert, update, delete, and_)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, url, encryption_key):
        self.engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 15}
                                    if url.startswith('sqlite') else {}, pool_pre_ping=True)
        self.cipher = Fernet(encryption_key)
        self.meta = MetaData()
        self.challenges = Table('workspace_challenges', self.meta,
            Column('id', String(64), primary_key=True), Column('wallet', String(42), nullable=False),
            Column('message', Text, nullable=False), Column('expires', Float, nullable=False),
            Column('used', Boolean, nullable=False, default=False))
        self.sessions = Table('workspace_web_sessions', self.meta,
            Column('id', String(64), primary_key=True), Column('wallet', String(42), nullable=False),
            Column('csrf', String(64), nullable=False), Column('expires', Float, nullable=False))
        self.agents = Table('workspace_agents', self.meta,
            Column('owner', String(42), primary_key=True), Column('account', String(42), unique=True, nullable=False),
            Column('address', String(42), nullable=False), Column('secret', Text, nullable=False),
            Column('name', String(120), nullable=False), Column('status', String(24), nullable=False),
            Column('approval', Text, nullable=False), Column('expires', Float, nullable=False))
        self.commands = Table('workspace_commands', self.meta,
            Column('id', String(64), primary_key=True), Column('owner', String(42), nullable=False),
            Column('fingerprint', String(64), nullable=False), Column('request', Text, nullable=False),
            Column('phase', String(24), nullable=False), Column('result', Text, nullable=False),
            Column('created', Float, nullable=False))
        self.meta.create_all(self.engine)

    def challenge(self, wallet, message):
        token = secrets.token_hex(32)
        with self.engine.begin() as db:
            db.execute(delete(self.challenges).where(self.challenges.c.expires < time.time()-600))
            db.execute(insert(self.challenges).values(id=digest(token), wallet=wallet,
                       message=message, expires=time.time()+300, used=False))
        return token

    def consume_challenge(self, token):
        with self.engine.begin() as db:
            row = db.execute(select(self.challenges).where(self.challenges.c.id == digest(token))).mappings().first()
            if not row or row['used'] or row['expires'] <= time.time():
                raise ValueError('Challenge expired or already used')
            changed = db.execute(update(self.challenges).where(and_(self.challenges.c.id == row['id'],
                        self.challenges.c.used == False)).values(used=True))
            if changed.rowcount != 1:
                raise ValueError('Challenge already used')
            return dict(row)

    def create_session(self, wallet):
        token, csrf = secrets.token_hex(32), secrets.token_hex(32)
        with self.engine.begin() as db:
            db.execute(delete(self.sessions).where(self.sessions.c.expires < time.time()))
            db.execute(insert(self.sessions).values(id=digest(token), wallet=wallet,
                       csrf=csrf, expires=time.time()+43200))
        return token

    def identity(self, token):
        if not token: return None
        with self.engine.connect() as db:
            row = db.execute(select(self.sessions).where(and_(self.sessions.c.id == digest(token),
                         self.sessions.c.expires > time.time()))).mappings().first()
            return dict(row) if row else None

    def logout(self, token):
        with self.engine.begin() as db:
            db.execute(delete(self.sessions).where(self.sessions.c.id == digest(token)))

    def agent(self, owner, *, secret=False):
        with self.engine.connect() as db:
            row = db.execute(select(self.agents).where(self.agents.c.owner == owner)).mappings().first()
            if not row: return None
            row = dict(row)
            encrypted = row.pop('secret')
            row['approval'] = json.loads(row['approval'])
            if secret: row['private_key'] = self.cipher.decrypt(encrypted.encode()).decode()
            return row

    def save_agent(self, owner, account, address, private_key, name, approval, expires):
        with self.engine.begin() as db:
            db.execute(delete(self.agents).where(self.agents.c.owner == owner))
            db.execute(insert(self.agents).values(owner=owner, account=account, address=address,
                secret=self.cipher.encrypt(private_key.encode()).decode(), name=name, status='prepared',
                approval=json.dumps(approval), expires=expires))

    def agent_status(self, owner, status):
        with self.engine.begin() as db:
            db.execute(update(self.agents).where(self.agents.c.owner == owner).values(status=status))

    def reserve_approval(self, owner):
        with self.engine.begin() as db:
            result = db.execute(update(self.agents).where(and_(self.agents.c.owner == owner,
                    self.agents.c.status.in_(['prepared', 'authorization_required']))).values(status='approval_pending'))
            return result.rowcount == 1

    def reserve_command(self, owner, key, payload):
        from sqlalchemy.exc import IntegrityError
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        values = dict(id=digest(owner+':'+key), owner=owner, fingerprint=digest(encoded), request=encoded,
                      phase='starting', result='{}', created=time.time())
        try:
            with self.engine.begin() as db: db.execute(insert(self.commands).values(**values))
            return {**values, 'result': {}}, True
        except IntegrityError:
            row = self.command(owner, key)
            if row['fingerprint'] != values['fingerprint']:
                raise ValueError('This command ID belongs to different settings')
            return row, False

    def command(self, owner, key):
        with self.engine.connect() as db:
            row = db.execute(select(self.commands).where(and_(self.commands.c.id == digest(owner+':'+key),
                         self.commands.c.owner == owner))).mappings().first()
            if not row: return None
            row = dict(row); row['result'] = json.loads(row['result']); return row

    def finish_command(self, owner, key, phase, result):
        with self.engine.begin() as db:
            db.execute(update(self.commands).where(and_(self.commands.c.id == digest(owner+':'+key),
                       self.commands.c.owner == owner)).values(phase=phase, result=json.dumps(result)))
