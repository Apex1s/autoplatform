"""Local stand-in for Groq + Gemini (OpenAI-compatible) to rehearse failover without keys.

    FAKE_GROQ_MODE=429 uvicorn scripts.dev_fake_llm:app --port 58001
    GROQ_BASE_URL=http://127.0.0.1:58001/groq GEMINI_BASE_URL=http://127.0.0.1:58001/gemini ...

Modes per provider (FAKE_GROQ_MODE / FAKE_GEMINI_MODE): ok | 429 | timeout | 500
"""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
calls = {"groq": 0, "gemini": 0}


@app.post("/{provider}/chat/completions")
async def chat(provider: str, request: Request):
    calls[provider] = calls.get(provider, 0) + 1
    mode = os.getenv(f"FAKE_{provider.upper()}_MODE", "ok")
    body = await request.json()
    if mode == "429":
        return JSONResponse({"error": {"message": "rate limit (simulated)"}}, 429, {"retry-after": "30"})
    if mode == "500":
        return JSONResponse({"error": "boom"}, 500)
    if mode == "timeout":
        await asyncio.sleep(120)
    return {"model": body["model"], "usage": {"prompt_tokens": 9, "completion_tokens": 2},
            "choices": [{"message": {"role": "assistant", "content": f"pong (from {provider})"}}]}


@app.get("/calls")
async def get_calls():
    return calls
