# 🛡️ CodeNexus AI — Multi-Agent Code Review System

> **A multi-agent system for automated code analysis, bug detection, refactoring, and documentation.**
>
> Powered by **FastAPI**, **LangGraph**, **Python AST / Scikit-Learn**, **Google Gemini (primary)** and **Groq (fallback)**, with a vanilla HTML/CSS/JS frontend.

---

## 📌 Project Overview

CodeNexus AI lets developers upload source code and receive structured, AI-assisted feedback from a team of specialized agents working through an MCP-style analysis pipeline. Instead of a single chatbot response, the system applies **static analysis** (AST metrics + ML risk scoring) **plus** **LLM-enhanced review** to produce:

* 🔍 **Architecture & performance review**
* 🐞 **Bug & vulnerability inspection**
* ⚡ **Production refactored code**
* 📄 **Documented implementation** (docstrings & comments)

The code is analyzed by two layers:

1. **MCP Core Engine** (`mcp_server.py`, port 8000) — Python AST feature extraction, an experimental RandomForest risk classifier, and deterministic health scoring.
2. **Web Gateway** (`web_server.py`, port 9000) — FastAPI HTTP frontend, API gateway, MCP router, input validation, and Markdown export.

---

## 🏗️ Architecture & Four-Agent Workflow

### Components

| Component | Technology | Local Port | Docker Port | Role |
|---|---|---|---|---|
| **MCP Core Engine** | FastAPI + Scikit-Learn + AST | 8000 | 8000 | AST metrics, ML risk classification, static bug finding, refactoring, documentation |
| **Web Gateway** | FastAPI + Jinja2 + httpx | 9000 | 9000 | HTTP frontend, API gateway, MCP router, Markdown export |
| **Agent Graph** | LangGraph + Gemini/Groq | N/A | N/A | Four-agent review → bug → refactor → docs pipeline |
| **Redis** (optional) | Redis | 6379 | optional | LLM response caching (crash-safe) |
| **Frontend** | HTML5 + CSS3 + Vanilla JS | Served by Web Gateway | Served by Web Gateway | Dark-themed code review UI |

### Four-Agent Workflow (from source)

The LangGraph graph defines four agent nodes (`reviewer`, `bug_hunter`, `refactor`, `documentation`) with sequential edges, but `run_pipeline()` in `agents.py` invokes all four nodes **concurrently** via `asyncio.gather` so no single slow or failed agent blocks the others.

Each agent calls the shared LLM router (`call_llm`), which routes to **Gemini (primary)** first and falls back to **Groq** on failure, with Redis caching and a Gemini circuit breaker.

```
User submits code (POST /analyze)
        │
        ▼
┌─────────────────────────────┐
│  MCP Core Engine (/code/*)  │  AST features + health_score + risk_level
│  (static analysis, ML risk) │
└─────────────────────────────┘
        │
        ▼
┌─────────────────────────────┐
│  run_pipeline() (LangGraph) │  Four agents run concurrently:
│                             │   • Reviewer        (review)
│                             │   • Bug Hunter      (bugs)
│                             │   • Refactor Agent  (refactored)
│                             │   • Doc Agent       (docs)
└─────────────────────────────┘
        │
        ▼
Web Gateway merges static metrics + agent output → returns JSON
        │
        ▼
Frontend renders review, bugs, refactored code, docs, health score, risk level
```

> **Not "official MCP":** CodeNexus AI uses an **MCP-style** architecture — a custom FastAPI service (`mcp_server.py`) that exposes analysis tools over HTTP. It does **not** implement the official Model Context Protocol specification.

---

## ⚙️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+, FastAPI (async) |
| Agent orchestration | LangGraph |
| Static analysis | Python `ast`, Scikit-Learn `RandomForestClassifier` |
| LLM providers | Google Gemini (`gemini-3.6-flash`, primary), Groq (`openai/gpt-oss-120b`, fallback) |
| HTTP client | `httpx` |
| Frontend | HTML5, CSS3 (dark mode), Vanilla JS |
| Templates | Jinja2 |
| Caching (optional) | Redis |
| Config | `python-dotenv` (`.env`, gitignored — never committed) |

## 🔑 Model Configuration (verified from source)

Defaults are read from environment variables (see `.env.example`; never `.env`):

| Setting | Default | Source |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.6-flash` | `agents.py` line 28 / `.env.example` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | `agents.py` line 29 / `.env.example` |
| `PRIMARY provider` | **Gemini** | `call_llm()` tries Gemini first |
| `FALLBACK provider` | **Groq** | `call_llm()` falls back to Groq on Gemini failure |
| `LLM_ENABLED` | `1` | `1` = live LLM, `0` = skip all LLM network calls |
| `LLM_STUB` | `0` | `1` = deterministic stub text, no network |
| `MCP_BASE` | `http://127.0.0.1:8000` | Web Gateway → MCP Core Engine URL |

### Routing behavior (from `agents.py` `call_llm()`)

1. If `LLM_STUB=1` → returns deterministic stub text (no network).
2. If `LLM_ENABLED=0` → returns "LLM analysis disabled" result (no network).
3. Otherwise → checks Redis cache, then tries **Gemini primary**; on failure → falls back to **Groq**.
4. If all providers fail → returns a clearly marked "unavailable" result; static-analysis metrics are still returned.

**Important:** Observed live behavior — Gemini experienced timeouts under load and the **Groq fallback was observed**; Gemini was **not** consistently successful. Do not rely on Gemini alone for production latency.

---

## 🚀 Installation & Local Execution

### Prerequisites
* Python 3.10 or higher (3.12+ recommended)
* A Google Gemini API key (free tier) — **only** needed for live LLM analysis
* Optional: Groq API key for the fallback provider

### 1. Clone & create virtual environment
```bash
git clone https://github.com/shahbaazcodes/-CodeNexus-AI.git
cd -CodeNexus-AI
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure (`.env` — gitignored, never committed)
Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
# edit .env with your GEMINI_API_KEY / GROQ_API_KEY
```

Never paste real API keys into this README, issues, or commits. `.env` is excluded from version control via `.gitignore`.

### 4. Start the two services (separate terminals)

Terminal 1 — MCP Core Engine (port 8000):
```bash
uvicorn mcp_server:app --reload --port 8000
```

Terminal 2 — Web Gateway (port 9000):
```bash
python3 web_server.py
```

### 5. Usage
Open http://127.0.0.1:9000 in your browser and submit code for analysis.

### Stub / offline mode (no API key required)
Set `LLM_ENABLED=0` and `LLM_STUB=1` in `.env` (or pass at runtime). The pipeline returns deterministic stub LLM text while still running real MCP static-analysis metrics. Ideal for offline demos and CI.

---

## 🐳 How to Run with Docker Compose

CodeNexus AI can be run entirely with Docker Compose without installing Python locally.

### Start both services
```bash
docker compose up -d --build
```
This creates a bridge network (`major_project_mcp_ai_default`) with two services:
* **mcp-server** — MCP Core Engine on port **8000** (host `8000:8000`)
* **web-server** — Web Gateway on port **9000** (host `9000:9000`)

The web service reaches MCP through Compose service-name DNS (`http://mcp-server:8000`), not `127.0.0.1`.

### Stub mode (default, no API keys)
By default Compose runs with `LLM_STUB=1` and `LLM_ENABLED=0`, so no live LLM calls are made and no API keys are required. The `.env` file is never copied into images; configuration is supplied at runtime via `docker-compose.yml` environment variables.

### Live LLM mode
Provide API keys at runtime without editing the image:
```bash
docker compose up -d \
  -e GEMINI_API_KEY="your_key" \
  -e GROQ_API_KEY="your_key" \
  -e LLM_ENABLED=1 \
  -e LLM_STUB=0 \
  web-server
```

### Health checks
Both services define `HEALTHCHECK` instructions and Compose `healthcheck` entries. `web-server` waits for `mcp-server` to be healthy (`condition: service_healthy`) before starting.

### Logs
```bash
# All service logs
docker compose logs

# Follow a single service
docker compose logs -f web-server
docker compose logs -f mcp-server
```

### Shutdown
```bash
# Stop and remove containers (keeps images)
docker compose down

# Stop and remove containers plus images
docker compose down --rmi all
```

### Troubleshooting
| Symptom | Likely cause / fix |
|---------|--------------------|
| `web-server` unhealthy / won't start | `mcp-server` not healthy yet; check `docker compose logs mcp-server`. Wait for MCP health then retry. |
| `/analyze` returns static metrics but stub text | You are in stub mode (`LLM_STUB=1`, `LLM_ENABLED=0`) — expected. |
| Port already in use (8000/9000) | Another process is using the host port; stop it or change the host mapping in `docker-compose.yml`. |
| `.env` not picked up | `.env` is gitignored and never copied into images. Set variables in `docker-compose.yml` or pass them on the `docker compose up` command line. |

---

## 📡 API Endpoints

### Web Gateway (`web_server.py`, port 9000)

| Endpoint | Method | Purpose |
|---|---|---|
| `GET /` | GET | Serve the web UI (index.html) |
| `GET /health` | GET | Application health status |
| `GET /health/providers` | GET | Provider availability (mcp, gemini, groq, redis) |
| `POST /analyze` | POST | Full code analysis (metrics + 4-agent pipeline) |
| `POST /export/markdown` | POST | Render an analysis result as Markdown report |

#### `POST /analyze` example
```bash
curl -X POST http://127.0.0.1:9000/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "main.py",
    "language": "python",
    "content": "def foo():\n    try:\n        x = 1/0\n    except:\n        pass\n  }'
```

Response includes `review`, `bugs`, `refactored`, `docs`, `health_score`, `risk_level`, and `metrics`.

### MCP Core Engine (`mcp_server.py`, port 8000)

| Endpoint | Method | Purpose |
|---|---|---|
| `GET /health` | GET | Engine health |
| `POST /code/metrics` | POST | AST features + health score + ML risk level |
| `POST /code/analyze` | POST | Static issues from metrics |
| `POST /code/find_bugs` | POST | Language-specific bug detection |
| `POST /code/document` | POST | Docstring header generation |
| `POST /code/refactor` | POST | Rule-based code refactoring |

#### `POST /code/metrics` example
```bash
curl -X POST http://127.0.0.1:8000/code/metrics \
  -H "Content-Type: application/json" \
  -d '{"filename":"main.py","language":"python","content":"x = 1"}'
```

Response includes `cyclomatic_complexity`, `line_count`, `bare_excepts_or_unfreed`, `todo_count`, `nesting_depth`, `health_score` (0–100), `risk_level` (Low/Medium/High/Critical), `parse_error`, `none_eq_count`, and `ml_classifier: "experimental"`.

---

## 🧪 Testing

All tests are **offline by default** — no live Gemini/Groq calls are made, so no provider quotas are consumed.

### Run the full suite
```bash
source venv/bin/activate
python -m pytest tests/ -q
```

### Verified result
```
83 passed in <1s
```

Breakdown:
* `tests/test_web_gateway.py` — 17 tests (valid input, invalid input, oversized requests, unavailable services, malformed responses, disabled LLM, Markdown export, health endpoints)
* `tests/test_eval_suite.py` — 20 tests (evaluator dataset schema, metric calculations, report generation)
* `tests/test_llm_reliability.py` — 46 tests (LLM retry/failover/quota/circuit-breaker reliability)

All 83 tests pass. Tests mock all HTTP/MCP calls; no real LLM provider is invoked during routine runs.

> The test count above was verified by running the suite at implementation time. If you modify tests or dependencies, rerun `python -m pytest tests/ -q` to confirm the current result.

---

## ⚠️ Limitations & Known Issues

* **ML risk classifier is experimental.** The `RandomForestClassifier` in `mcp_server.py` is trained on **six synthetic rows** with manually assigned labels. It has not been evaluated on independently labelled real-world code. Treat `risk_level` as a rough heuristic, not a verified prediction. The deterministic `health_score` formula is the primary trusted signal.
* **Health score is deterministic, not ML-derived.** It is computed from AST metrics (complexity, bare excepts, nesting depth, TODOs) via a fixed deduction formula — not a trained model.
* **Gemini is not consistently successful.** In live validation, Gemini experienced timeouts and the **Groq fallback was observed**. Do not claim Gemini was consistently successful; always design for fallback.
* **Not official MCP.** CodeNexus AI implements an **MCP-style** custom FastAPI service, not the official Model Context Protocol specification.
* **No production deployment, benchmarks, or performance claims** are made in this project. Do not cite users, throughput, or uptime figures that are not measured.
* **Small evaluation dataset.** The eval suite contains 20 labelled cases; results are not sufficient to establish generalization across languages or real-world security scenarios.
* **Concurrent agent execution.** `run_pipeline()` runs all four agents concurrently via `asyncio.gather`; the LangGraph edges themselves are sequential in definition but the pipeline invokes nodes directly and concurrently.

---

## 🖼️ Portfolio Demo & Screenshots

This section uses **existing repository assets only**. No new images were created or requested.

The project includes a dark-themed frontend (`templates/index.html`, `static/script.js`, `static/style.css`) served by the Web Gateway at http://127.0.0.1:9000. To capture a portfolio demo:

1. Start the system locally (see "Installation & Local Execution") — stub mode works with no API keys.
2. Open the Web Gateway in your browser.
3. Submit a Python sample (e.g., a function with a bare `except` and `== None`) and observe the review, bugs, refactored code, docs, health score, and risk level.
4. Screenshot the results page for your portfolio.

No additional screenshot/image assets exist in the repository beyond the frontend source. If you want custom screenshots or diagrams added, request approval before creating them.

---

## 🔒 Security

* API keys live in `.env` (gitignored) and are **never** copied into Docker images or committed.
* The Web Gateway validates all incoming code requests with Pydantic v2 models (filename traversal, unsupported language, oversized content rejected).
* Markdown export makes no LLM or MCP calls — it renders the supplied payload directly.
* Quarantine store records only minimal metadata (no source code, no secrets, no stack traces).

---

## 🔮 Future Improvements (not planned for Phase 8)

* Add first-class support for additional languages beyond Python/JavaScript/C++/Java.
* Integrate with GitHub webhooks for automatic PR reviews.
* Add "Download Report" PDF export.
* Replace the experimental 6-row ML classifier with a labelled real-world dataset.

---

Built by **[Shahbaaz Khan]** — Class of 2022–2026