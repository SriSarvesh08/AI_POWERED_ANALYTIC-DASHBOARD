"""
Apache Superset REST API client.
Handles: login, dataset creation, chart creation, dashboard creation, guest tokens.
"""

import json
import logging
from typing import Any, Dict, List, Optional
from fastapi import HTTPException
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SUPERSET = settings.SUPERSET_BASE_URL


class SupersetClient:
    """Async Superset REST API wrapper."""

    def __init__(self):
        self._token: Optional[str] = None
        self._csrf: Optional[str] = None
        self._client = httpx.AsyncClient(
            base_url=SUPERSET, 
            timeout=60, 
            follow_redirects=True
        )

    async def _post(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.post(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def _put(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.put(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.get(path, **kwargs)
        resp.raise_for_status()
        return resp

    async def _login(self) -> str:
        resp = await self._post("/api/v1/security/login", json={
            "username": settings.SUPERSET_ADMIN_USER,
            "password": settings.SUPERSET_ADMIN_PASSWORD,
            "provider": "db",
            "refresh": True,
        })
        self._token = resp.json()["access_token"]

        # Fetch CSRF token
        csrf_resp = await self._get(
            "/api/v1/security/csrf_token/",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        self._csrf = csrf_resp.json()["result"]

        if not self._token:
            raise ValueError("Failed to retrieve access token from Superset")
        return self._token

    async def _headers(self) -> Dict[str, str]:
        if not self._token:
            await self._login()
        return {
            "Authorization": f"Bearer {self._token}",
            "X-CSRFToken": self._csrf or "",
            "Content-Type": "application/json",
            "Referer": SUPERSET,
        }

    # ── Database / Dataset ────────────────────────────────────────────────────

    async def get_or_create_database(self, db_uri: str, db_name: str = "DataPilot Warehouse") -> int:
        """Register our data warehouse PostgreSQL with Superset and return its ID."""
        headers = await self._headers()
        
        # Check if already registered using precise filter. Use RISON format explicitly
        # Superset 3.1.1 strictly parses RISON format efficiently for these endpoints without 422 schema errors.
        q_rison = f"(filters:!((col:database_name,opr:eq,value:'{db_name}')))"
        
        resp = await self._client.get("/api/v1/database/", params={"q": q_rison}, headers=headers)
        resp.raise_for_status()
        
        results = resp.json().get("result", [])
        if results:
            return results[0]["id"]

        try:
            # Create new database connection
            create_resp = await self._post("/api/v1/database/", headers=headers, json={
                "database_name": db_name,
                "sqlalchemy_uri": db_uri,
                "expose_in_sqllab": True,
            })
            return create_resp.json()["id"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                # Check again directly just in case of parallel creation
                resp = await self._client.get("/api/v1/database/", params={"q": q_rison}, headers=headers)
                resp.raise_for_status()
                results = resp.json().get("result", [])
                if results:
                    return results[0]["id"]
                # If still not found, parse message to raise explicit error
                err_text = getattr(e.response, "text", str(e))
                raise RuntimeError(f"Failed to create Superset DB connection. 422: {err_text}")
            raise

    async def create_dataset(
        self, database_id: int, table_name: str, schema: str = "public"
    ) -> int:
        """Create a Superset virtual dataset pointing to our warehouse table. Idempotent."""
        headers = await self._headers()
        
        # Check if already exists using RISON filter (Superset 3.x requires RISON, not JSON)
        # We uniquely identify the dataset by its table_name (which is a UUID in our system).
        # We omit 'schema' from the filter to prevent 422 UNPROCESSABLE ENTITY errors from FAB.
        q_rison = f"(filters:!((col:table_name,opr:eq,value:'{table_name}')))"
        
        try:
            resp = await self._client.get("/api/v1/dataset/", params={"q": q_rison}, headers=headers)
            resp.raise_for_status()
            results = resp.json().get("result", [])
            if results:
                return results[0]["id"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                logger.warning(f"Dataset GET filter threw 422. Proceeding to create. Details: {e.response.text}")
            else:
                raise

        try:
            resp = await self._post("/api/v1/dataset/", headers=headers, json={
                "database": database_id,
                "schema": schema,
                "table_name": table_name,
                "always_filter_main_dttm": False,
            })
            dataset_id = resp.json()["id"]
            
            # --- Auto-configure Time Column ---
            try:
                # Fetch dataset details to see columns
                get_resp = await self._get(f"/api/v1/dataset/{dataset_id}", headers=headers)
                ds_data = get_resp.json().get("result", {})
                cols = ds_data.get("columns", [])
                
                # Look for a likely time column if none is set
                if not ds_data.get("main_dttm_col") and cols:
                    time_cols = [c["column_name"] for c in cols if any(x in c["column_name"].lower() for x in ["date", "time", "timestamp", "created", "updated"])]
                    if time_cols:
                        best_time_col = time_cols[0]
                        await self._client.put(f"/api/v1/dataset/{dataset_id}", headers=headers, json={
                            "main_dttm_col": best_time_col
                        })
                        logger.info(f"Auto-set main_dttm_col for dataset {dataset_id} to {best_time_col}")
            except Exception as e:
                logger.warning(f"Failed to auto-configure time column: {e}")
                
            return dataset_id
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                # Double check again if it was created in a race condition
                resp = await self._get("/api/v1/dataset/", headers=headers)
                for d in resp.json().get("result", []):
                    if d.get("table_name") == table_name:
                        return d["id"]
                logger.error(f"Superset 422 error details: {e.response.text}")
                raise ValueError(f"Superset 422 Error: {e.response.text}") from e
            raise

    # ── Charts ────────────────────────────────────────────────────────────────

    async def create_chart(
        self,
        datasource_id: int,
        title: str,
        chart_type: str,
        params: Dict[str, Any],
    ) -> int:
        """Create a Superset chart and return its ID."""
        # Trust the modern ECharts type passed from the orchestrator
        viz_type = chart_type
        
        # Override params viz_type for consistency
        params["viz_type"] = viz_type

        headers = await self._headers()
        try:
            resp = await self._post("/api/v1/chart/", headers=headers, json={
                "slice_name": title,
                "viz_type": viz_type,
                "datasource_id": datasource_id,
                "datasource_type": "table",
                "params": json.dumps(params),
            })
            return resp.json()["id"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                logger.error(f"Superset Chart 422 Error details: {e.response.text}")
                raise ValueError(f"Superset Chart 422 Error: {e.response.text}") from e
            raise

    async def update_chart(self, chart_id: int, updates: Dict[str, Any]) -> bool:
        """Update an existing chart."""
        headers = await self._headers()
        await self._put(f"/api/v1/chart/{chart_id}", headers=headers, json=updates)
        return True

    async def get_chart(self, chart_id: int) -> Dict[str, Any]:
        headers = await self._headers()
        resp = await self._get(f"/api/v1/chart/{chart_id}", headers=headers)
        return resp.json()["result"]

    # ── Dashboard ─────────────────────────────────────────────────────────────

    async def create_dashboard(
        self, title: str, chart_ids: List[int]
    ) -> int:
        """Create a dashboard with the given charts and return its ID."""
        headers = await self._headers()

        # Step 1: Create minimal dashboard
        resp = await self._post("/api/v1/dashboard/", headers=headers, json={
            "dashboard_title": title,
            "published": True,
        })
        resp.raise_for_status()
        dashboard_id = resp.json()["id"]

        # Step 2: Update with layout
        try:
            position_data = self._build_layout(chart_ids)
            await self._put(f"/api/v1/dashboard/{dashboard_id}", headers=headers, json={
                "position_json": json.dumps(position_data),
            })
        except httpx.HTTPStatusError as e:
            logger.warning(f"Dashboard layout update returned {e.response.status_code}: {e.response.text}")
            # Dashboard was already created — continue even if layout fails

        # Step 3: Link charts to dashboard
        for chart_id in chart_ids:
            try:
                chart_data = await self.get_chart(chart_id)
                current_dashboards = [d["id"] for d in chart_data.get("dashboards", [])]
                if dashboard_id not in current_dashboards:
                    current_dashboards.append(dashboard_id)
                    await self.update_chart(chart_id, {"dashboards": current_dashboards})
            except Exception as e:
                logger.warning(f"Failed to link chart {chart_id} to dashboard {dashboard_id}: {e}")

        return dashboard_id

    def _build_layout(self, chart_ids: List[int]) -> Dict[str, Any]:
        """Build a basic Superset grid layout for a list of chart IDs."""
        cols_per_row = 1
        layout: Dict[str, Any] = {
            "DASHBOARD_VERSION_KEY": "v2", 
            "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
            "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": []}
        }

        for i, chart_id in enumerate(chart_ids):
            row_id = f"ROW_{i // cols_per_row}"
            col_id = f"COLUMN_{i}"
            chart_key = f"CHART_{chart_id}"

            if col_id not in layout:
                layout[col_id] = {
                    "type": "COLUMN",
                    "id": col_id,
                    "children": [chart_key],
                    "meta": {
                        "background": "BACKGROUND_TRANSPARENT",
                        "width": 12,
                    },
                }
            layout[chart_key] = {
                "type": "CHART",
                "id": chart_key,
                "meta": {
                    "chartId": chart_id,
                    "width": 12,
                    "height": 80,
                },
            }
            if row_id not in layout:
                layout[row_id] = {
                    "type": "ROW",
                    "id": row_id,
                    "children": [],
                    "meta": {"background": "BACKGROUND_TRANSPARENT"}
                }
            if col_id not in layout[row_id]["children"]:
                layout[row_id]["children"].append(col_id)
            if row_id not in layout["GRID_ID"]["children"]:
                layout["GRID_ID"]["children"].append(row_id)

        return layout

    # ── Guest Token ───────────────────────────────────────────────────────────

    async def get_guest_token(self, dashboard_id: int, user_id: str) -> str:
        """Generate a short-lived Superset guest token for embedding."""
        headers = await self._headers()

        # Step 1: Get dashboard UUID (required for guest token)
        dashboard_uuid = None

        # Strategy A: Individual detail endpoint
        try:
            r = await self._get(f"/api/v1/dashboard/{dashboard_id}", headers=headers)
            result = r.json().get("result", {})
            # UUID can be at top level or nested
            dashboard_uuid = result.get("uuid")
            if not dashboard_uuid:
                # Some Superset versions return it differently
                dashboard_uuid = result.get("id_or_slug")
                if dashboard_uuid and len(str(dashboard_uuid)) < 30:
                    dashboard_uuid = None  # That was the numeric ID, not UUID
            if dashboard_uuid:
                logger.info(f"Found UUID for dashboard {dashboard_id} via detail: {dashboard_uuid}")
        except Exception as e:
            logger.warning(f"Failed to get dashboard {dashboard_id} detail: {e}")

        # Strategy B: List endpoint (fetch all dashboards, find ours)
        if not dashboard_uuid:
            try:
                r_list = await self._get("/api/v1/dashboard/", headers=headers)
                dashboards = r_list.json().get("result", [])
                for d in dashboards:
                    if d.get("id") == dashboard_id:
                        dashboard_uuid = d.get("uuid")
                        if dashboard_uuid:
                            logger.info(f"Found UUID for dashboard {dashboard_id} via list: {dashboard_uuid}")
                        break
            except Exception as e:
                logger.warning(f"Failed to find dashboard UUID in list: {e}")

        # Strategy C: Query Superset's metastore directly
        if not dashboard_uuid:
            try:
                import sqlalchemy as sa
                superset_db_uri = "postgresql+psycopg2://datapilot:datapilot@postgres:5432/superset"
                eng = sa.create_engine(superset_db_uri)
                with eng.connect() as conn:
                    row = conn.execute(
                        sa.text("SELECT uuid FROM dashboards WHERE id = :did"),
                        {"did": dashboard_id},
                    ).fetchone()
                    if row:
                        dashboard_uuid = str(row[0])
                        logger.info(f"Found UUID for dashboard {dashboard_id} via direct DB: {dashboard_uuid}")
                eng.dispose()
            except Exception as e:
                logger.warning(f"Direct DB lookup for dashboard UUID failed: {e}")

        if not dashboard_uuid:
            raise HTTPException(
                status_code=500,
                detail=f"Superset dashboard {dashboard_id} exists but its UUID could not be retrieved. Embedding failed.",
            )

        # Step 2: Request guest token
        resp = await self._post(
            "/api/v1/security/guest_token/",
            headers=headers,
            json={
                "user": {"username": f"user_{user_id}", "first_name": "Guest", "last_name": "User"},
                "resources": [{"type": "dashboard", "id": dashboard_uuid}],
                "rls": [],
            },
        )
        return resp.json()["token"]

