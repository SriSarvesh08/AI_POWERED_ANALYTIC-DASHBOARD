# DataPilot AI — AI-Powered Analytics Dashboard Platform

A fully automated, plug-and-play analytics platform powered by **Gemini AI**, **Apache Superset**, and a **RAG chatbot**. Upload any dataset or connect your database and get an AI-generated dashboard in minutes.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (React + Vite)                                        │
│  Login/Register · Upload/Connect · Pipeline · Dashboard · Chat  │
└────────────────────────┬────────────────────────────────────────┘
                         │ REST API
┌────────────────────────▼────────────────────────────────────────┐
│  Backend (FastAPI + Python)                                     │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Auth API   │  │  Ingest API  │  │   Connect DB API       │ │
│  │  JWT + bcrypt│  │  CSV/Excel/  │  │  PG/MySQL/Snowflake/  │ │
│  │             │  │  SQL Dump    │  │  Firebase              │ │
│  └─────────────┘  └──────┬───────┘  └──────────┬─────────────┘ │
│                          │                      │               │
│                   ┌──────▼──────────────────────▼─────────┐    │
│                   │   AI Pipeline Orchestrator             │    │
│                   │                                        │    │
│                   │  1. Ingest → PostgreSQL warehouse      │    │
│                   │  2. Extract full schema                │    │
│                   │  3. Gemini AI — analyze schema         │    │
│                   │  4. AI selects key columns/rows        │    │
│                   │  5. Push to Apache Superset            │    │
│                   │  6. Build RAG embeddings               │    │
│                   └──────┬──────────────────────┬──────────┘    │
│                          │                      │               │
│                   ┌──────▼────────┐   ┌─────────▼────────────┐  │
│                   │ Gemini Service│   │  Superset Client     │  │
│                   │ Schema analysis│  │  Dataset, Charts,    │  │
│                   │ SQL generation│  │  Dashboard, Tokens   │  │
│                   │ RAG answers   │  └──────────────────────┘  │
│                   │ Embeddings    │                             │
│                   └───────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
                         │                    │
            ┌────────────▼──────┐   ┌─────────▼────────┐
            │  PostgreSQL       │   │  Apache Superset  │
            │  · Auth DB        │   │  · Charts         │
            │  · Data Warehouse │   │  · Dashboards     │
            │  · Pipeline State │   │  · Embed tokens   │
            │  · RAG Embeddings │   └──────────────────┘
            └───────────────────┘
```

---

## Mandatory Data Pipeline

**No charts are ever generated outside this pipeline.**

```
Uploaded File / Connected Database
         ↓
  Gemini AI — Schema + Data Analysis
  (identifies metrics, dimensions, excludes irrelevant fields)
         ↓
  AI selects top rows and columns
  (invalid/empty/insufficient data → pipeline stops, no charts)
         ↓
  Selected data sent to Apache Superset
         ↓
  Superset generates charts (AI-specified types only)
         ↓
  RAG embeddings built for chatbot
         ↓
  Dashboard available to user
```

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A **Gemini API key** from [Google AI Studio](https://aistudio.google.com/app/apikey)

### 1. Clone and configure

```bash
git clone https://github.com/SriSarvesh08/AI_POWERED_ANALYTIC-DASHBOARD.git
cd AI_POWERED_ANALYTIC-DASHBOARD

# Create env file
cp .env.example .env

# Edit .env and set your Gemini API key
nano .env
```

### 2. Start everything

```bash
docker-compose up -d
```

This starts:
| Service | URL |
|---------|-----|
| Frontend | http://localhost:80 |
| Backend API | http://localhost:8000 |
| API Docs | http://localhost:8000/api/docs |
| Apache Superset | http://localhost:8088 |
| PostgreSQL | localhost:5432 |

### 3. Register and start

1. Open http://localhost:80
2. Click **Create account**
3. Upload a CSV or connect a database
4. Watch the AI pipeline run
5. View your auto-generated dashboard
6. Chat with your data using the RAG chatbot

---

## Supported Data Sources

### Upload Mode

| Format | Description |
|--------|-------------|
| `.csv` | Comma-separated values |
| `.xlsx` / `.xls` | Excel spreadsheets |
| `.sql` | MySQL/PostgreSQL dump files |

### Connect Mode

| Database | Authentication |
|----------|----------------|
| PostgreSQL | Host, port, database, username, password |
| MySQL | Host, port, database, username, password |
| Snowflake | Account, warehouse, database, schema, username, password |
| Firebase | Service account JSON + project ID |

---

## Local Development (without Docker)

### Backend

```bash
cd backend

# Install Python deps
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your database URLs and Gemini API key

# Start PostgreSQL (or use a cloud instance)
# Run migrations (tables auto-created on startup)

# Start the API server
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install

# Set backend URL
echo "VITE_API_BASE_URL=http://localhost:8000" > .env.local
echo "VITE_SUPERSET_URL=http://localhost:8088" >> .env.local

npm run dev
# → http://localhost:5173
```

### Apache Superset (local)

```bash
pip install apache-superset
superset db upgrade
superset fab create-admin --username admin --password admin --firstname Admin --lastname User --email admin@example.com
superset init
superset run -p 8088 --with-threads --reload --debugger
```

---

## Project Structure

```
datapilot/
├── docker-compose.yml          # Full stack orchestration
├── .env.example                # Root environment template
├── infra/
│   ├── init_db.sql             # Creates auth + superset databases
│   └── superset_config.py      # Superset configuration (embedding enabled)
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env.example
│   └── app/
│       ├── main.py             # FastAPI app + CORS + routers
│       ├── core/
│       │   ├── config.py       # Pydantic settings (loads from .env)
│       │   └── security.py     # JWT creation/verification, bcrypt
│       ├── db/
│       │   └── session.py      # Async SQLAlchemy engines + Base models
│       ├── models/
│       │   ├── __init__.py     # ORM: User, Dataset, Chart, ChunkEmbedding, PipelineRun
│       │   └── schemas.py      # Pydantic request/response schemas
│       ├── api/
│       │   ├── auth.py         # POST /register, POST /login, GET /me
│       │   ├── ingest.py       # POST /upload, GET /datasets
│       │   ├── connect.py      # POST /test, POST /connect
│       │   ├── pipeline.py     # GET /{dataset_id} — pipeline status
│       │   ├── dashboard.py    # GET /, GET /{id}/embed-token
│       │   └── chat.py         # POST /query, POST /modify-chart
│       └── services/
│           ├── ingestion.py            # CSV/Excel/SQL → PostgreSQL warehouse
│           ├── db_connector.py         # External DB schema extraction
│           ├── gemini_service.py       # All Gemini API calls
│           ├── superset_client.py      # Superset REST API wrapper
│           ├── pipeline_orchestrator.py # 6-step background pipeline
│           └── rag_engine.py           # RAG retrieval + answer generation
│
└── frontend/
    ├── Dockerfile
    ├── nginx.conf
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── main.jsx            # React entry point
        ├── App.jsx             # Router with auth guards
        ├── contexts/
        │   └── AuthContext.jsx # Global auth state (JWT stored in localStorage)
        ├── services/
        │   └── api.js          # Axios instance with error interceptor
        ├── styles/
        │   └── global.css      # Design system (CSS variables, reset, animations)
        ├── components/
        │   └── AppShell.jsx    # Sidebar + topbar layout
        └── pages/
            ├── LoginPage.jsx       # JWT login
            ├── RegisterPage.jsx    # Account creation
            ├── OverviewPage.jsx    # Stats + dataset list
            ├── DataSourcePage.jsx  # Upload (drag & drop) or Connect DB
            ├── PipelinePage.jsx    # Live AI pipeline status with polling
            ├── DashboardPage.jsx   # Embedded Superset dashboard
            └── ChatPage.jsx        # RAG chatbot interface
```

---

## API Reference

All endpoints require `Authorization: Bearer <token>` except `/auth/*`.

### Authentication
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Get JWT token |
| GET  | `/api/auth/me` | Get current user |

### Data Ingestion (Upload Mode)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/ingest/upload` | Upload CSV/Excel/SQL (multipart) |
| GET  | `/api/ingest/datasets` | List user's datasets |
| GET  | `/api/ingest/datasets/{id}` | Get single dataset |

### Database Connection (Connect Mode)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/connect/test` | Test DB connectivity |
| POST | `/api/connect/connect` | Connect DB + start pipeline |

### Pipeline
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pipeline/{dataset_id}` | Get pipeline status + step details |

### Dashboards
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/dashboard/` | List all ready dashboards |
| GET | `/api/dashboard/{dataset_id}/embed-token` | Get Superset guest token |

### RAG Chatbot
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat/query` | Ask a question about your data |
| POST | `/api/chat/modify-chart` | Modify chart via natural language |

Interactive docs: http://localhost:8000/api/docs

---

## Key Design Decisions

### No Default Charts
Charts are **only** generated after Gemini AI analysis. The pipeline checks:
- Dataset has ≥ 1 row and ≥ 1 meaningful column
- AI returns `is_valid_for_analytics: true`
- AI selects ≥ 1 metric and ≥ 1 chart suggestion

If any check fails, the pipeline halts and no visualization is created.

### Two Exclusive Modes
The system enforces mode exclusivity at the UI level — the mode toggle switches between Upload and Connect, and the backend routes handle each independently.

### 100% RAG Chatbot
The chatbot never uses pre-programmed answers. Every response:
1. Embeds the user query via Gemini Embedding API
2. Performs cosine similarity search over stored data chunks
3. Retrieves top-K relevant chunks (schema info + sample rows)
4. Passes chunks as context to Gemini for grounded answer generation

### Per-User Isolation
Each user's Superset dashboards are accessed via short-lived guest tokens, ensuring data isolation without requiring separate Superset accounts.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | ✅ | Google AI Studio API key |
| `DATABASE_URL` | ✅ | PostgreSQL async URL (asyncpg) |
| `AUTH_DATABASE_URL` | ✅ | Auth DB URL (can be same DB) |
| `SECRET_KEY` | ✅ | JWT signing secret (32+ chars) |
| `SUPERSET_BASE_URL` | ✅ | Superset base URL |
| `SUPERSET_ADMIN_USER` | ✅ | Superset admin username |
| `SUPERSET_ADMIN_PASSWORD` | ✅ | Superset admin password |
| `SUPERSET_SECRET_KEY` | ✅ | Superset secret key |
| `GEMINI_MODEL` | optional | Default: `gemini-1.5-pro` |
| `MAX_UPLOAD_SIZE_MB` | optional | Default: `100` |

---

## Production Checklist

- [ ] Set strong random `SECRET_KEY` and `SUPERSET_SECRET_KEY`
- [ ] Use a managed PostgreSQL (RDS, Cloud SQL, Supabase)
- [ ] Enable HTTPS — update `SESSION_COOKIE_SECURE=True` in Superset config
- [ ] Restrict `CORS_ORIGINS` to your actual domain
- [ ] Install `pgvector` extension for native vector similarity search
- [ ] Set `GUEST_TOKEN_JWT_EXP_SECONDS` to appropriate expiry
- [ ] Add rate limiting to chat endpoints
- [ ] Store uploaded files in S3 or GCS instead of local disk
- [ ] Encrypt database connection credentials at rest

---

## License

MIT
