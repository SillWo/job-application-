import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.router import router as api_router
from backend.api.router import session_socket
from backend.browser.sessions import close_browser, open_browsers
from backend.config import settings
from backend.orchestrator.workflow import recover_orphaned_sessions, workflow_manager
from backend.persistence.database import init_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    from backend.persistence.database import SessionLocal
    from backend.services.profile_memory import collect_finished_sessions

    with SessionLocal() as db:
        collect_finished_sessions(db)
        db.commit()
    recover_orphaned_sessions()
    try:
        yield
    finally:
        tasks = list(workflow_manager.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_browser(key) for key in list(open_browsers)), return_exceptions=True)


app = FastAPI(title="Job Application Orchestrator", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)

# Keep the SPA fallback from masking misspelled or removed API endpoints.
@app.get("/api/{path:path}", include_in_schema=False)
def unknown_api_path(path: str) -> None:
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="API endpoint not found")


@app.websocket("/ws/sessions/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: int) -> None:
    await session_socket(websocket, session_id)


dist = Path(settings.frontend_dist)
if dist.exists():
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"message": "Frontend не собран. Выполните scripts/bootstrap.ps1", "docs": "/docs"}
