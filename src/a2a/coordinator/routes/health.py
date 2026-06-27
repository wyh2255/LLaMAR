"""健康检查。"""

def register_routes(app, server):
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "workers": len(await server.registry.list_online()),
            "pending_tasks": len(server.task_queue.list_pending()),
        }

    @app.get("/health/ready")
    async def ready():
        online = await server.registry.list_online()
        if not online:
            return {"ready": False, "reason": "no workers online"}
        return {"ready": True}
