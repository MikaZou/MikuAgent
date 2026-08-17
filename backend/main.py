"""MikuAgent 后端入口：FastAPI 服务 + 前端静态资源。"""
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from agent import MikuAgent
from memory import MemoryStore
from stt import SpeechToText

memory = MemoryStore(config.DB_PATH)
agent = MikuAgent(memory)
stt = SpeechToText()

app = FastAPI(
    title="MikuAgent",
    description="初音未来虚拟桌宠后端（DeepSeek Agent）",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="用户输入")
    session_id: Optional[int] = Field(None, description="会话 ID，缺省时自动新建")


class ChatResponse(BaseModel):
    reply: str
    emotion: str
    session_id: int
    mock: bool


class MetaRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    value: str = Field("", max_length=500)


class MicStartResponse(BaseModel):
    ok: bool
    message: str = ""


class MicStopResponse(BaseModel):
    ok: bool
    text: str = ""
    duration: float = 0.0
    error: str = ""


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mock": not agent.live,
        "has_api_key": config.HAS_API_KEY,
        "model": agent.model,
        "base_url": config.DEEPSEEK_BASE_URL,
        "version": app.version,
        "stt": {
            "status": stt.status,
            "model": config.STT_MODEL,
            "language": config.STT_LANGUAGE or "auto",
        },
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    result = agent.chat(req.session_id, req.message)
    return ChatResponse(
        reply=result["reply"],
        emotion=result["emotion"],
        session_id=result["session_id"],
        mock=result["mock"],
    )


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": memory.list_sessions()}


@app.post("/api/sessions")
def create_session():
    return memory.create_session()


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages(session_id: int):
    if memory.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"messages": memory.get_messages(session_id)}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int):
    if memory.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    memory.delete_session(session_id)
    return {"ok": True}


@app.get("/api/memory")
def list_memories():
    return {"memories": memory.list_memories()}


@app.delete("/api/memory/{memory_id}")
def delete_memory(memory_id: int):
    if not memory.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@app.post("/api/meta")
def set_meta(req: MetaRequest):
    memory.set_meta(req.key.strip(), req.value.strip())
    return {"ok": True}


@app.post("/api/mic/start")
def mic_start():
    ok, message = stt.start()
    return MicStartResponse(ok=ok, message=message)


@app.post("/api/mic/stop")
def mic_stop():
    result = stt.stop()
    return MicStopResponse(**result)


@app.post("/api/mic/cancel")
def mic_cancel():
    stt.cancel()
    return {"ok": True}


# 前端静态资源（最后挂载，/api 路由优先）
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
