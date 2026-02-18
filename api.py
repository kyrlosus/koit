from dotenv import load_dotenv
load_dotenv()

import sys
import os
import asyncio
import time
import uuid
import psutil
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Response, Depends, APIRouter, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse

# Core Services & Utils
from core.services import redis
from core.utils.openapi_config import configure_openapi
from core.agentpress.thread_manager import ThreadManager
from core.services.supabase import DBConnection
from core.utils.config import config, EnvMode
from core.utils.logger import logger, structlog

# Routers
from core.versioning.api import router as versioning_router
from core.agents.api import router as agent_runs_router
from core.agents.agent_crud import router as agent_crud_router
from core.agents.agent_tools import router as agent_tools_router
from core.agents.agent_json import router as agent_json_router
from core.agents.agent_setup import router as agent_setup_router
from core.threads.api import router as threads_router
from core.categorization.api import router as categorization_router
from core.endpoints import router as endpoints_router
from core.sandbox import api as sandbox_api
from core.billing.api import router as billing_router
from core.setup import router as setup_router, webhook_router
from core.admin.admin_api import router as admin_router
from core.admin.billing_admin_api import router as billing_admin_router
from core.admin.feedback_admin_api import router as feedback_admin_router
from core.admin.notification_admin_api import router as notification_admin_router
from core.admin.analytics_admin_api import router as analytics_admin_router
from core.admin.stress_test_admin_api import router as stress_test_admin_router
from core.admin.system_status_admin_api import router as system_status_admin_router
from core.admin.sandbox_pool_admin_api import router as sandbox_pool_admin_router
from core.endpoints.system_status_api import router as system_status_router
from core.services import transcription as transcription_api
from core.triggers import api as triggers_api
from core.services import api_keys_api
from core.notifications import api as notifications_api
from core.services.orphan_cleanup import cleanup_orphaned_agent_runs
from auth import api as auth_api
from core.utils.auth_utils import verify_and_get_user_id_from_jwt

# Additional Routers
from core.mcp_module import api as mcp_api
from core.credentials import api as credentials_api
from core.templates import api as template_api
from core.templates import presentations_api
from core.services import voice_generation as voice_api
from core.knowledge_base import api as knowledge_base_api
from core.notifications import presence_api
from core.composio_integration import api as composio_api
from core.google.google_slides_api import router as google_slides_router
from core.google.google_docs_api import router as google_docs_router
from core.referrals import router as referrals_router
from core.memory.api import router as memory_router
from core.test_harness.api import router as test_harness_router, e2e_router
from core.sandbox.canvas_ai_api import router as canvas_ai_router
from core.admin.stateless_admin_api import router as stateless_admin_router

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

db = DBConnection()
from core.utils.instance import get_instance_id, INSTANCE_ID
instance_id = INSTANCE_ID

_worker_metrics_task = None
_memory_watchdog_task = None
_stream_cleanup_task = None
_is_shutting_down = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_metrics_task, _memory_watchdog_task, _stream_cleanup_task, _is_shutting_down
    try:
        await db.initialize()
        from core.services.db import init_db
        await init_db()
        try:
            from core.services.db import execute_one
            await execute_one("SELECT 1", {})
        except Exception: pass
        
        from core.utils.tool_discovery import warm_up_tools_cache
        warm_up_tools_cache()
        from core.cache.runtime_cache import load_static_suna_config
        load_static_suna_config()
        sandbox_api.initialize(db)
        
        try:
            await redis.initialize_async()
        except Exception: pass

        try:
            client = await db.client
            await cleanup_orphaned_agent_runs(client)
        except Exception: pass
        
        triggers_api.initialize(db)
        credentials_api.initialize(db)
        template_api.initialize(db)
        composio_api.initialize(db)
        
        if config.ENV_MODE == EnvMode.PRODUCTION:
            from core.services import worker_metrics
            _worker_metrics_task = asyncio.create_task(worker_metrics.start_cloudwatch_publisher())
        
        from core.services import worker_metrics
        _stream_cleanup_task = asyncio.create_task(worker_metrics.start_stream_cleanup_task())
        _memory_watchdog_task = asyncio.create_task(_memory_watchdog())
        
        from core.sandbox.pool_background import start_pool_service
        asyncio.create_task(start_pool_service())

        from core.agents.pipeline.stateless import lifecycle
        await lifecycle.initialize()
        
        yield

        _is_shutting_down = True
        await asyncio.sleep(2)
        
        if _worker_metrics_task: _worker_metrics_task.cancel()
        if _memory_watchdog_task: _memory_watchdog_task.cancel()
        
        try: await redis.close()
        except Exception: pass
        await db.disconnect()
        from core.services.db import close_db
        await close_db()
    except Exception as e:
        logger.error(f"Error startup: {e}")
        raise

app = FastAPI(lifespan=lifespan)
configure_openapi(app)

# 🛠️ Middleware to fix Singular/Plural Path Mismatch (The Fix for 404s)
@app.middleware("http")
async def fix_paths_middleware(request: Request, call_next):
    path = request.url.path
    # Rewrite singular requests to plural
    if path.startswith("/v1/agent-run/"):
        request.scope["path"] = path.replace("/v1/agent-run/", "/v1/agent-runs/")
    elif path.startswith("/v1/thread/"):
        request.scope["path"] = path.replace("/v1/thread/", "/v1/threads/")
    
    response = await call_next(request)
    return response

@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    request_id = str(uuid.uuid4())
    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        # Only log errors or slow requests to reduce noise
        if response.status_code >= 400:
             logger.error(f"{request.method} {request.url.path} | Status: {response.status_code} | Time: {process_time:.2f}s")
        return response
    except Exception as e:
        logger.error(f"Request failed: {str(e)}")
        raise

# CORS
allowed_origins = ["*"] # Allow all for debugging
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_router = APIRouter()
api_router.include_router(versioning_router)
api_router.include_router(agent_runs_router)
api_router.include_router(agent_crud_router)
api_router.include_router(agent_tools_router)
api_router.include_router(agent_json_router)
api_router.include_router(agent_setup_router)
api_router.include_router(threads_router)
api_router.include_router(categorization_router)
api_router.include_router(endpoints_router)
api_router.include_router(sandbox_api.router)
api_router.include_router(billing_router)
api_router.include_router(setup_router)
api_router.include_router(webhook_router)
api_router.include_router(api_keys_api.router)
api_router.include_router(billing_admin_router)
api_router.include_router(admin_router)
api_router.include_router(feedback_admin_router)
api_router.include_router(notification_admin_router)
api_router.include_router(analytics_admin_router)
api_router.include_router(stress_test_admin_router)
api_router.include_router(system_status_admin_router)
api_router.include_router(sandbox_pool_admin_router)
api_router.include_router(system_status_router)
api_router.include_router(mcp_api.router)
api_router.include_router(credentials_api.router, prefix="/secure-mcp")
api_router.include_router(template_api.router, prefix="/templates")
api_router.include_router(presentations_api.router, prefix="/presentation-templates")
api_router.include_router(transcription_api.router)
api_router.include_router(voice_api.router)
api_router.include_router(knowledge_base_api.router)
api_router.include_router(triggers_api.router)
api_router.include_router(notifications_api.router)
api_router.include_router(presence_api.router)
api_router.include_router(composio_api.router)
api_router.include_router(google_slides_router)
api_router.include_router(google_docs_router)
api_router.include_router(referrals_router)
api_router.include_router(memory_router)
api_router.include_router(test_harness_router)
api_router.include_router(e2e_router)
api_router.include_router(canvas_ai_router)
api_router.include_router(stateless_admin_router)
api_router.include_router(auth_api.router)

@api_router.get("/health", tags=["system"])
async def health_check():
    return {"status": "ok", "instance_id": instance_id}

@api_router.post("/prewarm", tags=["system"])
async def prewarm_user_caches(user_id: str = Depends(verify_and_get_user_id_from_jwt)):
    return {"status": "accepted"}

@api_router.get("/metrics", tags=["system"])
async def metrics_endpoint():
    from core.services import worker_metrics
    return await worker_metrics.get_worker_metrics()

app.include_router(api_router, prefix="/v1")

async def _memory_watchdog():
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError: pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=7860, workers=1, loop="asyncio")
