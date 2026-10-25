"""Launch the gateway on loopback only (AGPL-3.0-or-later)."""

import os

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    port = int(os.getenv("WEB2API_PORT", "8765"))
    print(
        f"\nGemini Web2API: http://127.0.0.1:{port}\n按 Ctrl+C 停止服务。\n", flush=True
    )
    uvicorn.run("server:app", host="127.0.0.1", port=port, access_log=False)
