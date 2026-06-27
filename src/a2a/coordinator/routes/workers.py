"""Worker 管理 REST API。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="", tags=["workers"])


class WorkerListResponse(BaseModel):
    workers: list[dict]


class WorkerResponse(BaseModel):
    worker_id: str
    a2a_endpoint: str
    capabilities: list[str] = []
    status: str


def register_routes(app, server):
    """将路由注册到 FastAPI app。"""

    @router.get("/workers", response_model=WorkerListResponse)
    async def list_workers():
        online = await server.registry.list_online()
        workers_data = []
        for w in online:
            caps: list[str] = []
            try:
                agent = server._agent_registry.get(w.worker_id)
                caps = agent.capabilities
            except Exception:
                pass
            workers_data.append({
                "worker_id": w.worker_id,
                "a2a_endpoint": w.a2a_endpoint,
                "capabilities": caps,
                "status": w.status.value,
            })
        return WorkerListResponse(workers=workers_data)

    @router.get("/workers/{worker_id}", response_model=WorkerResponse)
    async def get_worker(worker_id: str):
        try:
            w = server.registry.get(worker_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Worker not found")
        caps: list[str] = []
        try:
            agent = server._agent_registry.get(worker_id)
            caps = agent.capabilities
        except Exception:
            pass
        return WorkerResponse(
            worker_id=w.worker_id,
            a2a_endpoint=w.a2a_endpoint,
            capabilities=caps,
            status=w.status.value,
        )

    app.include_router(router)
