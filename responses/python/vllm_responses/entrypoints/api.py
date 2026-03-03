import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from gunicorn.app.base import BaseApplication
from loguru import logger

from vllm_responses.configs import ENV_CONFIG
from vllm_responses.entrypoints.state import VRAppState, VRRequestState
from vllm_responses.mcp.runtime_client import BuiltinMcpRuntimeClient
from vllm_responses.observability.metrics import install_prometheus_metrics
from vllm_responses.observability.tracing import configure_tracing
from vllm_responses.responses_core.store import get_default_response_store
from vllm_responses.routers import (
    mcp,
    serving,
)
from vllm_responses.tools.code_interpreter import start_server
from vllm_responses.types.api import UserAgent
from vllm_responses.utils import uuid7_str
from vllm_responses.utils.exceptions import VRException
from vllm_responses.utils.handlers import (
    exception_handler,
    make_request_log_str,
    path_not_found_handler,
)
from vllm_responses.utils.io import HTTP_ACLIENT
from vllm_responses.utils.logging import setup_logger_sinks, suppress_logging_handlers

OVERHEAD_LOG_ROUTES = {r.path for r in serving.router.routes}
services = [
    (serving.router, ["Serving"], ""),
    (mcp.router, ["MCP"], ""),
]

# Setup logging
setup_logger_sinks(None)
suppress_logging_handlers(["uvicorn", "litellm", "pottery"], True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info(f"Using configuration: {ENV_CONFIG}")

    tracing_shutdown = configure_tracing(app)

    # Ensure the ResponseStore schema exists.
    #
    # Multi-worker policy:
    # - `vllm-responses serve` initializes the schema once in the supervisor and sets `VR_DB_SCHEMA_READY=1`
    #   for all workers. In that mode this call is a cheap no-op.
    # - If you start the gateway without `vllm-responses serve` and set `VR_WORKERS > 1` with SQLite,
    #   schema init is not safe (race) and `ensure_schema()` will raise with guidance.
    await get_default_response_store().ensure_schema()

    app.state.vllm_responses.builtin_mcp_runtime_client = None
    if (
        ENV_CONFIG.mcp_builtin_runtime_url is not None
        and ENV_CONFIG.mcp_builtin_runtime_url.strip()
    ):
        app.state.vllm_responses.builtin_mcp_runtime_client = BuiltinMcpRuntimeClient(
            base_url=ENV_CONFIG.mcp_builtin_runtime_url.strip(),
        )

    if ENV_CONFIG.code_interpreter_mode == "spawn":
        if ENV_CONFIG.workers > 1:
            raise RuntimeError(
                "VR_CODE_INTERPRETER_MODE=spawn is not allowed when VR_WORKERS > 1. "
                "Use VR_CODE_INTERPRETER_MODE=external (recommended with Gunicorn), "
                "or run `vllm-responses serve` to supervise a single shared code-interpreter process."
            )
        app.state.vllm_responses.code_interpreter_process = await start_server(
            port=ENV_CONFIG.code_interpreter_port,
            workers=ENV_CONFIG.code_interpreter_workers,
        )

    yield
    logger.info("Shutting down...")

    tracing_shutdown()

    # Shutdown code interpreter server
    code_interpreter_process = app.state.vllm_responses.code_interpreter_process
    if code_interpreter_process:
        logger.info("Stopping code interpreter server...")
        try:
            code_interpreter_process.terminate()
            await asyncio.wait_for(code_interpreter_process.wait(), timeout=10.0)
            logger.info("Code interpreter server stopped.")
        except asyncio.TimeoutError:
            logger.warning("Code interpreter server did not stop gracefully, forcing kill...")
            code_interpreter_process.kill()
            await code_interpreter_process.wait()
        except Exception as e:
            logger.warning(f"Error stopping code interpreter server: {repr(e)}")

    runtime_client = app.state.vllm_responses.builtin_mcp_runtime_client
    if runtime_client is not None:
        try:
            await runtime_client.aclose()
        except Exception as e:
            logger.warning(f"Error closing Built-in MCP runtime client: {repr(e)}")

    # Close DB connection
    # NOTE: the DB engine is cached for the process lifetime; explicit disposal is not required here.

    # Close HTTPX client
    await HTTP_ACLIENT.aclose()
    # Ensure Loguru's background queue (enqueue=True) is fully drained before process exit.
    # Without this, interactive `Ctrl+C` shutdown can require a second interrupt.
    try:
        logger.complete()
    except Exception as e:
        logger.warning(f"Failed to flush logger queue: {repr(e)}")
    logger.info("Shutdown complete.")


app = FastAPI(
    title="TokenVisor API",
    logger=logger,
    default_response_class=ORJSONResponse,  # Should be faster
    openapi_url="/public/openapi.json",
    docs_url="/public/docs",
    redoc_url="/public/redoc",
    # license_info={
    #     "name": "Apache 2.0",
    #     "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    # },
    # servers=[dict(url="https://api.jamaibase.com")],
    lifespan=lifespan,
)
app.state.vllm_responses = VRAppState()

install_prometheus_metrics(app)


# Mount
for router, tags, prefix in services:
    app.include_router(
        router,
        prefix=prefix,
        tags=tags,
    )

# Permissive CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_exception_handler(VRException, exception_handler)  # Suppress starlette traceback
app.add_exception_handler(Exception, exception_handler)
app.add_exception_handler(404, path_not_found_handler)


@app.middleware("http")
async def log_request(request: Request, call_next):
    """
    Args:
        request (Request): Starlette request object.
        call_next (Callable): A function that will receive the request,
            pass it to the path operation, and returns the response generated.

    Returns:
        response (Response): Response of the path operation.
    """
    request_id = request.headers.get("x-request-id", uuid7_str())
    request.state.vllm_responses = VRRequestState(
        id=request_id,
        user_agent=UserAgent.from_user_agent_string(request.headers.get("user-agent", "")),
        timing=defaultdict(float),
    )

    # Call request
    path = request.url.path
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        logger.info(make_request_log_str(request))
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    if "/health" not in path:
        logger.info(make_request_log_str(request, response.status_code))

    # Process billing (this will run BEFORE any responses are sent)
    if request.state.vllm_responses.billing is not None:
        # Background tasks will run AFTER streaming responses are sent
        tasks = BackgroundTasks()
        tasks.add_task(request.state.vllm_responses.billing.process_all)
        response.background = tasks
    # Log timing
    model_start_time = request.state.vllm_responses.model_start_time
    if (
        ENV_CONFIG.log_timings
        and model_start_time
        and any(p for p in OVERHEAD_LOG_ROUTES if p in path)
    ):
        overhead = model_start_time - request.state.vllm_responses.request_start_time
        breakdown = {
            k: f"{v * 1e3:,.1f} ms" for k, v in request.state.vllm_responses.timing.items()
        }
        logger.info(
            f"{request.state.vllm_responses.id} - Total overhead: {overhead * 1e3:,.1f} ms. Breakdown: {breakdown}"
        )
    return response


@app.get("/health", tags=["Health"])
async def health() -> ORJSONResponse:
    """Health check."""
    return ORJSONResponse(status_code=200, content={})


# Process OpenAPI docs
openapi_schema = app.openapi()
# Add security schemes
openapi_schema["components"]["securitySchemes"] = {
    "Authentication": {"type": "http", "scheme": "bearer"},
}
openapi_schema["security"] = [{"Authentication": []}]
openapi_schema["info"]["x-logo"] = {"url": ""}
app.openapi_schema = openapi_schema


class StandaloneApplication(BaseApplication):
    def __init__(self, app, options=None):
        self.options = options or {}
        self.application = app
        super().__init__()

    def load_config(self):
        config = {
            key: value
            for key, value in self.options.items()
            if key in self.cfg.settings and value is not None
        }
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.application


if __name__ == "__main__":
    options = {
        "bind": f"{ENV_CONFIG.host}:{ENV_CONFIG.port}",
        "workers": ENV_CONFIG.workers,
        "worker_class": "uvicorn.workers.UvicornWorker",
        "limit_concurrency": ENV_CONFIG.max_concurrency,
        "timeout": 600,
        "graceful_timeout": 60,
        "max_requests": 2000,
        "max_requests_jitter": 200,
        "keepalive": 60,  # AWS ALB and Nginx default to 60 seconds
        "loglevel": "error",
    }
    StandaloneApplication(app, options).run()
