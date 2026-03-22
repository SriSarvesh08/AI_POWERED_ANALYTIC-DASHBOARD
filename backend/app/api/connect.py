"""
Database connection endpoints — connect external databases
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models import Dataset
from app.models.schemas import DBConnectRequest, DatasetResponse
from app.services.db_connector import test_connection
from app.services.pipeline_orchestrator import run_full_pipeline

router = APIRouter()


@router.post("/test", status_code=200)
async def test_db_connection(
    body: DBConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Test connectivity before saving — returns success or error detail."""
    ok, message = await test_connection(body)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"status": "ok", "message": message}


@router.post("/connect", response_model=DatasetResponse, status_code=202)
async def connect_database(
    body: DBConnectRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Connect an external database.
    Tests connectivity, creates a dataset record, runs AI pipeline in background.
    """
    ok, message = await test_connection(body)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Connection failed: {message}")

    import json
    # Store the config with the password masked — the actual password is passed
    # directly to the pipeline worker and never persisted in plaintext.
    connection_config = body.model_dump()
    stored_config = {**connection_config, "password": "***REDACTED***"} if connection_config.get("password") else connection_config

    dataset = Dataset(
        user_id=current_user["user_id"],
        name=body.alias,
        source_type=body.db_type,
        source_reference=json.dumps(stored_config),
        status="pending",
    )
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)

    background_tasks.add_task(
        run_full_pipeline,
        dataset_id=str(dataset.id),
        mode="connect",
        source_type=body.db_type,
        connection_config=body.model_dump(),
    )

    return dataset
