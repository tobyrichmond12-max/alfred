"""Alfred server, unified web UI + API + WebSocket."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

from webserver import start_web_server

async def main():
    await start_web_server(host="0.0.0.0", port=8080)
    print("Alfred is ready, sir.")
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
