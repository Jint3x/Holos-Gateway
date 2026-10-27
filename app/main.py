from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.observability import Telemetry, TelemetrySettings

router = APIRouter()


def create_app(*, telemetry: Telemetry | None = None) -> FastAPI:
    telemetry = telemetry or Telemetry(TelemetrySettings.from_env())

    @asynccontextmanager
    async def lifespan(app):
        telemetry.start_logging()
        try:
            yield
        finally:
            telemetry.shutdown()

    app = FastAPI(
        title="Holos Gateway",
        version="0.1.0",
        description="The public API for Holos Gateway.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.state.telemetry = telemetry
    telemetry.instrument(app)
    return app


class GreetingResponse(BaseModel):
    message: str = Field(description="The greeting text.", examples=["Hello World"])


class ErrorResponse(BaseModel):
    detail: str = Field(
        description="A human-readable explanation of the error.",
        examples=["Name must contain a non-whitespace character."],
    )


@router.get(
    "/",
    response_model=GreetingResponse,
    tags=["Greetings"],
    summary="Return a greeting",
    description=(
        "Returns Hello World by default, or a personalized greeting when name is "
        "provided. Names must contain 1-80 characters before trimming and cannot "
        "consist entirely of whitespace."
    ),
    responses={
        400: {
            "model": ErrorResponse,
            "description": "The name contains only whitespace.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Name must contain a non-whitespace character."
                    }
                }
            },
        },
    },
)
def hello_world(
    name: Annotated[
        str,
        Query(
            min_length=1,
            max_length=80,
            description="Optional name to greet. Surrounding whitespace is trimmed.",
            examples=["World", "Ada"],
        ),
    ] = "World",
) -> GreetingResponse:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise HTTPException(
            status_code=400,
            detail="Name must contain a non-whitespace character.",
        )
    return GreetingResponse(message=f"Hello {cleaned_name}")
