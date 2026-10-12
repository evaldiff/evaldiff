"""Controlled model fixture. Run behind trusted HTTPS on a separate public host."""

import asyncio
import os

from fastapi import FastAPI, Header, HTTPException

VERSION = 1
CONTENT = "qualification-ok".ljust(1024, " ")
app = FastAPI()


@app.get("/health")
def health():
    return {
        "fixture_version": VERSION,
        "content_bytes": len(CONTENT.encode()),
        "delays": {"healthy": 0.2, "slow": 2},
        "stall_seconds": 3600,
    }


@app.post("/{behavior}/v1/chat/completions")
async def completion(behavior: str, authorization: str = Header(default="")):
    secret = os.environ.get("EVALDIFF_FIXTURE_KEY")
    if not secret or authorization != "Bearer " + secret:
        raise HTTPException(401, "fixture authentication failed")
    if behavior == "error":
        raise HTTPException(503, "controlled failure")
    if behavior == "stall":
        await asyncio.sleep(3600)
    elif behavior in ("healthy", "slow"):
        await asyncio.sleep(0.2 if behavior == "healthy" else 2)
    else:
        raise HTTPException(404, "unknown fixture")
    return {
        "choices": [{"message": {"content": CONTENT}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
