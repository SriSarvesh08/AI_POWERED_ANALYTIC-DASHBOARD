"""
RAG Engine — Retrieval-Augmented Generation chatbot.
Every answer is grounded in real data retrieved from chunk embeddings.
"""

import json
import logging
import math
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dataset, Chart, ChunkEmbedding
from app.models.schemas import ChatResponse, ChartDataItem
from app.services.gemini_service import GeminiService
from app.services.superset_client import SupersetClient
from app.core.config import settings

logger = logging.getLogger(__name__)


class RAGEngine:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.gemini = GeminiService()
        self.superset = SupersetClient()

    def _is_column_numeric(self, dataset, col_name: str) -> bool:
        """Check if a column is numeric by inspecting the dataset's raw_schema."""
        s = dataset.raw_schema or {}
        cols = []
        for t in s.get("tables", []):
            cols.extend(t.get("columns", []))
        cols.extend(s.get("columns", []))
        for c in cols:
            nm = c.get("name", c.get("column_name", ""))
            if nm.upper() == col_name.upper():
                ct = str(c.get("type", c.get("data_type", c.get("dtype", "")))).upper()
                return any(k in ct for k in ["INT", "FLOAT", "DECIMAL", "NUMERIC", "DOUBLE", "REAL", "BIGINT", "NUMBER"])
        return False

    def _find_datetime_column(self, dataset) -> str | None:
        """
        Scan the dataset's raw_schema for the first DATE/TIMESTAMP/DATETIME column.
        Returns the column name if found, None otherwise.
        """
        s = dataset.raw_schema or {}
        cols = []
        for t in s.get("tables", []):
            cols.extend(t.get("columns", []))
        cols.extend(s.get("columns", []))
        for c in cols:
            ct = str(c.get("type", c.get("data_type", c.get("dtype", "")))).upper()
            if any(kw in ct for kw in ["DATETIME", "TIMESTAMP", "DATE", "TIME"]):
                return c.get("name", c.get("column_name"))
        return None

    # ── Main query handler ────────────────────────────────────────────────────

    async def query(
        self,
        dataset: Dataset,
        user_message: str,
        history: List[Dict],
        user_id: str,
    ) -> ChatResponse:
        """
        Full RAG pipeline:
        1. Embed the user query
        2. Retrieve top-K relevant chunks from the dataset's embeddings
        3. Detect intent (chart request vs data question vs chart modification)
        4. If chart: generate SQL → create chart
        5. If modification: return modification instructions
        6. Generate grounded answer with Gemini
        """
        # Step 1: embed query gracefully
        try:
            query_vector = await self.gemini.embed_query(user_message)
            # Step 2: retrieve relevant chunks
            chunks = await self._retrieve_chunks(dataset.id, query_vector, top_k=8)
        except Exception as e:
            logger.warning(f"Embedding query failed, likely quota hit. Skipping dense retrieval. Error: {e}")
            query_vector = []
            chunks = []
            
        context_texts = [c.chunk_text for c in chunks]

        # Get existing chart titles for modification detection
        charts_result = await self.db.execute(
            select(Chart).where(Chart.dataset_id == dataset.id)
        )
        existing_charts = charts_result.scalars().all()
        chart_titles = [c.title for c in existing_charts if c.title]

        # Step 3: detect intent
        intent = await self._detect_intent(user_message, dataset.ai_schema or {}, chart_titles)

        new_chart_id = None
        sql_generated = None
        chart_data = None
        chart_modification = None

        # Step 4a: handle chart TYPE CHANGE intent
        if intent.get("is_chart_modification"):
            target_type = intent.get("target_type", "bar")
            matched_title = intent.get("matched_title")
            
            target_chart = None
            if matched_title:
                for c in existing_charts:
                    if c.title == matched_title:
                        target_chart = c
                        break
            elif existing_charts:
                # Fallback to the most recent chart if title not matched reliably
                target_chart = existing_charts[-1]
                
            if target_chart and target_chart.superset_chart_id:
                try:
                    # Fetch current params and update viz_type
                    c_data = await self.superset.get_chart(target_chart.superset_chart_id)
                    params = json.loads(c_data.get("params", "{}"))
                    
                    # Extract the current x-axis from existing chart params
                    existing_x = None
                    if params.get("groupby") and len(params["groupby"]) > 0:
                        existing_x = params["groupby"][0]
                    elif params.get("x_axis"):
                        existing_x = params["x_axis"]
                    elif params.get("granularity_sqla"):
                        existing_x = params["granularity_sqla"]
                    
                    # With GENERIC_CHART_AXES enabled in Superset, line/area charts
                    # work with ANY x-axis type using the 'x_axis' parameter.
                    # No datetime column required!
                    if target_type in ("line", "area"):
                        new_viz = "echarts_timeseries_line" if target_type == "line" else "echarts_timeseries"
                        # Use x_axis (generic axes) instead of granularity_sqla
                        params["x_axis"] = existing_x
                        params.pop("granularity_sqla", None)
                        params.pop("time_grain_sqla", None)
                        params["groupby"] = []
                        params.pop("columns", None)
                    else:
                        viz_map = {
                            "bar": "dist_bar",
                            "pie": "pie",
                            "scatter": "echarts_scatter",
                            "table": "table",
                            "big_number": "big_number_total",
                        }
                        new_viz = viz_map.get(target_type, "dist_bar")
                        # For bar/pie, restore groupby
                        if new_viz in ("dist_bar", "pie") and existing_x:
                            params["groupby"] = [existing_x]
                            params.pop("x_axis", None)
                    
                    params["viz_type"] = new_viz
                    
                    # Sanitize existing metrics — prevent AVG/SUM on text columns
                    metrics = params.get("metrics", [])
                    for i, m in enumerate(metrics):
                        if isinstance(m, dict) and "sqlExpression" in m:
                            expr = m["sqlExpression"].upper()
                            if any(expr.startswith(agg) for agg in ["AVG(", "SUM(", "MIN(", "MAX("]):
                                col_match = expr.split('(', 1)[1].rstrip(')').strip().strip('"')
                                if not self._is_column_numeric(dataset, col_match):
                                    orig_col = m["sqlExpression"].split('(', 1)[1].rstrip(')')
                                    m["sqlExpression"] = f"COUNT({orig_col})"
                                    m["label"] = f"Count of {col_match}"
                                    logger.warning(f"Sanitized metric: changed aggregation to COUNT for non-numeric column '{col_match}'")
                    
                    # Also sanitize the 'metric' key (singular, used by pie/big_number)
                    single_metric = params.get("metric")
                    if isinstance(single_metric, dict) and "sqlExpression" in single_metric:
                        expr = single_metric["sqlExpression"].upper()
                        if any(expr.startswith(agg) for agg in ["AVG(", "SUM(", "MIN(", "MAX("]):
                            col_match = expr.split('(', 1)[1].rstrip(')').strip().strip('"')
                            if not self._is_column_numeric(dataset, col_match):
                                orig_col = single_metric["sqlExpression"].split('(', 1)[1].rstrip(')')
                                single_metric["sqlExpression"] = f"COUNT({orig_col})"
                                single_metric["label"] = f"Count of {col_match}"
                    
                    await self.superset.update_chart(
                        target_chart.superset_chart_id, 
                        {"viz_type": new_viz, "params": json.dumps(params)}
                    )
                    
                    target_chart.chart_type = target_type
                    await self.db.commit()
                    
                    new_chart_id = target_chart.superset_chart_id
                    
                    answer = f"Done! I've changed '{target_chart.title}' to a **{target_type} chart**. The visualization has been updated on your dashboard."
                    
                    chart_modification = {
                        "target_title": target_chart.title,
                        "new_type": target_type,
                    }
                    
                    return ChatResponse(
                        answer=answer,
                        chart_modification=chart_modification,
                        new_chart_id=new_chart_id,
                        sources=[],
                    )
                except Exception as e:
                    logger.warning(f"Error modifying chart in Superset: {e}")
                    pass
            
            return ChatResponse(
                answer="I understood you want to modify a chart, but I couldn't automatically configure that specific chart right now.",
                sources=[],
            )

        # Step 4b: handle chart creation intent
        wh_table = dataset.warehouse_table
        if intent.get("is_chart_request") and wh_table:
            try:
                sql_result = await self.gemini.generate_sql(
                    question=user_message,
                    schema=dataset.ai_schema or {},
                    table_name=wh_table,
                    history=history,
                )
                sql_generated = sql_result.get("sql")

                if sql_generated:
                    # Execute SQL and build native chart data
                    chart_data = await self._execute_chart_sql(
                        sql=sql_generated,
                        table_name=dataset.warehouse_table,
                        chart_type=sql_result.get("chart_type", "bar"),
                        title=sql_result.get("title", "AI Chart"),
                        x_col=sql_result.get("x_col"),
                        y_col=sql_result.get("y_col"),
                    )

                    # Also save chart record + Superset chart if available
                    if dataset.superset_dataset_id:
                        try:
                            raw_type = sql_result.get("chart_type", "table")
                            x_col_r = sql_result.get("x_col")
                            y_col_r = sql_result.get("y_col")
                            datetime_col = self._find_datetime_column(dataset)

                            # Smart viz type: use timeseries when datetime exists
                            if raw_type in ("line", "area") and datetime_col:
                                ss_viz = "echarts_timeseries_line" if raw_type == "line" else "echarts_timeseries"
                            elif raw_type in ("line", "area") and not datetime_col:
                                ss_viz = "dist_bar"  # fallback — no datetime available
                            else:
                                viz_type_map = {
                                    "bar": "dist_bar", "pie": "pie",
                                    "scatter": "echarts_scatter",
                                    "table": "table", "big_number": "big_number_total",
                                }
                                ss_viz = viz_type_map.get(raw_type, "dist_bar")

                            # Safe aggregation — prevent AVG/SUM on text columns
                            agg = "SUM" if self._is_column_numeric(dataset, y_col_r or "") else "COUNT"
                            quoted_y = f'"{y_col_r}"' if y_col_r else None
                            metric_obj = {
                                "expressionType": "SQL",
                                "sqlExpression": f"{agg}({quoted_y})" if quoted_y else "COUNT(*)",
                                "label": f"{agg} of {y_col_r}" if y_col_r else "Count",
                                "hasCustomLabel": True,
                                "optionName": f"metric_{uuid.uuid4().hex[:8]}",
                            }
                            if not x_col_r and ss_viz != "table":
                                ss_viz = "big_number_total"

                            chart_params = {"viz_type": ss_viz, "adhoc_filters": [], "row_limit": 1000, "color_scheme": "supersetColors"}

                            if ss_viz.startswith("echarts_timeseries"):
                                # Use x_axis (GENERIC_CHART_AXES) — works with any column type
                                chart_params["x_axis"] = x_col_r
                                chart_params["metrics"] = [metric_obj]
                                chart_params["groupby"] = []
                            elif ss_viz == "dist_bar":
                                chart_params["groupby"] = [x_col_r] if x_col_r else []
                                chart_params["metrics"] = [metric_obj]
                            elif ss_viz == "pie":
                                chart_params["groupby"] = [x_col_r] if x_col_r else []
                                chart_params["metric"] = metric_obj
                                chart_params["metrics"] = [metric_obj]
                            elif ss_viz == "big_number_total":
                                chart_params["metric"] = metric_obj
                            else:
                                chart_params["groupby"] = [x_col_r] if x_col_r else []
                                chart_params["metrics"] = [metric_obj]

                            ss_chart_id = await self.superset.create_chart(
                                datasource_id=dataset.superset_dataset_id,
                                title=sql_result.get("title", "AI Chart"),
                                chart_type=ss_viz,
                                params=chart_params,
                            )
                            new_chart_id = ss_chart_id

                            chart = Chart(
                                dataset_id=dataset.id,
                                user_id=user_id,
                                superset_chart_id=ss_chart_id,
                                title=sql_result.get("title", "AI Chart"),
                                chart_type=sql_result.get("chart_type", "table"),
                                sql_query=sql_generated,
                                ai_reasoning=f"Generated from user query: {user_message}",
                            )
                            self.db.add(chart)
                            await self.db.commit()

                            if dataset.superset_dashboard_id:
                                await self._add_chart_to_dashboard(
                                    dataset.superset_dashboard_id, ss_chart_id
                                )
                        except Exception as e:
                            logger.warning("Superset chart creation failed (non-blocking): %s", e)

            except Exception as e:
                logger.warning("Chart generation failed: %s", e)
                context_texts.append(f"Note: chart generation encountered an issue: {e}")

        # Step 5: grounded answer
        answer = await self.gemini.generate_rag_answer(
            question=user_message,
            context_chunks=context_texts,
            data_summary=dataset.ai_schema.get("data_summary", "") if dataset.ai_schema else "",
            history=history,
        )

        if chart_data:
            answer += f"\n\n📊 Chart generated and displayed below."

        return ChatResponse(
            answer=answer,
            sql_generated=sql_generated,
            new_chart_id=new_chart_id,
            sources=[c.chunk_text[:120] + "…" for c in chunks[:3]],
            chart_data=chart_data,
        )

    # ── Chart modification ────────────────────────────────────────────────────

    async def modify_chart(
        self,
        dataset: Dataset,
        chart_id: int,
        instruction: str,
        user_id: str,
    ) -> ChatResponse:
        """Modify an existing Superset chart based on natural language instruction."""
        current = await self.superset.get_chart(chart_id)
        plan = await self.gemini.plan_chart_modification(current, instruction)

        updates: Dict[str, Any] = {}
        if plan.get("viz_type"):
            updates["viz_type"] = plan["viz_type"]
        if plan.get("new_title"):
            updates["slice_name"] = plan["new_title"]
        if plan.get("params_override"):
            updates["params"] = str(plan["params_override"])

        if updates:
            await self.superset.update_chart(chart_id, updates)

        return ChatResponse(
            answer=plan.get("explanation", "Chart updated successfully."),
            modified_chart_id=chart_id,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _retrieve_chunks(
        self, dataset_id, query_vector: List[float], top_k: int = 8
    ) -> List[ChunkEmbedding]:
        """Cosine similarity retrieval over stored embeddings (pure Python fallback)."""
        result = await self.db.execute(
            select(ChunkEmbedding).where(ChunkEmbedding.dataset_id == dataset_id)
        )
        all_chunks = result.scalars().all()

        if not all_chunks:
            return []

        def cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x ** 2 for x in a))
            norm_b = math.sqrt(sum(x ** 2 for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

        scored = [
            (chunk, cosine(query_vector, chunk.embedding or []))
            for chunk in all_chunks
            if chunk.embedding
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in scored[:top_k]]

    async def _detect_intent(self, message: str, ai_schema: Dict, chart_titles: List[str] = None) -> Dict:
        """Quick intent detection — checks for chart modification, chart creation, or data questions."""
        lower = message.lower()

        # ── Chart type MODIFICATION detection ─────────────────────────────
        modify_keywords = [
            "change", "convert", "switch", "make it", "turn it", "turn into",
            "transform", "as a", "to a", "into a", "change it to", "make this",
            "change the", "convert the", "switch the",
        ]
        chart_type_map = {
            "bar": "bar", "bar chart": "bar", "bar graph": "bar",
            "line": "line", "line chart": "line", "line graph": "line",
            "pie": "pie", "pie chart": "pie", "donut": "doughnut", "doughnut": "doughnut",
            "scatter": "scatter", "scatter plot": "scatter", "scatter chart": "scatter",
            "area": "area", "area chart": "area",
            "table": "table",
        }

        is_modify = any(mk in lower for mk in modify_keywords)
        if is_modify:
            # Find target chart type
            target_type = None
            for type_phrase, mapped_type in sorted(chart_type_map.items(), key=lambda x: len(x[0]), reverse=True):
                if type_phrase in lower:
                    target_type = mapped_type
                    break

            if target_type:
                # Find the BEST matching chart title using word overlap scoring
                matched_title = None
                best_score = 0
                
                # Words to ignore when matching (too common in chart titles)
                stop_words = {
                    "chart", "graph", "the", "and", "for", "by", "of", "in",
                    "on", "at", "to", "a", "an", "vs", "versus", "it", "this",
                    "average", "total", "distribution", "comparison",
                }
                
                # Extract meaningful words from the user message (skip chart type words + stop words)
                chart_type_words = {"bar", "line", "pie", "scatter", "area", "table"}
                msg_words = set(
                    w.lower() for w in lower.split()
                    if len(w) > 3 and w.lower() not in stop_words and w.lower() not in chart_type_words
                )
                
                if chart_titles and msg_words:
                    for title in chart_titles:
                        title_words = set(w.lower() for w in title.split() if len(w) > 3 and w.lower() not in stop_words)
                        score = len(msg_words & title_words)
                        if score > best_score:
                            best_score = score
                            matched_title = title
                
                # Only accept the match if at least 2 meaningful words overlap
                if best_score < 2:
                    matched_title = None

                return {
                    "is_chart_modification": True,
                    "target_type": target_type,
                    "matched_title": matched_title,
                }

        # ── Chart CREATION detection ──────────────────────────────────────
        chart_keywords = [
            "chart", "graph", "plot", "visualize", "show", "create", "generate",
            "bar", "line", "pie", "scatter", "trend", "compare",
        ]
        if any(kw in lower for kw in chart_keywords):
            return {"is_chart_request": True}
        return {"is_chart_request": False}

    async def _add_chart_to_dashboard(self, dashboard_id: int, chart_id: int):
        """Append a new chart to an existing dashboard."""
        try:
            headers = await self.superset._headers()
            import httpx
            async with httpx.AsyncClient(base_url=settings.SUPERSET_BASE_URL, timeout=30) as client:
                resp = await client.get(f"/api/v1/dashboard/{dashboard_id}", headers=headers)
                if resp.status_code == 200:
                    existing = resp.json().get("result", {})
                    slices = existing.get("slices", [])
                    slice_ids = [s["slice_id"] for s in slices] + [chart_id]
                    await client.put(
                        f"/api/v1/dashboard/{dashboard_id}",
                        headers=headers,
                        json={"slices": slice_ids},
                    )
        except Exception as e:
            logger.warning("Could not add chart to dashboard: %s", e)

    async def _execute_chart_sql(
        self,
        sql: str,
        table_name: str,
        chart_type: str,
        title: str,
        x_col: str = None,
        y_col: str = None,
    ) -> ChartDataItem:
        """
        Execute SQL and build a ChartDataItem for native rendering.
        """
        import asyncio
        import sqlalchemy as sa

        COLORS = [
            "rgba(59, 130, 246, 0.85)", "rgba(16, 185, 129, 0.85)",
            "rgba(249, 115, 22, 0.85)", "rgba(139, 92, 246, 0.85)",
            "rgba(236, 72, 153, 0.85)", "rgba(14, 165, 233, 0.85)",
            "rgba(245, 158, 11, 0.85)", "rgba(20, 184, 166, 0.85)",
        ]
        BORDER_COLORS = [
            "rgba(59, 130, 246, 1)", "rgba(16, 185, 129, 1)",
            "rgba(249, 115, 22, 1)", "rgba(139, 92, 246, 1)",
            "rgba(236, 72, 153, 1)", "rgba(14, 165, 233, 1)",
            "rgba(245, 158, 11, 1)", "rgba(20, 184, 166, 1)",
        ]

        sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
        sync_engine = sa.create_engine(sync_url)

        def _run_query():
            with sync_engine.connect() as conn:
                result = conn.execute(sa.text(sql))
                columns = list(result.keys())
                rows = result.fetchall()
            return columns, rows

        columns, rows = await asyncio.to_thread(_run_query)
        sync_engine.dispose()

        if not rows or not columns:
            return ChartDataItem(
                chart_id="chat_chart",
                title=title,
                chart_type=chart_type,
                labels=[],
                datasets=[],
            )

        def _safe(v):
            if v is None: return 0
            if isinstance(v, (int, float)): return v
            try: return float(v)
            except: return str(v)

        if chart_type == "scatter" and len(columns) >= 2:
            data_points = [{"x": _safe(r[0]), "y": _safe(r[1])} for r in rows]
            return ChartDataItem(
                chart_id="chat_chart",
                title=title,
                chart_type="scatter",
                labels=[],
                datasets=[{
                    "label": f"{columns[0]} vs {columns[1]}",
                    "data": data_points,
                    "backgroundColor": COLORS[0],
                    "borderColor": BORDER_COLORS[0],
                    "borderWidth": 1,
                    "pointRadius": 5,
                }],
                x_label=columns[0],
                y_label=columns[1],
            )

        if chart_type == "pie":
            labels = [str(_safe(r[0])) for r in rows]
            values = [_safe(r[1]) if len(r) > 1 else _safe(r[0]) for r in rows]
            n = len(labels)
            return ChartDataItem(
                chart_id="chat_chart",
                title=title,
                chart_type="pie",
                labels=labels,
                datasets=[{
                    "label": columns[1] if len(columns) > 1 else columns[0],
                    "data": values,
                    "backgroundColor": [COLORS[i % len(COLORS)] for i in range(n)],
                    "borderColor": [BORDER_COLORS[i % len(BORDER_COLORS)] for i in range(n)],
                    "borderWidth": 2,
                }],
            )

        # bar / line / area
        labels = [str(_safe(r[0])) for r in rows]
        values = [_safe(r[1]) if len(r) > 1 else 1 for r in rows]
        mapped = "line" if chart_type == "area" else chart_type

        return ChartDataItem(
            chart_id="chat_chart",
            title=title,
            chart_type=mapped,
            labels=labels,
            datasets=[{
                "label": columns[1] if len(columns) > 1 else "Count",
                "data": values,
                "backgroundColor": COLORS[:len(values)],
                "borderColor": BORDER_COLORS[:len(values)],
                "borderWidth": 2,
                "borderRadius": 6,
            }],
            x_label=columns[0] if columns else None,
            y_label=columns[1] if len(columns) > 1 else None,
        )
