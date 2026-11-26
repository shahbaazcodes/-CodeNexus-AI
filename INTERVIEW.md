# CodeNexus AI — Interview Talking Points

> Technical talking points and honest project explanations for recruiters and interviewers. All details are drawn from the actual source code (`agents.py`, `web_server.py`, `mcp_server.py`, `.env.example`), not from marketing claims.

---

## 👋 Elevator Pitch (30 seconds)

**CodeNexus AI** is a multi-agent code review system. You upload source code, and a team of four AI agents — Reviewer, Bug Hunter, Refactor, and Documentation — analyze it through an MCP-style pipeline that combines **static analysis** (Python AST metrics + experimental ML risk scoring) with **LLM-enhanced review** (Google Gemini primary, Groq fallback). It returns a health score, risk level, refactored code, and docstrings — all through a FastAPI web gateway with a dark-themed frontend.

---

## 🏗️ Architecture (from source)

### Two service layers

| Service | File | Port | Responsibility |
|---|---|---|---|
| MCP Core Engine | `mcp_server.py` | 8000 | AST feature extraction, RandomForest risk classifier, deterministic health score, static bug/refactor/doc tools |
| Web Gateway | `web_server.py` | 9000 | FastAPI HTTP frontend, API gateway, MCP router, Pydantic input validation, Markdown export |

They communicate over HTTP (`MCP_BASE`). In Docker Compose the web container reaches MCP via service-name DNS (`http://mcp-server:8000`), not `127.0.0.1`.

### Important: Not "official MCP"

CodeNexus AI uses an **MCP-style** architecture — a custom FastAPI service exposing analysis tools over HTTP. It does **not** implement the official Model Context Protocol specification. This is an intentional, honest distinction.

---

## 🤖 Four-Agent Workflow (accurate description)

### What the source actually shows

The LangGraph graph (`agents.py`) defines four nodes — `reviewer`, `bug_hunter`, `refactor`, `documentation` — connected by **sequential edges**:

```
reviewer → bug_hunter → refactor → documentation → END
```

However, `run_pipeline()` (the function actually called by the Web Gateway) does **not** run the graph sequentially. It invokes all four agent nodes **concurrently** via `asyncio.gather`, so one slow or failed agent does not block the others. Each agent independently calls the shared LLM router (`call_llm`).

### What each agent does

* **Reviewer** — calls MCP `/code/analyze` for static issues, then asks the LLM for an architectural/performance review.
* **Bug Hunter** — calls MCP `/code/find_bugs` for static bug hints, then asks the LLM for a detailed bug inspection with fixes.
* **Refactor** — calls MCP `/code/refactor` for rule-based refactoring, then asks the LLM to productionize the code.
* **Documentation** — calls MCP `/code/document` for a docstring header, then asks the LLM for docstrings and comments.

Outputs are merged and returned with `health_score` and `risk_level` from the MCP metrics call.

---

## 🔑 Model Routing & Fallback (verified from `agents.py`)

### Configured defaults

| Setting | Default | Source |
|---|---|---|
| Primary model | `gemini-3.6-flash` | `.env.example`, `agents.py` line 28 |
| Fallback model | `openai/gpt-oss-120b` | `.env.example`, `agents.py` line 29 |
| Primary provider | **Gemini** | `call_llm()` tries Gemini first |
| Fallback provider | **Groq** | `call_llm()` falls back to Groq on Gemini failure |

### `call_llm()` behavior

1. `LLM_STUB=1` → deterministic stub text, no network.
2. `LLM_ENABLED=0` → "LLM analysis disabled", no network.
3. Otherwise → Redis cache check → **Gemini primary** → on failure → **Groq fallback**.
4. If all providers fail → clearly marked "unavailable" result; static metrics still returned.

### Honest limitation (important for interviews)

**Gemini is not consistently successful.** In live Docker validation, Gemini experienced timeouts and the **Groq fallback was observed**. Do not claim Gemini was consistently successful. Always design for fallback. There is also a Gemini circuit breaker that, when open, skips Gemini and goes straight to Groq.

Stub mode (`LLM_STUB=1`, `LLM_ENABLED=0`) lets you demo offline with no API keys — ideal for portfolio demos and CI.

---

## 🧪 ML Components — Be Transparent

### Experimental RandomForest risk classifier

* Trained on **six synthetic rows** with manually assigned labels (complexity, line_count, bare_excepts, todo_count, nesting_depth → Low/Medium/High/Critical).
* **Not** evaluated on independently labelled real-world code.
* `risk_level` should be treated as a rough heuristic, not a verified prediction.

### Health score is deterministic

* Computed from AST metrics via a fixed deduction formula: `(complexity * 2) + (bare_excepts * 10) + (nesting_depth * 5) + (todos * 2)`, capped at 50 on parse error, floored at 5.
* **Not ML-derived.** It is the primary trusted signal, not the ML classifier.

> Interview talking point: "We knew the ML piece was experimental, so we made the deterministic health score the primary signal and clearly flagged the classifier output as experimental."

---

## 🧪 Testing (verified)

```
source venv/bin/activate
python -m pytest tests/ -q
```

**Verified result: 83 passed.**

| Test file | Count | Focus |
|---|---|---|
| `tests/test_web_gateway.py` | 17 | Valid/invalid input, oversized requests, unavailable services, malformed responses, disabled LLM, Markdown export, health endpoints |
| `tests/test_eval_suite.py` | 20 | Evaluator dataset schema, metric calculations (precision/recall/F1, zero denominators), report generation |
| `tests/test_llm_reliability.py` | 46 | LLM retry/failover, quota control, circuit breaker, provider failure handling |

All tests are **offline by default** — they mock all HTTP/MCP calls and consume no Gemini/Groq quota. Rerun the suite to confirm the current result before citing it.

---

## 📡 API Endpoints (accurate examples)

### Web Gateway (port 9000)

`POST /analyze`:
```bash
curl -X POST http://127.0.0.1:9000/analyze \
  -H "Content-Type: application/json" \
  -d '{"filename":"main.py","language":"python","content":"def foo():\n    try:\n        x = 1/0\n    except:\n        pass\n  }'
```

Returns `review`, `bugs`, `refactored`, `docs`, `health_score`, `risk_level`, `metrics`.

`GET /health` / `GET /health/providers` — app health and provider availability (mcp, gemini, groq, redis).

### MCP Core Engine (port 8000)

`POST /code/metrics`:
```bash
curl -X POST http://127.0.0.1:8000/code/metrics \
  -H "Content-Type: application/json" \
  -d '{"filename":"main.py","language":"python","content":"x = 1"}'
```

Returns `cyclomatic_complexity`, `line_count`, `bare_excepts_or_unfreed`, `todo_count`, `nesting_depth`, `health_score` (0–100), `risk_level`, `parse_error`, `none_eq_count`, `ml_classifier: "experimental"`.

Other endpoints: `/code/analyze`, `/code/find_bugs`, `/code/document`, `/code/refactor`.

---

## 🚀 How to Run

### Local (two terminals)

Terminal 1 — MCP Core Engine:
```bash
uvicorn mcp_server:app --reload --port 8000
```

Terminal 2 — Web Gateway:
```bash
python3 web_server.py
```

Then open http://127.0.0.1:9000.

### Docker Compose

Default stub mode (no API keys):
```bash
docker compose up -d --build
```

Live LLM mode (provide keys at runtime, never hardcoded):
```bash
docker compose up -d \
  -e GEMINI_API_KEY="${GEMINI_API_KEY}" \
  -e GROQ_API_KEY="${GROQ_API_KEY}" \
  -e LLM_ENABLED=1 \
  -e LLM_STUB=0 \
  web-server
```

Or use `docker-compose.live.yml` for a dedicated live override.

---

## 🔒 Security (what we do and don't claim)

* API keys live in `.env` (gitignored) and are **never** copied into Docker images or committed.
* Pydantic v2 models validate filename (no traversal), language, and content size.
* Markdown export makes no LLM/MCP calls — renders the payload directly.
* Quarantine store records only minimal metadata (no source code, no secrets, no stack traces).

**Never expose `.env` contents or real API keys** in an interview, repo, or issue.

---

## ⚠️ Honest Limitations (say these out loud)

1. **Gemini timeouts / Groq fallback observed.** Do not claim Gemini was consistently successful.
2. **ML risk classifier is experimental** — 6 synthetic rows, not real-world validated.
3. **Health score is deterministic**, not ML-derived.
4. **Not official MCP** — it's an MCP-style custom FastAPI service.
5. **No production deployment, benchmarks, users, or performance claims** are made. Don't invent them.
6. **Small evaluation dataset** (20 labelled cases) — not sufficient for generalization claims.
7. **Concurrent agent execution** — `run_pipeline()` runs agents concurrently even though LangGraph edges are sequential.

---

## 🧠 Genuine Implementation Decisions (interview answers)

**Q: Why Gemini primary with Groq fallback?**
A: Cost and quota control on the free Gemini tier, with Groq as a low-latency fallback when Gemini is slow or unavailable. There's a circuit breaker so repeated Gemini failures don't keep blocking.

**Q: Why run agents concurrently if the graph is sequential?**
A: `run_pipeline()` calls the four agent nodes directly via `asyncio.gather` so one slow LLM call doesn't stall the whole analysis. The LangGraph edges define the logical pipeline, but the runtime execution is concurrent for responsiveness.

**Q: Why a deterministic health score plus an ML classifier?**
A: The classifier is experimental (6 synthetic rows). We didn't want to rely on it, so the deterministic AST-based health score is the primary trusted signal, and the classifier output is clearly flagged as experimental.

**Q: How do you handle LLM failure in production?**
A: `call_llm()` falls back to Groq; if both fail it returns a clearly marked "unavailable" result while still returning static-analysis metrics. The gateway degrades gracefully — no crash, no fabricated findings.

**Q: Is this production-ready?**
A: It's a well-structured portfolio/demo project with 83 passing offline tests, input validation, graceful degradation, and no hardcoded secrets. It is not a production deployment — no benchmarks, SLAs, or real-user data.

---

## 🔒 What to Never Say / Expose

* Never share `.env` contents or real API keys.
* Never claim Gemini was consistently successful (timeouts observed).
* Never claim the ML classifier is production-validated (6 synthetic rows).
* Never claim official MCP spec compliance (it's MCP-style).
* Never cite users, production deployment, throughput, or uptime that aren't measured.

---

Built by **[Shahbaaz Khan]** — Class of 2022–2026