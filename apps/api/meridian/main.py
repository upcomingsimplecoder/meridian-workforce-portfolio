from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from meridian.analytics import router as analytics_router
from meridian.compensation import router as compensation_router
from meridian.errors import DomainError, problem_response
from meridian.finance_engine import FinanceInputError
from meridian.http_boundary import HttpBoundary
from meridian.identity import DB, CurrentActor
from meridian.identity import router as identity_router
from meridian.ingestion import router as ingestion_router
from meridian.models import ConsumerReceipt, OutboxEvent, Principal, WorkerHeartbeat, now
from meridian.operations import router as operations_router
from meridian.people import router as people_router
from meridian.replay import router as replay_router
from meridian.scenarios import router as scenarios_router
from meridian.schemas import Health, Overview, Problem
from meridian.talent import router as talent_router

# Pinned to this application's required schema, checked against Alembic in tests.
SUPPORTED_SCHEMA_REVISION = "0011"

app = FastAPI(
    title="Meridian Workforce API",
    version="0.1.0",
    responses={
        401: {"model": Problem},
        403: {"model": Problem},
        409: {"model": Problem},
        422: {"model": Problem},
    },
)
app.include_router(identity_router)
app.include_router(people_router)
app.include_router(ingestion_router)
app.include_router(scenarios_router)
app.include_router(compensation_router)
app.include_router(talent_router)
app.include_router(analytics_router)
app.include_router(replay_router)
app.include_router(operations_router)
app.add_middleware(HttpBoundary)


@app.exception_handler(DomainError)
async def domain_error(request: Request, error: DomainError) -> Response:
    return problem_response(request, error.status, error.code, error.detail)


@app.exception_handler(FinanceInputError)
async def finance_error(request: Request, error: FinanceInputError) -> Response:
    return problem_response(
        request, 422, "financial_input_invalid", f"Calculation refused: {str(error)}."
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError) -> Response:
    # Never reflect request values or raw Pydantic error input into telemetry or responses.
    return problem_response(
        request,
        422,
        "invalid_request",
        "Input failed schema validation. Check required fields, types and limits.",
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException) -> Response:
    return problem_response(
        request, error.status_code, "http_error", "The requested operation is unavailable."
    )


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", service="meridian-api")


@app.get("/ready", response_model=Health)
def ready(db: DB) -> Health:
    try:
        markers = list(db.scalars(text("SELECT version_num FROM alembic_version")))
        if markers != [SUPPORTED_SCHEMA_REVISION]:
            raise DomainError(
                503, "schema_unavailable", "Database schema is incompatible with this application."
            )
        return Health(status="ready", service="meridian-api")
    except SQLAlchemyError as exc:
        raise DomainError(
            503, "database_unavailable", "Database or migrations are unavailable."
        ) from exc


@app.get("/api/v1/overview", response_model=Overview)
def overview(current: CurrentActor, db: DB) -> Overview:
    heartbeat = db.get(WorkerHeartbeat, "outbox")
    return Overview(
        as_of=now(),
        source="PostgreSQL / live query",
        demo_accounts=db.scalar(select(func.count()).select_from(Principal)) or 0,
        pending_events=db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.state != "DONE")
        )
        or 0,
        processed_events=db.scalar(select(func.count()).select_from(ConsumerReceipt)) or 0,
        worker_last_seen=heartbeat.last_seen_at if heartbeat else None,
        seed_version="synthetic-v1",
        phase="Workforce planning and review",
    )
