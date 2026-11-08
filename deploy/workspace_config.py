"""EC2 configuration helpers. Never print credentials or change an existing key."""
import argparse
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import urlsplit


def public_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Provide the public HTTPS origin without a path')
    return value.rstrip('/')


def caddy_target(text, hostname):
    """Only replace a single simple upstream inside the specified site block."""
    depth = 0
    start = None
    target = None
    offset = 0
    for line in text.splitlines(keepends=True):
        clean = line.split('#', 1)[0]
        if depth == 0 and '{' in clean:
            names = clean.split('{', 1)[0].split()
            target = any(name in (hostname, 'https://' + hostname) for name in names)
            start = offset
        depth += clean.count('{') - clean.count('}')
        if depth < 0:
            raise ValueError('Unsupported Caddy configuration; no change made')
        if depth == 0 and target and start is not None:
            end = offset + len(line)
            block = text[start:end]
            old = r'(?m)^([ \t]*reverse_proxy[ \t]+)127\.0\.0\.1:8080([ \t]*(?:#.*)?$)'
            new = r'(?m)^[ \t]*reverse_proxy[ \t]+127\.0\.0\.1:8081[ \t]*(?:#.*)?$'
            if len(re.findall(new, block)) == 1 and not re.search(old, block):
                return text
            changed, count = re.subn(old, r'\g<1>127.0.0.1:8081\2', block)
            if count != 1:
                raise ValueError('Expected one simple Zeus upstream on 8080; update Caddy manually')
            return text[:start] + changed + text[end:]
        offset += len(line)
    raise ValueError('Public hostname not found in Caddyfile; no change made')


def psql(query):
    result = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                             '-At', '-d', 'postgres'], input=query, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('PostgreSQL administration failed; no credentials printed')
    return result.stdout.strip()


def configure(root, origin):
    from cryptography.fernet import Fernet
    from dotenv import dotenv_values
    from sqlalchemy import create_engine, text
    origin = public_origin(origin)
    path = root / '.env.workspace'
    if path.exists():
        values = dict(dotenv_values(path))
        if values.get('ZEUS_WORKSPACE_ORIGIN') != origin:
            raise ValueError('Existing workspace origin differs; review .env.workspace before changing it')
        if values.get('ZEUS_WORKSPACE_PRODUCTION') != '1' or not values.get('ZEUS_WORKSPACE_KEY'):
            raise ValueError('Existing workspace production configuration is incomplete; preserving it')
        if not str(values.get('ZEUS_WORKSPACE_DATABASE_URL', '')).startswith(('postgresql://', 'postgresql+psycopg://')):
            raise ValueError('Existing workspace requires a PostgreSQL URL')
    else:
        # Reserve credentials first so interrupted provisioning resumes with the same key.
        password = secrets.token_hex(32)
        values = {'ZEUS_WORKSPACE_PRODUCTION': '1', 'ZEUS_WORKSPACE_HOST': '127.0.0.1',
                  'ZEUS_WORKSPACE_PORT': '8081', 'ZEUS_WORKSPACE_ORIGIN': origin,
                  'ZEUS_WORKSPACE_DATABASE_URL': f'postgresql+psycopg://zeus_workspace:{password}@127.0.0.1/zeus_workspace',
                  'ZEUS_WORKSPACE_KEY': Fernet.generate_key().decode(),
                  'ZEUS_WORKSPACE_LOCAL_DATABASE': '1', 'ZEUS_WALLETCONNECT_PROJECT_ID': ''}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    Fernet(values['ZEUS_WORKSPACE_KEY'].encode())
    os.chmod(path, 0o600)
    if values.get('ZEUS_WORKSPACE_LOCAL_DATABASE') == '1':
        password = urlsplit(values['ZEUS_WORKSPACE_DATABASE_URL']).password
        if not password or not re.fullmatch('[0-9a-f]{64}', password):
            raise ValueError('Invalid generated local database configuration')
        if not psql("SELECT 1 FROM pg_roles WHERE rolname='zeus_workspace';"):
            psql(f"SET password_encryption='scram-sha-256'; CREATE ROLE zeus_workspace LOGIN PASSWORD '{password}';")
        if not psql("SELECT 1 FROM pg_database WHERE datname='zeus_workspace';"):
            psql('CREATE DATABASE zeus_workspace OWNER zeus_workspace;')
        # Scope password authentication to this database, role and loopback only.
        hba = psql('SHOW hba_file;')
        desired = 'host zeus_workspace zeus_workspace 127.0.0.1/32 scram-sha-256'
        current = subprocess.check_output(['sudo', 'cat', hba], text=True)
        if desired not in current:
            backup = hba + '.before-zeus-' + secrets.token_hex(4)
            subprocess.run(['sudo', 'cp', '-p', hba, backup], check=True)
            subprocess.run(['sudo', 'tee', hba], input=desired + '\n' + current,
                           text=True, stdout=subprocess.DEVNULL, check=True)
            psql('SELECT pg_reload_conf();')
    url = values['ZEUS_WORKSPACE_DATABASE_URL'].replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except Exception:
        raise RuntimeError('Workspace database connection failed; existing credentials preserved') from None
    finally:
        engine.dispose()
    print('Production configuration and PostgreSQL connection verified; secrets remain in .env.workspace')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['configure', 'caddy'])
    parser.add_argument('--origin', required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    origin = public_origin(args.origin)
    if args.mode == 'configure':
        configure(args.root, origin)
    else:
        args.output.write_text(caddy_target(args.input.read_text(), urlsplit(origin).netloc))
