import os
import uvicorn
from zeus import PROJECT_ROOT
from zeus.config import load_dotenv

# Tenant managers are isolated from the private dashboard. Never load the
# operator .env or instantiate the legacy manager in this process.
os.environ.setdefault('ZEUS_WORKSPACE_ONLY', '1')
load_dotenv(PROJECT_ROOT / '.env.workspace')

from .app import create_app

if __name__ == '__main__':
    uvicorn.run(create_app(), host=os.getenv('ZEUS_WORKSPACE_HOST', '127.0.0.1'),
                port=int(os.getenv('ZEUS_WORKSPACE_PORT', '8081')))
