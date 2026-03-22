"""
Ingestion service — parses uploaded files and loads rows into the data warehouse.
Supports: CSV, Excel (.xlsx/.xls), SQL dump.
"""

import re
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Tuple, Dict, Any

import pandas as pd
import sqlalchemy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings

logger = logging.getLogger(__name__)


class IngestionService:
    """
    Reads a file, infers schema, and bulk-inserts into a per-dataset
    table in the data warehouse PostgreSQL database.
    """

    def __init__(self):
        self.engine = create_async_engine(settings.DATABASE_URL, echo=False)

    async def ingest_file(
        self, file_path: str, source_type: str, dataset_id: str
    ) -> Tuple[str, int, int, Dict[str, Any]]:
        """
        Ingest a local file into the warehouse.
        Returns: (warehouse_table_name, row_count, col_count, raw_schema)
        """
        df = await asyncio.get_event_loop().run_in_executor(
            None, self._load_file, file_path, source_type
        )

        if df is None or df.empty:
            raise ValueError("File is empty or could not be parsed")

        table_name = self._make_table_name(dataset_id)
        schema = self._extract_schema(df)

        await self._create_table_and_insert(df, table_name)

        return table_name, len(df), len(df.columns), schema

    def _load_file(self, file_path: str, source_type: str) -> pd.DataFrame:
        path = Path(file_path)
        if source_type == "csv":
            df = pd.read_csv(path, low_memory=False)
        elif source_type == "excel":
            df = pd.read_excel(path)
        elif source_type == "sql_dump":
            df = self._parse_sql_dump(path)
        else:
            raise ValueError(f"Unknown source_type: {source_type}")
            
        # Permanent Fix: Normalise column names to lowercase to avoid Postgres case sensitivity issues
        df.columns = [str(c).lower().replace(" ", "_").replace(".", "_") for c in df.columns]
        return df

    def _parse_sql_dump(self, path: Path) -> pd.DataFrame:
        """
        Parse a SQL dump file — extract INSERT statements and build DataFrame.
        Handles basic mysqldump / pg_dump INSERT INTO formats.
        """
        sql = path.read_text(errors="replace")
        # Find all INSERT INTO <table> VALUES (...) blocks
        pattern = re.compile(
            r"INSERT INTO\s+[`\"']?(\w+)[`\"']?\s+(?:\([^)]+\)\s+)?VALUES\s*(.+?)(?:;|\Z)",
            re.IGNORECASE | re.DOTALL,
        )
        matches = pattern.findall(sql)
        if not matches:
            raise ValueError("No INSERT statements found in SQL dump")

        rows = []
        for _, values_block in matches:
            row_pattern = re.compile(r"\(([^)]+)\)")
            for row_match in row_pattern.finditer(values_block):
                vals = [v.strip().strip("'\"") for v in row_match.group(1).split(",")]
                rows.append(vals)

        if not rows:
            raise ValueError("Could not extract rows from SQL dump")

        df = pd.DataFrame(rows)
        df.columns = [f"col_{i}" for i in range(len(df.columns))]
        return df

    def _make_table_name(self, dataset_id: Any) -> str:
        safe_id = str(dataset_id).replace("-", "_")
        return f"ds_{safe_id}"

    def _extract_schema(self, df: pd.DataFrame) -> Dict[str, Any]:
        columns = []
        for col in df.columns:
            dtype = str(df[col].dtype)
            sample = df[col].dropna().head(3).tolist()
            null_pct = round(df[col].isna().mean() * 100, 1)
            unique_count = int(df[col].nunique())
            
            # Map pandas dtype to SQL-like type for downstream detection
            sql_type = "TEXT"
            dtype_lower = dtype.lower()
            if "int" in dtype_lower:
                sql_type = "INTEGER"
            elif "float" in dtype_lower or "double" in dtype_lower:
                sql_type = "FLOAT"
            elif "datetime" in dtype_lower:
                sql_type = "TIMESTAMP"
            elif "date" in dtype_lower:
                sql_type = "DATE"
            elif "bool" in dtype_lower:
                sql_type = "BOOLEAN"
            
            columns.append({
                "name": col,
                "type": sql_type,
                "dtype": dtype,
                "sample_values": [str(s) for s in sample],
                "null_pct": null_pct,
                "unique_count": unique_count,
                "cardinality": "high" if unique_count > 50 else "low",
            })
        return {
            "tables": [{"name": "uploaded_file", "columns": columns}],
            "columns": columns,
            "row_count": len(df),
            "col_count": len(df.columns),
        }

    async def _create_table_and_insert(self, df: pd.DataFrame, table_name: str):
        """
        Create a warehouse table and bulk-insert the dataframe.
        Uses synchronous SQLAlchemy for pandas to_sql compatibility.
        """
        import sqlalchemy as sa

        sync_url = settings.DATABASE_URL.replace("+asyncpg", "")
        sync_engine = sa.create_engine(sync_url, echo=False)

        def _do_insert():
            df.to_sql(
                table_name,
                sync_engine,
                if_exists="replace",
                index=False,
                chunksize=1000,
            )
            sync_engine.dispose()

        await asyncio.get_event_loop().run_in_executor(None, _do_insert)
        logger.info("Inserted %d rows into warehouse table %s", len(df), table_name)
