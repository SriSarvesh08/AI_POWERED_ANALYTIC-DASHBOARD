"""
Pipeline orchestrator — the core AI-driven pipeline:
  1. Ingest data (file or DB)
  2. Extract schema
  3. Gemini AI schema analysis
  4. AI data selection (only meaningful columns/rows)
  5. Push to Apache Superset (create dataset + charts + dashboard)
  6. Build RAG embeddings for chatbot

This runs as a background task — dataset.status is updated at each step.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import settings
from app.models import Dataset, Chart, ChunkEmbedding, PipelineRun

logger = logging.getLogger(__name__)

_engine = create_async_engine(settings.DATABASE_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def run_full_pipeline(
    dataset_id: str,
    mode: str,  # "upload" | "connect"
    source_path: Optional[str] = None,
    source_type: Optional[str] = None,
    connection_config: Optional[Dict[str, Any]] = None,
):
    """Entry point called from background_tasks."""
    current_step = "unknown"
    async with _Session() as db:
        try:
            # Wrap _start_step locally to track which step is currently running.
            # This avoids any global mutation — each pipeline run tracks its own step.
            _original_start_step = _start_step

            async def _tracked_start(db, ds_id, step):
                nonlocal current_step
                current_step = step
                await _original_start_step(db, ds_id, step)

            # Patch only within this call's scope by passing the wrapper through _run
            await _run(db, dataset_id, mode, source_path, source_type,
                       connection_config, _start_step_fn=_tracked_start)

        except Exception as exc:
            logger.exception("Pipeline failed for dataset %s at step %s: %s", dataset_id, current_step, exc)
            await _finish_step(db, uuid.UUID(dataset_id), current_step, detail=str(exc), status="error")
            await _set_status(db, dataset_id, "error", error_message=str(exc))



async def _run(
    db: AsyncSession,
    dataset_id: str,
    mode: str,
    source_path,
    source_type,
    connection_config,
    _start_step_fn=None,   # optional per-invocation override for step tracking
):
    ds_id = uuid.UUID(str(dataset_id))
    # Use the injected tracker if provided, otherwise fall back to the module-level helper
    _step_start = _start_step_fn or _start_step


    # ── Step 1 — Ingest ───────────────────────────────────────────────────────
    await _step_start(db, ds_id, "ingest")
    warehouse_table = None
    raw_schema = None

    if mode == "upload":
        from app.services.ingestion import IngestionService
        svc = IngestionService()
        warehouse_table, row_count, col_count, raw_schema = await svc.ingest_file(
            source_path, source_type, dataset_id
        )
        await _update_dataset(db, ds_id, {
            "warehouse_table": warehouse_table,
            "row_count": row_count,
            "column_count": col_count,
            "raw_schema": raw_schema,
            "status": "ingested",
        })
    else:  # connect
        from app.services.db_connector import ExternalDBConnector
        from app.models.schemas import DBConnectRequest
        cfg = DBConnectRequest(**connection_config)
        connector = ExternalDBConnector(cfg)
        raw_schema = await connector.extract_schema()
        # Count tables/columns
        tables = raw_schema.get("tables", [])
        col_count = sum(len(t.get("columns", [])) for t in tables)
        await _update_dataset(db, ds_id, {
            "raw_schema": raw_schema,
            "column_count": col_count,
            "status": "ingested",
        })

    await _finish_step(db, ds_id, "ingest", f"Schema extracted — {col_count} columns")

    # ── Step 2 — Schema extraction ────────────────────────────────────────────
    await _step_start(db, ds_id, "schema_extract")
    await _finish_step(db, ds_id, "schema_extract", "Schema mapped successfully")

    # ── Step 3 — Gemini AI schema analysis ───────────────────────────────────
    await _step_start(db, ds_id, "ai_schema_analysis")
    await _set_status(db, ds_id, "ai_analyzing")

    dataset = await _get_dataset(db, ds_id)
    if dataset.ai_schema:
        logger.info(f"Reusing existing AI schema analysis for dataset {ds_id}")
        ai_schema = dataset.ai_schema
    else:
        from app.services.gemini_service import GeminiService
        gemini = GeminiService()
        ai_schema = await gemini.analyze_schema(raw_schema)
        await _update_dataset(db, ds_id, {"ai_schema": ai_schema})

    await _set_status(db, ds_id, "ai_analyzing")
    await _finish_step(db, ds_id, "ai_schema_analysis",
                       f"AI selected {len(ai_schema.get('selected_columns', []))} columns, "
                       f"suggesting {len(ai_schema.get('suggested_charts', []))} charts")

    # ── Step 4 — AI data selection ────────────────────────────────────────────
    await _step_start(db, ds_id, "ai_data_selection")
    await _set_status(db, ds_id, "ai_done")

    selected_cols = [
        c["name"] for c in ai_schema.get("selected_columns", []) if c["role"] != "skip"
    ]
    suggested_charts = ai_schema.get("suggested_charts", [])

    if not selected_cols or not suggested_charts:
        await _finish_step(db, ds_id, "ai_data_selection", "No meaningful columns/charts selected", status="error")
        await _set_status(db, ds_id, "error", error_message="AI: No meaningful data found for visualization")
        return

    await _finish_step(db, ds_id, "ai_data_selection",
                       f"{len(selected_cols)} columns selected, {len(suggested_charts)} charts queued")

    # ── Step 5 — Superset push ────────────────────────────────────────────────
    await _step_start(db, ds_id, "superset_push")

    from app.services.superset_client import SupersetClient
    superset = SupersetClient()

    sync_db_uri = settings.DATABASE_URL.replace("+asyncpg", "+psycopg2")
    db_id = await superset.get_or_create_database(sync_db_uri)

    # For file uploads: use the warehouse table; for DB connects: create a virtual view
    logger.info(f"Pushing to Superset. Mode: {mode}, source_type: {source_type}")
    if mode == "upload" and warehouse_table:
        ss_dataset_id = await superset.create_dataset(db_id, warehouse_table)
        table_ref = warehouse_table
    else:
        # For external DB: create a Superset DB connection for the external DB, use first table
        tables = raw_schema.get("tables", [])
        if not tables:
            raise ValueError("No tables found in connected database")
        table_ref = tables[0]["name"]
        
        target_db_id = db_id
        logger.info(f"Targeting Superset DB {target_db_id}. Model source_type: {source_type}")
        
        if source_type == "firebase" or raw_schema.get("db_type") == "firebase":
            # Superset lacks a native Firebase connector; we ingest into PostgreSQL dynamically
            import json, firebase_admin
            from firebase_admin import credentials, firestore
            import pandas as pd
            from app.services.ingestion import IngestionService
            
            try:
                # Get the collection name from raw_schema (usually the first one)
                tables = raw_schema.get("tables", [])
                if not tables:
                     raise ValueError("No collections found in Firebase")
                collection_name = tables[0]["name"]
                
                logger.info(f"Fetching Firebase data for collection '{collection_name}'...")
                sa_info = json.loads(connection_config["service_account_json"])
                cred = credentials.Certificate(sa_info)
                app_name = f"dp_ingest_{str(ds_id).replace('-', '')}"
                try:
                    fb_app = firebase_admin.get_app(app_name)
                except ValueError:
                    fb_app = firebase_admin.initialize_app(cred, name=app_name)
                    
                fb_db = firestore.client(fb_app)
                docs = list(fb_db.collection(collection_name).stream())
                if not docs:
                    raise ValueError(f"Firebase collection '{collection_name}' is empty.")
                    
                data = [doc.to_dict() for doc in docs]
                logger.info(f"Retrieved {len(data)} documents from Firebase.")
                
                # Flatten complex objects for pandas SQL save
                for row in data:
                    for k, v in row.items():
                        if isinstance(v, (dict, list)):
                            row[k] = str(v)
                            
                df = pd.DataFrame(data)
                svc = IngestionService()
                warehouse_table = svc._make_table_name(str(ds_id))
                logger.info(f"Ingesting {len(df)} rows into warehouse table '{warehouse_table}'...")
                await svc._create_table_and_insert(df, warehouse_table)
                
                # IMPORTANT: Use the warehouse table name in Superset, NOT the collection name
                table_ref = warehouse_table
                await _update_dataset(db, ds_id, {"warehouse_table": warehouse_table})
                logger.info(f"Ingestion complete. Superset table_ref: {table_ref}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise ValueError(f"Failed to ingest Firebase data: {str(e)}")
        
        else:
            # For SQL DBs, we Use the actual table name from the connected DB
            tables = raw_schema.get("tables", [])
            if not tables:
                raise ValueError("No tables found in connected database")
            table_ref = tables[0]["name"]
            
            if source_type in ("postgresql", "mysql", "snowflake"):
                cfg = connection_config
                if source_type == "postgresql":
                    uri = f"postgresql+psycopg2://{cfg['username']}:{cfg['password']}@{cfg['host']}:{cfg.get('port', 5432)}/{cfg['database']}"
                elif source_type == "mysql":
                    uri = f"mysql+pymysql://{cfg['username']}:{cfg['password']}@{cfg['host']}:{cfg.get('port', 3306)}/{cfg['database']}"
                elif source_type == "snowflake":
                    schema_str = cfg.get("schema", "PUBLIC")
                    wh_str = cfg.get("warehouse", "")
                    uri = f"snowflake://{cfg['username']}:{cfg['password']}@{cfg['account']}/{cfg['database']}/{schema_str}?warehouse={wh_str}"
                
                logger.info(f"Registering external {source_type} DB with Superset...")
                target_db_id = await superset.get_or_create_database(uri, db_name=f"Ext_{str(ds_id)[:8]}")
                logger.info(f"External DB registered with ID: {target_db_id}")

        logger.info(f"Creating Superset dataset: DB={target_db_id}, table={table_ref}")
        ss_dataset_id = await superset.create_dataset(target_db_id, table_ref)

    chart_ids = []
    # Quote table name for SQL
    quoted_table = f'"{table_ref}"'
    
    for chart_spec in suggested_charts:
        original_table = ""
        tables_list = raw_schema.get("tables", [])
        if tables_list:
            original_table = tables_list[0].get("name", "")
        elif raw_schema.get("filename"):
            original_table = raw_schema.get("filename", "").split(".")[0]
        
        sql = chart_spec.get("sql", "").replace("{{table}}", quoted_table).replace("{table}", quoted_table)
        if original_table:
            # Aggressive string replacements for hallucinated tables, ignoring quotes 
            sql = sql.replace(f'"{original_table}"', quoted_table).replace(original_table, quoted_table)
        y_col = chart_spec.get("y_column")
        x_col = chart_spec.get("x_column")
        quoted_y = f'"{y_col}"' if y_col else None

        # ── Quote all known column names in the AI-generated SQL ──────────
        # This prevents postgres syntax errors from mixed-case or keyword columns.
        import re
        all_col_names = set()
        for t in raw_schema.get("tables", []):
            for c in t.get("columns", []):
                all_col_names.add(c.get("name", c.get("column_name", "")))
        for c in raw_schema.get("columns", []):
            all_col_names.add(c.get("name", c.get("column_name", "")))
        all_col_names.discard("")

        # Sort longest first to avoid partial-name collisions
        for col_name in sorted(all_col_names, key=len, reverse=True):
            pattern = r'(?<!")(?<!\w)' + re.escape(col_name) + r'(?!\w)(?!")'
            sql = re.sub(pattern, f'"{col_name}"', sql)
        
        # Aggregate all columns for quick lookup
        # Handle both file uploads (columns at root) and DB connects (nested in tables)
        temp_list = []
        if "tables" in raw_schema:
            for t in raw_schema.get("tables", []):
                temp_list.extend(t.get("columns", []))
        if "columns" in raw_schema:
            temp_list.extend(raw_schema.get("columns", []))
            
        all_cols = []
        for c in temp_list:
            col_entry = dict(c)
            # Normalize field names
            if "column_name" in col_entry and "name" not in col_entry:
                col_entry["name"] = col_entry["column_name"]
            if "data_type" in col_entry and "type" not in col_entry:
                col_entry["type"] = col_entry["data_type"]
                
            # Map various dtype strings to standard SQL-like categories
            dtype = str(col_entry.get("dtype", col_entry.get("type", ""))).lower()
            if any(kw in dtype for kw in ["int", "long", "number"]):
                col_entry["type"] = "INTEGER"
            elif any(kw in dtype for kw in ["float", "double", "decimal", "numeric", "real"]):
                col_entry["type"] = "FLOAT"
            elif any(kw in dtype for kw in ["datetime", "timestamp", "time"]):
                col_entry["type"] = "TIMESTAMP"
            elif "date" in dtype:
                col_entry["type"] = "DATE"
            elif "bool" in dtype:
                col_entry["type"] = "BOOLEAN"
            else:
                # Default to whatever it had, or TEXT
                col_entry["type"] = col_entry.get("type", "TEXT")
            all_cols.append(col_entry)
            
        # Determine X and Y types
        x_col_info = next((c for c in all_cols if c.get("name") == x_col), None)
        target_col_info = next((c for c in all_cols if c.get("name") == y_col), None)
        
        is_x_date = False
        if x_col_info:
            x_type = str(x_col_info.get("type", "")).upper()
            if any(kw in x_type for kw in ["DATE", "TIME", "TIMESTAMP"]):
                is_x_date = True
        
        is_y_numeric = False
        if target_col_info:
            y_type = str(target_col_info.get("type", "")).upper()
            if any(kw in y_type for kw in ["INT", "FLOAT", "DECIMAL", "NUMERIC", "DOUBLE", "REAL", "BIGINT"]):
                is_y_numeric = True

        # --- Smart Viz Selection ---
        # Use CATEGORICAL chart types by default (they don't need datetime).
        # Only use echarts_timeseries_* when x-axis is a confirmed date column.
        ai_chart_type = chart_spec.get("chart_type", "bar")
        
        if is_x_date:
            # Date-based X axis → use ECharts time-series variants
            type_map = {
                "bar": "echarts_timeseries_bar",
                "line": "echarts_timeseries_line",
                "area": "echarts_timeseries",
                "pie": "pie",
                "scatter": "echarts_scatter",
                "big_number": "big_number_total",
                "table": "table",
            }
        else:
            # Non-date X axis → use categorical variants that NEVER need datetime
            # Superset 'line' and 'area' ALWAYS require datetime. Force them to 'dist_bar'.
            type_map = {
                "bar": "dist_bar",
                "line": "dist_bar",
                "area": "dist_bar",
                "pie": "pie",
                "scatter": "echarts_scatter",
                "big_number": "big_number_total",
                "table": "table",
            }
        viz_type = type_map.get(ai_chart_type, "dist_bar")

        # Force valid aggregation if AI hallucinates "None"
        raw_agg = chart_spec.get("aggregation", "")
        if not raw_agg or raw_agg.upper() == "NONE":
            agg_func = "SUM" if is_y_numeric else "COUNT"
        else:
            agg_func = raw_agg.upper()
            
        sanitized_label = "".join(c if c.isalnum() or c == " " else "_" for c in (y_col or "Value")).strip()
        option_name = f"metric_{uuid.uuid4().hex[:8]}"
        
        # EXTRA SAFETY: Handle Postgres COUNT(DISTINCT ...) syntax and Hallucinated Aggregations
        if agg_func == "COUNT_DISTINCT":
            sql_expr = f"COUNT(DISTINCT {quoted_y})" if quoted_y else "COUNT(*)"
            label_text = f"Unique {sanitized_label}"
        elif not is_y_numeric and agg_func in ("AVG", "SUM", "MIN", "MAX"):
            logger.warning(f"AI requested {agg_func} on non-numeric column {y_col}. Falling back to COUNT.")
            agg_func = "COUNT"
            sql_expr = f"COUNT({quoted_y})" if quoted_y else "COUNT(*)"
            label_text = f"Count of {sanitized_label}"
        else:
            sql_expr = f"{agg_func}({quoted_y})" if quoted_y else "COUNT(*)"
            label_text = f"{agg_func} of {sanitized_label}" if quoted_y else "Count"

        metric_obj = {
            "expressionType": "SQL",
            "sqlExpression": sql_expr,
            "label": label_text,
            "hasCustomLabel": True,
            "optionName": option_name,
        }


        
        # If there is no x_col, we CANNOT draw a 2D chart (bar, pie, line). Force big_number_total.
        if not x_col and viz_type != "table":
            viz_type = "big_number_total"
        
        # --- Build chart_params per viz_type ---
        chart_params = {
            "viz_type": viz_type,
            "adhoc_filters": [],
            "row_limit": 1000,
            "show_legend": True,
            "rich_tooltip": True,
            "color_scheme": "supersetColors",
            "y_axis_format": "SMART_NUMBER",
        }
        
        if viz_type == "dist_bar":
            # Categorical bar: needs groupby + metrics
            chart_params["groupby"] = [x_col] if x_col else []
            chart_params["metrics"] = [metric_obj]
            
        elif viz_type == "pie":
            # Pie: needs groupby + metric (singular)
            chart_params["groupby"] = [x_col] if x_col else []
            chart_params["metric"] = metric_obj
            chart_params["metrics"] = [metric_obj]
            
        elif viz_type in ("line", "area"):
            # Universal fallback for purely categorical non-temporal lines in Superset 3.1.1 w/o generic axes flags config
            chart_params["viz_type"] = "dist_bar"
            chart_params["groupby"] = [x_col] if x_col else []
            chart_params["metrics"] = [metric_obj]
            
        elif viz_type == "echarts_scatter":
            # ECharts scatter: needs x_axis and y_axis in superset 3.x
            chart_params["x_axis"] = x_col if x_col else None
            chart_params["y_axis"] = y_col if y_col else None
            chart_params["metrics"] = [metric_obj]
            chart_params["groupby"] = [x_col] if x_col else []
            
        elif viz_type == "big_number_total":
            chart_params["metric"] = metric_obj
            chart_params["metrics"] = [metric_obj]
            
        elif viz_type == "table":
            chart_params["all_columns"] = [x_col, y_col] if x_col and y_col else [x_col] if x_col else []
            chart_params["metrics"] = []
            
        elif viz_type.startswith("echarts_timeseries"):
            # ECharts time-series: use x_axis (GENERIC_CHART_AXES) — works with any column type
            chart_params["x_axis"] = x_col
            chart_params["metrics"] = [metric_obj]
            chart_params["groupby"] = []  # no groupby for timeseries
            
        else:
            # Fallback
            chart_params["groupby"] = [x_col] if x_col else []
            chart_params["metrics"] = [metric_obj]
        
        ss_chart_id = await superset.create_chart(
            datasource_id=ss_dataset_id,
            title=chart_spec["title"],
            chart_type=viz_type,
            params=chart_params,
        )
        chart_ids.append(ss_chart_id)



        # Save chart record
        chart = Chart(
            dataset_id=ds_id,
            user_id=(await _get_dataset(db, ds_id)).user_id,
            superset_chart_id=ss_chart_id,
            title=chart_spec["title"],
            chart_type=chart_spec["chart_type"],
            sql_query=sql,
            ai_reasoning=chart_spec.get("reasoning", ""),
        )
        db.add(chart)

    # Create dashboard
    ds_name = (await _get_dataset(db, ds_id)).name
    ss_dashboard_id = await superset.create_dashboard(
        title=f"AI Dashboard — {ds_name}",
        chart_ids=chart_ids,
    )

    await db.commit()

    await _update_dataset(db, ds_id, {
        "superset_dataset_id": ss_dataset_id,
        "superset_dashboard_id": ss_dashboard_id,
        "status": "superset_ready",
    })
    await _finish_step(db, ds_id, "superset_push",
                       f"{len(chart_ids)} charts created, dashboard ID {ss_dashboard_id}")

    # ── Step 6 — RAG embedding ────────────────────────────────────────────────
    await _step_start(db, ds_id, "rag_embed")
    await _build_rag_embeddings(db, ds_id, ai_schema, table_ref if mode == "upload" else table_ref)
    await _finish_step(db, ds_id, "rag_embed", "RAG embeddings built successfully")


# ── RAG embedding builder ─────────────────────────────────────────────────────

async def _build_rag_embeddings(
    db: AsyncSession, dataset_id: uuid.UUID, ai_schema: Dict, table_name: str
):
    """
    Sample data rows and schema info, chunk them, embed with Gemini, store.
    """
    from app.services.gemini_service import GeminiService
    import sqlalchemy as sa

    gemini = GeminiService()

    # Build text chunks from schema info
    chunks = []

    # Chunk 1: data summary
    summary = ai_schema.get("data_summary", "")
    if summary:
        chunks.append(f"Data summary: {summary}")

    # Chunk 2: column descriptions
    for col in ai_schema.get("selected_columns", []):
        if col["role"] != "skip":
            chunks.append(
                f"Column '{col['name']}' ({col['sql_type']}) — role: {col['role']}. {col.get('reason', '')}"
            )

    # Chunk 3-N: sample data rows from warehouse
    if table_name:
        try:
            sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
            import sqlalchemy as sa_sync
            sync_engine = sa_sync.create_engine(sync_url)
            with sync_engine.connect() as conn:
                rows = conn.execute(sa_sync.text(f'SELECT * FROM "{table_name}" LIMIT 200')).fetchall()
                keys = conn.execute(sa_sync.text(f'SELECT * FROM "{table_name}" LIMIT 1')).keys()
                col_names = list(keys)
                
                # BATCH rows securely into larger chunks to save API calls! (Max ~5 requests instead of 200)
                current_batch = []
                for row in rows:
                    row_str = " | ".join(f"{col_names[i]}: {v}" for i, v in enumerate(row))
                    current_batch.append(row_str)
                    
                    if len(current_batch) >= 25:
                        chunks.append("\n".join(current_batch))
                        current_batch = []
                
                if current_batch:
                    chunks.append("\n".join(current_batch))
                    
            sync_engine.dispose()
        except Exception as e:
            logger.warning("Could not sample rows for RAG: %s", e)

    # Embed and store (batch sequentially with slight delay to respect free-tier quotas)
    import asyncio
    for i, chunk in enumerate(chunks):
        try:
            if i > 0 and i % 5 == 0:
                await asyncio.sleep(2)  # Pause every 5 requests to avoid RPM spikes
                
            embedding = await gemini.embed_text(chunk[:4000])  # slightly larger truncate limit
            emb = ChunkEmbedding(
                dataset_id=dataset_id,
                chunk_text=chunk[:4000],
                embedding=embedding,
                chunk_index=i,
            )
            db.add(emb)
        except Exception as e:
            logger.warning("Embedding failed for chunk %d: %s", i, e)

    await db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_dataset(db: AsyncSession, ds_id: uuid.UUID) -> Dataset:
    result = await db.execute(select(Dataset).where(Dataset.id == ds_id))
    return result.scalar_one()


async def _update_dataset(db: AsyncSession, ds_id: uuid.UUID, fields: Dict):
    ds = await _get_dataset(db, ds_id)
    for k, v in fields.items():
        setattr(ds, k, v)
    await db.commit()


async def _set_status(db, ds_id, status, error_message=None):
    ds = await _get_dataset(db, ds_id)
    ds.status = status
    if error_message:
        ds.error_message = error_message
    await db.commit()


async def _start_step(db: AsyncSession, ds_id: uuid.UUID, step: str):
    run = PipelineRun(dataset_id=ds_id, step=step, status="running")
    db.add(run)
    await db.commit()


async def _finish_step(
    db: AsyncSession, ds_id: uuid.UUID, step: str, detail: str = "", status: str = "done"
):
    result = await db.execute(
        select(PipelineRun).where(
            PipelineRun.dataset_id == ds_id,
            PipelineRun.step == step,
        ).order_by(PipelineRun.started_at.desc())
    )
    run = result.scalars().first()
    if run:
        run.status = status
        run.detail = detail
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
