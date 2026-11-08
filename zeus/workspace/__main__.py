import os
import uvicorn
from zeus.server import load_dotenv
from zeus import PROJECT_ROOT
from .app import create_app

if __name__ == '__main__':
    load_dotenv(PROJECT_ROOT/'.env')
    uvicorn.run(create_app(), host=os.getenv('ZEUS_WORKSPACE_HOST', '127.0.0.1'),
                port=int(os.getenv('ZEUS_WORKSPACE_PORT', '8081')))
