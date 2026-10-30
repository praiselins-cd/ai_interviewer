import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.repositories import ai_interview_repo
from app.infra.redis_client import get_redis

logger = logging.getLogger("app.api.internal")
router = APIRouter(prefix="/internal/v1")

FINALISED_CHANNEL = "interview_session_finalised"


@router.post("/interview-sessions/{session_id}/finalise")
async def finalise_interview_session(session_id: UUID):
    session = await ai_interview_repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown interview_session_id")

    if session["finalised_at"] is not None:
        # Idempotent: already finalised, no-op, no re-publish.
        return {"status": "already_finalised", "interview_session_id": str(session_id)}

    did_finalise = await ai_interview_repo.finalise_session(session_id)
    if not did_finalise:
        # Lost a race with another finalise call — treat as success, don't double-publish.
        return {"status": "already_finalised", "interview_session_id": str(session_id)}

    r = get_redis()
    await r.publish(FINALISED_CHANNEL, json.dumps({"interview_session_id": str(session_id)}))
    logger.info(f"Published {FINALISED_CHANNEL} for session {session_id}")

    return {"status": "finalised", "interview_session_id": str(session_id)}


@router.get("/bedrock/connectivity")
async def check_bedrock_connectivity():
    """Opens a real Bedrock bidirectional stream, sends a minimal sessionStart,
    then immediately closes it. A 200 response means credentials, region, and
    network path to Bedrock are all working. Any failure returns 503."""
    from app.realtime.connection import _build_bedrock_client
    from app.config import get_settings
    import json as _json
    from aws_sdk_bedrock_runtime.client import InvokeModelWithBidirectionalStreamOperationInput
    from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart

    TIMEOUT_SECONDS = 15
    settings = get_settings()
    t0 = time.monotonic()
    client = None

    async def _probe():
        nonlocal client
        client = await _build_bedrock_client()
        stream = await client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=settings.bedrock_model_id)
        )
        payload = _json.dumps({"event": {"sessionStart": {
            "inferenceConfiguration": {"maxTokens": 1, "topP": 0.9, "temperature": 0.7},
        }}}).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload)
        )
        await stream.input_stream.send(chunk)
        # Read one event from the output to confirm the model is actually
        # reachable and enabled — AccessDeniedException surfaces here if
        # the model isn't enabled in this account/region.
        output = await stream.await_output()
        await output[1].receive()
        await stream.input_stream.close()

    try:
        await asyncio.wait_for(_probe(), timeout=TIMEOUT_SECONDS)
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        logger.info(f"Bedrock connectivity check OK ({elapsed_ms}ms).")
        return {
            "status": "ok",
            "model_id": settings.bedrock_model_id,
            "region": settings.aws_region,
            "latency_ms": elapsed_ms,
        }
    except asyncio.TimeoutError:
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        import os
        # The Smithy SDK resolves credentials in a background HTTP task so a
        # missing-creds error surfaces as a timeout rather than a direct exception.
        if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            logger.error("Bedrock connectivity check failed: AWS credentials not set in environment.")
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "error",
                    "model_id": settings.bedrock_model_id,
                    "region": settings.aws_region,
                    "error": "MissingCredentials",
                    "message": "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are not set. Add them to your .env file and restart the server.",
                    "latency_ms": elapsed_ms,
                },
            )
        logger.error(f"Bedrock connectivity check timed out after {TIMEOUT_SECONDS}s.")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "timeout",
                "model_id": settings.bedrock_model_id,
                "region": settings.aws_region,
                "error": "TimeoutError",
                "message": f"No response from Bedrock within {TIMEOUT_SECONDS}s — check network/VPC and that the model is enabled in this region.",
                "latency_ms": elapsed_ms,
            },
        )
    except Exception as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000)
        logger.error(f"Bedrock connectivity check failed ({elapsed_ms}ms): {e}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "model_id": settings.bedrock_model_id,
                "region": settings.aws_region,
                "error": type(e).__name__,
                "message": str(e),
                "latency_ms": elapsed_ms,
            },
        )
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass
