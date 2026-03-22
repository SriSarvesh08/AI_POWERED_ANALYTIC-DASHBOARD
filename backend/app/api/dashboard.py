"""
Dashboard endpoints — list, embed token, chart list
"""

import uuid
import logging
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.session import get_db
from app.models import Dataset, Chart
from app.models.schemas import DashboardResponse, ChartDataResponse, ChartDataItem
from app.services.superset_client import SupersetClient
from app.core.config import settings

logger = logging.getLogger(__name__)

# Module-level singleton — one connection pool shared across all requests
_sync_engine = None


def _get_sync_engine():
    """Return the shared sync SQLAlchemy engine, creating it once."""
    global _sync_engine
    if _sync_engine is None:
        sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
        _sync_engine = sa.create_engine(sync_url, pool_size=5, max_overflow=10)
    return _sync_engine

router = APIRouter()


@router.get("/", response_model=list[DashboardResponse])
async def list_dashboards(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Dataset).where(
            Dataset.user_id == current_user["user_id"],
            Dataset.status == "superset_ready",
        ).order_by(Dataset.created_at.desc())
    )
    datasets = result.scalars().all()

    dashboards = []
    for ds in datasets:
        charts_result = await db.execute(
            select(Chart).where(Chart.dataset_id == ds.id)
        )
        charts = charts_result.scalars().all()
        dashboards.append(
            DashboardResponse(
                dataset_id=ds.id,
                dataset_name=ds.name,
                superset_dashboard_id=ds.superset_dashboard_id,
                superset_url=f"{settings.SUPERSET_PUBLIC_URL}/superset/dashboard/{ds.superset_dashboard_id}/"
                             if ds.superset_dashboard_id else None,
                embed_token=None,  # generated on demand via /embed-token
                charts=[
                    {
                        "id": str(c.id),
                        "superset_chart_id": c.superset_chart_id,
                        "title": c.title,
                        "chart_type": c.chart_type,
                    }
                    for c in charts
                ],
            )
        )
    return dashboards


@router.get("/{dataset_id}/embed-token")
async def get_embed_token(
    dataset_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a short-lived Superset guest token for embedding dashboards."""
    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.user_id == current_user["user_id"],
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset or not dataset.superset_dashboard_id:
        raise HTTPException(status_code=404, detail="Dashboard not found or not ready")

    client = SupersetClient()
    token = await client.get_guest_token(
        dashboard_id=dataset.superset_dashboard_id,
        user_id=str(current_user["user_id"]),
    )
    return {"embed_token": token, "superset_base_url": settings.SUPERSET_PUBLIC_URL}


@router.get("/{dataset_id}/superset-view")
async def superset_view(
    dataset_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns an HTML page that auto-logs into Superset and redirects
    to the dashboard — no manual login required.
    """
    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.user_id == current_user["user_id"],
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset or not dataset.superset_dashboard_id:
        raise HTTPException(status_code=404, detail="Dashboard not found")

    dashboard_id = dataset.superset_dashboard_id
    ss_user = settings.SUPERSET_ADMIN_USER
    ss_pass = settings.SUPERSET_ADMIN_PASSWORD

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Opening Superset Dashboard...</title>
<style>
  body {{
    margin: 0; display: flex; align-items: center; justify-content: center;
    height: 100vh; font-family: 'DM Sans', system-ui, sans-serif;
    background: linear-gradient(135deg, #0B1829 0%, #1A2B4A 100%);
    color: #B8D8F8;
  }}
  .loader {{
    text-align: center; animation: fadeIn 0.3s ease;
  }}
  .loader h2 {{ font-size: 18px; font-weight: 500; margin-bottom: 8px; color: #E3F0FD; }}
  .loader p {{ font-size: 13px; opacity: 0.7; }}
  .spinner {{
    width: 36px; height: 36px; border: 3px solid rgba(126, 184, 247, 0.2);
    border-top-color: #7EB8F7; border-radius: 50%;
    animation: spin 0.8s linear infinite; margin: 0 auto 16px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  .error {{ color: #F87171; margin-top: 12px; font-size: 13px; }}
</style>
</head>
<body>
<div class="loader">
  <div class="spinner"></div>
  <h2>Opening Superset Dashboard</h2>
  <p>Authenticating and loading your charts...</p>
  <p id="status"></p>
</div>
<form id="loginForm" method="POST" style="display:none;">
  <input name="username" value="{ss_user}">
  <input name="password" value="{ss_pass}">
  <input name="csrf_token" id="csrfInput" value="">
</form>
<script>
const DASHBOARD_URL = '/superset/dashboard/{dashboard_id}/?standalone=2';
const statusEl = document.getElementById('status');

async function autoLogin() {{
  try {{
    statusEl.textContent = 'Fetching authentication token...';

    // Step 1: Fetch the Superset login page to get the CSRF token
    const loginPage = await fetch('/login/', {{ credentials: 'include' }});
    const html = await loginPage.text();

    // Extract CSRF token from the login form
    const csrfMatch = html.match(/name="csrf_token"[^>]*value="([^"]+)"/);
    if (!csrfMatch) {{
      // Try WTF_CSRF pattern
      const altMatch = html.match(/csrf_token.*?value="([^"]+)"/);
      if (altMatch) {{
        document.getElementById('csrfInput').value = altMatch[1];
      }} else {{
        // No CSRF found — try direct redirect (maybe already logged in)
        statusEl.textContent = 'Redirecting...';
        window.location.href = DASHBOARD_URL;
        return;
      }}
    }} else {{
      document.getElementById('csrfInput').value = csrfMatch[1];
    }}

    statusEl.textContent = 'Logging in...';

    // Step 2: Submit login form via POST (sets session cookie)
    const form = document.getElementById('loginForm');
    form.action = '/login/?next=' + encodeURIComponent(DASHBOARD_URL);
    form.submit();

  }} catch (err) {{
    statusEl.innerHTML = '<span class="error">Auto-login failed: ' + err.message + '</span>';
    // Fallback: try direct redirect
    setTimeout(() => {{ window.location.href = DASHBOARD_URL; }}, 2000);
  }}
}}

autoLogin();
</script>
</body>
</html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ── Color palettes for native charts ─────────────────────────────────────────

CHART_COLORS = [
    "rgba(59, 130, 246, 0.85)",    # blue
    "rgba(16, 185, 129, 0.85)",    # emerald
    "rgba(249, 115, 22, 0.85)",    # orange
    "rgba(139, 92, 246, 0.85)",    # violet
    "rgba(236, 72, 153, 0.85)",    # pink
    "rgba(14, 165, 233, 0.85)",    # sky
    "rgba(245, 158, 11, 0.85)",    # amber
    "rgba(20, 184, 166, 0.85)",    # teal
    "rgba(244, 63, 94, 0.85)",     # rose
    "rgba(99, 102, 241, 0.85)",    # indigo
    "rgba(34, 197, 94, 0.85)",     # green
    "rgba(168, 85, 247, 0.85)",    # purple
]

CHART_BORDER_COLORS = [
    "rgba(59, 130, 246, 1)",
    "rgba(16, 185, 129, 1)",
    "rgba(249, 115, 22, 1)",
    "rgba(139, 92, 246, 1)",
    "rgba(236, 72, 153, 1)",
    "rgba(14, 165, 233, 1)",
    "rgba(245, 158, 11, 1)",
    "rgba(20, 184, 166, 1)",
    "rgba(244, 63, 94, 1)",
    "rgba(99, 102, 241, 1)",
    "rgba(34, 197, 94, 1)",
    "rgba(168, 85, 247, 1)",
]


@router.get("/{dataset_id}/chart-data", response_model=ChartDataResponse)
async def get_chart_data(
    dataset_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Execute each chart's SQL and return data formatted for native Chart.js rendering.
    No Superset iframe needed.
    """
    result = await db.execute(
        select(Dataset).where(
            Dataset.id == dataset_id,
            Dataset.user_id == current_user["user_id"],
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not dataset.warehouse_table:
        raise HTTPException(status_code=400, detail="Dataset has no warehouse table")

    # Get charts for this dataset
    charts_result = await db.execute(
        select(Chart).where(Chart.dataset_id == dataset_id)
    )
    charts = charts_result.scalars().all()

    if not charts:
        raise HTTPException(status_code=404, detail="No charts found for this dataset")

    # Execute SQL and build chart data
    engine = _get_sync_engine()

    chart_items = []
    for chart in charts:
        try:
            chart_item = _build_chart_data(
                engine, chart, dataset.warehouse_table
            )
            chart_items.append(chart_item)
        except Exception as e:
            logger.warning(f"Failed to build chart data for {chart.title}: {e}")
            # Add a fallback empty chart
            chart_items.append(ChartDataItem(
                chart_id=str(chart.id),
                title=chart.title or "Chart",
                chart_type=chart.chart_type or "bar",
                labels=[],
                datasets=[],
                ai_reasoning=chart.ai_reasoning,
            ))

    # No dispose — the singleton engine manages its own pool

    return ChartDataResponse(
        dataset_id=dataset.id,
        dataset_name=dataset.name,
        total_charts=len(chart_items),
        charts=chart_items,
    )


def _build_chart_data(engine, chart: Chart, table_name: str) -> ChartDataItem:
    """
    Execute a chart's SQL query and convert results to Chart.js-compatible format.
    """
    import sqlalchemy as sa
    import re

    sql = chart.sql_query or ""
    # Replace table placeholder
    quoted_table = f'"{table_name}"'
    sql = sql.replace("{{table}}", quoted_table).replace("{table}", quoted_table)

    # If SQL is empty, build a basic query from chart metadata
    if not sql.strip():
        sql = f'SELECT * FROM "{table_name}" LIMIT 100'

    # Fix PostgreSQL case-sensitivity: quote all column names that exist in the table
    with engine.connect() as conn:
        # Get actual column names from the table
        col_result = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :tbl"
        ), {"tbl": table_name})
        actual_columns = [row[0] for row in col_result.fetchall()]

    # Sort by length descending to replace longer names first (e.g. App_Usage_Time_min before App)
    for col in sorted(actual_columns, key=len, reverse=True):
        # Only quote if not already quoted and the column has mixed case or underscores
        # Use word boundary matching to avoid partial replacements
        pattern = r'(?<!")(?<!\w)' + re.escape(col) + r'(?!\w)(?!")'
        sql = re.sub(pattern, f'"{col}"', sql)

    with engine.connect() as conn:
        result = conn.execute(sa.text(sql))
        columns = list(result.keys())
        rows = result.fetchall()

    if not rows or not columns:
        return ChartDataItem(
            chart_id=str(chart.id),
            title=chart.title or "Chart",
            chart_type=chart.chart_type or "bar",
            labels=[],
            datasets=[],
            ai_reasoning=chart.ai_reasoning,
        )

    chart_type = chart.chart_type or "bar"

    # Build labels and data based on chart type
    if chart_type == "table":
        raw_rows = [dict(zip(columns, [_safe_val(v) for v in row])) for row in rows]
        return ChartDataItem(
            chart_id=str(chart.id),
            title=chart.title or "Table",
            chart_type="table",
            labels=columns,
            datasets=[],
            raw_rows=raw_rows,
            ai_reasoning=chart.ai_reasoning,
        )

    if chart_type == "big_number":
        value = _safe_val(rows[0][0]) if rows else 0
        return ChartDataItem(
            chart_id=str(chart.id),
            title=chart.title or "Metric",
            chart_type="big_number",
            labels=[columns[0] if columns else "Value"],
            datasets=[{"label": columns[0] if columns else "Value", "data": [value]}],
            ai_reasoning=chart.ai_reasoning,
        )

    if chart_type == "scatter":
        # Scatter: first col = x, second col = y
        if len(columns) >= 2:
            data_points = [
                {"x": _safe_val(row[0]), "y": _safe_val(row[1])}
                for row in rows
            ]
            return ChartDataItem(
                chart_id=str(chart.id),
                title=chart.title or "Scatter",
                chart_type="scatter",
                labels=[],
                datasets=[{
                    "label": f"{columns[0]} vs {columns[1]}",
                    "data": data_points,
                    "backgroundColor": CHART_COLORS[0],
                    "borderColor": CHART_BORDER_COLORS[0],
                    "borderWidth": 1,
                    "pointRadius": 5,
                }],
                x_label=columns[0],
                y_label=columns[1],
                ai_reasoning=chart.ai_reasoning,
            )

    if chart_type == "pie":
        labels = [str(_safe_val(row[0])) for row in rows]
        values = [_safe_val(row[1]) if len(row) > 1 else _safe_val(row[0]) for row in rows]
        n = len(labels)
        return ChartDataItem(
            chart_id=str(chart.id),
            title=chart.title or "Pie",
            chart_type="pie",
            labels=labels,
            datasets=[{
                "label": columns[1] if len(columns) > 1 else columns[0],
                "data": values,
                "backgroundColor": [CHART_COLORS[i % len(CHART_COLORS)] for i in range(n)],
                "borderColor": [CHART_BORDER_COLORS[i % len(CHART_BORDER_COLORS)] for i in range(n)],
                "borderWidth": 2,
            }],
            ai_reasoning=chart.ai_reasoning,
        )

    # Default: bar / line / area
    labels = [str(_safe_val(row[0])) for row in rows]
    # If multiple value columns, create multiple datasets
    if len(columns) > 2:
        datasets = []
        for col_idx in range(1, len(columns)):
            ci = (col_idx - 1) % len(CHART_COLORS)
            datasets.append({
                "label": columns[col_idx],
                "data": [_safe_val(row[col_idx]) for row in rows],
                "backgroundColor": CHART_COLORS[ci],
                "borderColor": CHART_BORDER_COLORS[ci],
                "borderWidth": 2,
                "borderRadius": 6,
            })
    else:
        values = [_safe_val(row[1]) if len(row) > 1 else 1 for row in rows]
        datasets = [{
            "label": columns[1] if len(columns) > 1 else "Count",
            "data": values,
            "backgroundColor": CHART_COLORS[:len(values)],
            "borderColor": CHART_BORDER_COLORS[:len(values)],
            "borderWidth": 2,
            "borderRadius": 6,
        }]

    mapped_type = chart_type
    if chart_type == "area":
        mapped_type = "line"  # Chart.js uses line with fill for area

    return ChartDataItem(
        chart_id=str(chart.id),
        title=chart.title or "Chart",
        chart_type=mapped_type,
        labels=labels,
        datasets=datasets,
        x_label=columns[0] if columns else None,
        y_label=columns[1] if len(columns) > 1 else None,
        ai_reasoning=chart.ai_reasoning,
    )


def _safe_val(v):
    """Convert DB values to JSON-safe types. Preserves None for non-numeric columns."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (ValueError, TypeError):
        return str(v)
