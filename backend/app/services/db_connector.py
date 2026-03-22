"""
Database connector service — connects to external databases, extracts schema.
Supports: PostgreSQL, MySQL, Snowflake, Firebase Firestore.
"""

import asyncio
import logging
from typing import Tuple, Dict, Any, List

from app.models.schemas import DBConnectRequest

logger = logging.getLogger(__name__)


async def test_connection(config: DBConnectRequest) -> Tuple[bool, str]:
    """Quick connectivity test — returns (ok, message)."""
    try:
        if config.db_type == "postgresql":
            return await _test_postgres(config)
        elif config.db_type == "mysql":
            return await _test_mysql(config)
        elif config.db_type == "snowflake":
            return await _test_snowflake(config)
        elif config.db_type == "firebase":
            return await _test_firebase(config)
        else:
            return False, f"Unsupported db_type: {config.db_type}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


async def _test_postgres(cfg: DBConnectRequest) -> Tuple[bool, str]:
    import asyncpg
    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port or 5432,
        database=cfg.database, user=cfg.username, password=cfg.password,
        timeout=10,
    )
    await conn.close()
    return True, "PostgreSQL connection successful"


async def _test_mysql(cfg: DBConnectRequest) -> Tuple[bool, str]:
    import aiomysql
    conn = await aiomysql.connect(
        host=cfg.host, port=cfg.port or 3306,
        db=cfg.database, user=cfg.username, password=cfg.password,
        connect_timeout=10,
    )
    conn.close()
    return True, "MySQL connection successful"


async def _test_snowflake(cfg: DBConnectRequest) -> Tuple[bool, str]:
    # Snowflake connector is synchronous — run in executor
    def _connect():
        import snowflake.connector
        conn = snowflake.connector.connect(
            account=cfg.account,
            user=cfg.username,
            password=cfg.password,
            warehouse=cfg.warehouse,
            database=cfg.database,
            schema=cfg.schema or "PUBLIC",
        )
        conn.close()
    await asyncio.get_event_loop().run_in_executor(None, _connect)
    return True, "Snowflake connection successful"


async def _test_firebase(cfg: DBConnectRequest) -> Tuple[bool, str]:
    import json
    import firebase_admin
    from firebase_admin import credentials, firestore

    def _do_test():
        sa_info = json.loads(cfg.service_account_json)
        cred = credentials.Certificate(sa_info)
        app_name = f"datapilot_{cfg.project_id}"
        try:
            app = firebase_admin.get_app(app_name)
        except ValueError:
            app = firebase_admin.initialize_app(cred, name=app_name)
        db = firestore.client(app)
        # Just list collections as a connectivity check
        list(db.collections())

    await asyncio.get_event_loop().run_in_executor(None, _do_test)
    return True, "Firebase Firestore connection successful"


class ExternalDBConnector:
    """Extracts full schema from an external database."""

    def __init__(self, config: DBConnectRequest):
        self.config = config

    async def extract_schema(self) -> Dict[str, Any]:
        if self.config.db_type == "postgresql":
            return await self._schema_postgres()
        elif self.config.db_type == "mysql":
            return await self._schema_mysql()
        elif self.config.db_type == "snowflake":
            return await self._schema_snowflake()
        elif self.config.db_type == "firebase":
            return await self._schema_firebase()
        raise ValueError(f"Unsupported db_type: {self.config.db_type}")

    async def _schema_postgres(self) -> Dict[str, Any]:
        import asyncpg
        conn = await asyncpg.connect(
            host=self.config.host, port=self.config.port or 5432,
            database=self.config.database,
            user=self.config.username, password=self.config.password,
        )
        try:
            rows = await conn.fetch("""
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.is_nullable,
                    (SELECT COUNT(*) FROM information_schema.key_column_usage k
                     WHERE k.table_name=c.table_name AND k.column_name=c.column_name) AS is_key
                FROM information_schema.columns c
                WHERE c.table_schema = 'public'
                ORDER BY c.table_name, c.ordinal_position
            """)
            return self._rows_to_schema(rows, "table_name", "column_name", "data_type")
        finally:
            await conn.close()

    async def _schema_mysql(self) -> Dict[str, Any]:
        import aiomysql
        conn = await aiomysql.connect(
            host=self.config.host, port=self.config.port or 3306,
            db=self.config.database, user=self.config.username, password=self.config.password,
        )
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""
                    SELECT TABLE_NAME as table_name, COLUMN_NAME as column_name,
                           DATA_TYPE as data_type
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                """, (self.config.database,))
                rows = await cur.fetchall()
            return self._rows_to_schema(rows, "table_name", "column_name", "data_type")
        finally:
            conn.close()

    async def _schema_snowflake(self) -> Dict[str, Any]:
        def _extract():
            import snowflake.connector
            conn = snowflake.connector.connect(
                account=self.config.account, user=self.config.username,
                password=self.config.password, warehouse=self.config.warehouse,
                database=self.config.database, schema=self.config.schema or "PUBLIC",
            )
            cursor = conn.cursor()
            cursor.execute("""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = CURRENT_SCHEMA()
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """)
            rows = [{"table_name": r[0], "column_name": r[1], "data_type": r[2]}
                    for r in cursor.fetchall()]
            conn.close()
            return rows

        rows = await asyncio.get_event_loop().run_in_executor(None, _extract)
        return self._rows_to_schema(rows, "table_name", "column_name", "data_type")

    async def _schema_firebase(self) -> Dict[str, Any]:
        """
        Firebase is schema-less — sample documents to infer field types.
        Maps Python type names to SQL types so the orchestrator can correctly
        identify numeric vs text columns for aggregation safety.
        """
        import json
        import firebase_admin
        from firebase_admin import credentials, firestore

        # Python type name -> SQL type for the orchestrator's column type checker
        PYTHON_TO_SQL = {
            "int": "INTEGER",
            "float": "FLOAT",
            "bool": "BOOLEAN",
            "str": "TEXT",
            "datetime": "TIMESTAMP",
            "Timestamp": "TIMESTAMP",
            "NoneType": "TEXT",
        }

        config = self.config  # capture for use inside thread

        def _sample():
            sa_info = json.loads(config.service_account_json)
            cred = credentials.Certificate(sa_info)
            app_name = f"datapilot_{config.project_id}_schema"
            try:
                app = firebase_admin.get_app(app_name)
            except ValueError:
                app = firebase_admin.initialize_app(cred, name=app_name)

            db = firestore.client(app)
            tables = {}
            for col in db.collections():
                docs = list(col.limit(50).stream())
                if not docs:
                    continue
                fields = {}
                for doc in docs:
                    for k, v in doc.to_dict().items():
                        python_type = type(v).__name__
                        # Translate to SQL type so the orchestrator understands it
                        fields[k] = PYTHON_TO_SQL.get(python_type, "TEXT")
                tables[col.id] = [
                    {"column_name": k, "data_type": v} for k, v in fields.items()
                ]
            return tables

        tables = await asyncio.get_event_loop().run_in_executor(None, _sample)
        return {
            "tables": [
                {"name": t, "columns": cols} for t, cols in tables.items()
            ],
            "db_type": "firebase",
        }

    @staticmethod
    def _rows_to_schema(
        rows, table_key: str, col_key: str, type_key: str
    ) -> Dict[str, Any]:
        tables: Dict[str, List] = {}
        for r in rows:
            t = r[table_key]
            if t not in tables:
                tables[t] = []
            tables[t].append({"column_name": r[col_key], "data_type": r[type_key]})
        return {
            "tables": [{"name": t, "columns": cols} for t, cols in tables.items()]
        }
