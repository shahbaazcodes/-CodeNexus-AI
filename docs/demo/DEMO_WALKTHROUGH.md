# CodeNexus AI — Portfolio Demo Walkthrough

**Purpose:** A short, honest, technically accurate walkthrough you can present to AI engineering recruiters and interviewers. Every claim below is drawn from the actual source code (`web_server.py`, `mcp_server.py`, `agents.py`, `.env.example`, `templates/index.html`, `static/script.js`), not from marketing material.

> **Status:** Prepared for live demonstration. No source files were modified to produce this document. The existing architecture, UI, dependencies, services, and working-tree changes are fully preserved.

---

## 1. Project Introduction (30 seconds)

**CodeNexus AI** is a multi-agent code review system. A developer uploads a source-code snippet, and a team of four specialized AI agents — **Reviewer**, **Bug Hunter**, **Refactor**, and **Documentation** — analyze it through an MCP-style pipeline that combines **static analysis** (Python AST metrics + an experimental ML risk classifier) with **LLM-enhanced review** (Google Gemini primary, Groq fallback). The system returns a **health score (0–100)**, a **risk classification**, refactored code, docstrings, and a downloadable **Markdown audit report** — all through a FastAPI web gateway with a dark-themed vanilla HTML/CSS/JS frontend.

**Presented as:** a portfolio-capable, end-to-end code-review demo that runs entirely locally via Docker Compose.

---

## 2. Problem Being Solved

Code review is essential but time-consuming and inconsistent across reviewers. CodeNexus AI addresses this by:

* Applying deterministic static analysis (AST metrics, bug patterns) **plus** LLM-enhanced review in one pipeline.
* Giving reviewers a consistent **health score** and **risk level** so they can triage which snippets need human attention.
* Producing **refactored code** and **documentation** automatically, not just a list of complaints.
* Letting teams run it **locally or in Docker**, with stub mode available when no LLM API keys are configured.
---

## 4. Four-Agent Workflow

Each agent calls the shared LLM router (`call_llm`), which routes to **Gemini (primary)** first and falls back to **Groq** on failure, with Redis caching and a Gemini circuit breaker.

| Agent | Static MCP call first | LLM task | Output field |
|---|---|---|---|
| **Reviewer** | `/code/analyze` | Architectural & performance review | `review` |
| **Bug Hunter** | `/code/find_bugs` | Detailed bug & vulnerability inspection with fixes | `bugs` |
| **Refactor** | `/code/refactor` | Productionize the code | `refactored` |
| **Documentation** | `/code/document` | Docstrings and inline comments | `docs` |

Outputs are merged and returned together with `health_score` and `risk_level` from the MCP metrics call.

---

## 5. MCP and AST Analysis Explanation

The **MCP Core Engine** (`mcp_server.py`) performs deterministic static analysis before any LLM is invoked:

1. **AST feature extraction** (`extract_python_ast_features`): parses Python source into an AST and computes:
   - Cyclomatic complexity
   - Line count
   - Bare `except:` count
   - TODO count
   - Nesting depth
   - `== None` comparison count (a Python anti-pattern; correct form is `is None`)
2. **Experimental ML risk classifier:** a `RandomForestClassifier` trained on **six synthetic rows** predicts Low/Medium/High/Critical risk. Flagged `ml_classifier: "experimental"` in every response.
3. **Deterministic health score (0–100):** `100 - ((complexity*2) + (bare_excepts*10) + (nesting_depth*5) + (todos*2))`, floored at 5 and capped at 50 on parse error. This is **not ML-derived**; it is the primary trusted signal.
4. **Static tools:** `/code/analyze`, `/code/find_bugs`, `/code/refactor`, `/code/document` — rule-based (e.g., replacing `== None` → `is None`, `except:` → `except Exception:`).

**Honest framing:** The deterministic health score is the part you can stand behind. The ML risk level is a rough heuristic only.

---

## 6. Redis Caching Explanation

Redis (port 6379) is used by `agents.py` for **LLM response caching**:

* Before calling Gemini or Groq, the router checks Redis for a cached response keyed by the content + routing-config fingerprint.
* On a cache hit, the LLM call is skipped entirely — no provider latency, no quota consumed.
* Caching is **crash-safe**: if Redis is unavailable, the app degrades to direct LLM calls (or stub mode) without crashing.
* **Failures are never cached**, so a transient provider error won't poison the cache.
* Cache invalidation is driven by the routing-config fingerprint (provider/model identities + generation settings), so changing a model or token limit invalidates prior entries.

**Demo-safe note:** You can demonstrate caching by submitting the same snippet twice and observing the second response is faster, but do not claim Redis holds source code or secrets — it only holds LLM responses keyed by content hash.

---

## 7. Gemini Primary / Groq Fallback Explanation

Configured in `agents.py` and `.env.example`:

| Setting | Default | Source |
|---|---|---|
| Primary provider | **Gemini** | `call_llm()` tries Gemini first |
| Primary model | `gemini-3.6-flash` | `.env.example`, `agents.py` line 28 |
| Fallback provider | **Groq** | `call_llm()` falls back to Groq on Gemini failure |
| Fallback model | `openai/gpt-oss-120b` | `.env.example`, `agents.py` line 29 |

**`call_llm()` behavior:**
1. `LLM_STUB=1` → deterministic stub text, no network calls (ideal for offline demos).
2. `LLM_ENABLED=0` → "LLM analysis disabled", no network calls.
3. Otherwise → Redis cache check → **Gemini primary** → on failure → **Groq fallback**.
4. If all providers fail → clearly marked "unavailable" result; static metrics still returned.

**Gemini circuit breaker:** A process-local breaker tracks consecutive Gemini transient failures. When the threshold is reached, the circuit opens and requests go straight to Groq without contacting Gemini. A recovery probe tests whether Gemini is back after a cooldown.

**Honest limitation (say this out loud):** In live validation, **Gemini experienced timeouts and the Groq fallback was observed**. Do **not** claim Gemini was consistently successful. Always design for fallback.

---

## 8. Experimental ML Limitations

Be transparent about this with interviewers:

* The `RandomForestClassifier` in `mcp_server.py` is trained on **six synthetic rows** with manually assigned labels.
* It has **not** been evaluated on independently labelled real-world code.
* Accuracy, precision, recall, and F1 cannot be meaningfully computed from 6 synthetic samples.
* `risk_level` should be treated as a **rough heuristic**, not a verified prediction.
* The deterministic `health_score` formula is the primary trusted signal; the ML output is explicitly labeled `"ml_classifier": "experimental"` in every response.

> Interview talking point: *"We knew the ML piece was experimental, so we made the deterministic health score the primary signal and clearly flagged the classifier output as experimental."*

---

## 9. Live Demonstration Script (2–3 minutes)

**Prerequisites (already running in this environment):**
* Docker Compose services healthy: `mcp-server` (8000), `web-server` (9000), `redis` (6379).
* Web Gateway accessible at `http://127.0.0.1:9000`.
* `.env` present with `LLM_ENABLED=1`, `LLM_STUB=0` (live LLM) — API keys are in `.env`, **never** shown on screen.

### Minute 0–0:30 — Open the interface
1. Open `http://127.0.0.1:9000` in a browser.
2. Point out the dark-themed editor, language selector (Python/Java/C++/JavaScript/TypeScript), and the two actions: **"⚡ Initiate Analysis"** and **"📄 Export MD"**.
3. Mention the four result tabs (Review / Bugs / Fix / Docs) and the health-score pill and risk badge.

### Minute 0:30–1:30 — Submit the demo example
Paste this Python snippet into the editor:

```python
def get_user_role(user, roles=None):
    if roles is None:
        roles = []
    try:
        if user['status'] == 'active':
            if user['role'] == None:
                return 'guest'
        return 'unknown'
    except:
        pass
```

4. Click **"⚡ Initiate Analysis"**.
5. While the four loaders spin, explain:
   * The gateway calls MCP `/code/metrics` once (bounded 5s timeout, degrades gracefully).
   * All four agents run **concurrently** via `asyncio.gather`.
   * Each agent first calls its static MCP tool, then the LLM router (Gemini primary → Groq fallback, Redis-cached).

### Minute 1:30–2:30 — Review the four agent outputs
Switch through the tabs and narrate what is **actual** vs **static** vs **LLM-generated**:

* **Review tab:** LLM-generated architectural/performance review (markdown table of observations).
* **Bugs tab:** Mix of static MCP hints + LLM bug inspection. Point out:
  * Static: "Anti-pattern: Comparison to None using '==' (Use 'is None')." (from MCP `/code/find_bugs`)
  * Static: "Critical Bug: Bare except clause catches SystemExit & KeyboardInterrupt."
  * LLM: detailed bug analysis with suggested fixes.
* **Fix tab:** Diff view — original vs refactored. Static rule replaces `== None` → `is None` and `except:` → `except Exception:`; the LLM productionizes further.
* **Docs tab:** LLM-generated docstrings/comments plus an MCP docstring header.

### Minute 2:30–3:00 — Health score, risk, and export
1. Note the **health-score pill** (e.g., `Score: 67`) and **risk badge** (e.g., `Risk: Medium`). Explain these come from MCP static analysis (deterministic formula + experimental ML), not the LLM.
2. Click **"📄 Export MD"**. The browser downloads `CodeNexus_Audit_Report.md` containing filename, language, health score, risk level, and all four agent sections. Emphasize: export makes **no LLM or MCP calls** — it renders the payload directly.
3. (Optional, only if Redis is reachable) Re-submit the same snippet to show the faster cached second response.

**Closing line:** *"Static analysis gives us the score and risk; the four LLM agents give the review, bugs, refactor, and docs; Redis caches LLM responses safely; and everything exports to Markdown. The ML risk is experimental — we rely on the deterministic health score as the trusted signal."*

---

## 10. Honest Limitations and Known Issues

State these proactively — recruiters value honesty:

1. **Gemini timeouts / Groq fallback observed.** Do not claim Gemini was consistently successful.
2. **ML risk classifier is experimental** — 6 synthetic rows, not real-world validated. Treat `risk_level` as a heuristic.
3. **Health score is deterministic**, not ML-derived. It is the primary trusted signal.
4. **Not official MCP** — it is an MCP-style custom FastAPI service over HTTP.
5. **No production deployment, benchmarks, users, or performance claims.** Do not invent them.
6. **Small evaluation dataset** (20 labelled cases) — insufficient for generalization claims.
7. **Concurrent agent execution:** `run_pipeline()` runs all four agents concurrently; LangGraph edges are sequential in definition but the pipeline invokes nodes directly and concurrently.
8. **The container's `/` (root) endpoint** returns a 500 due to a pre-existing Jinja2/starlette template incompatibility inside the container image; `/health`, `/analyze`, `/export/markdown`, and MCP `/code/*` all work. The app runs correctly when launched locally. Do not modify the container or source to work around this during the demo; instead run locally on an alternate port if needed.

---

## 11. Suggested Interview Talking Points

* *"CodeNexus AI is a multi-agent code-review system: four agents (Review, Bug Hunter, Refactor, Documentation) run concurrently over an MCP-style pipeline that combines static AST analysis with Gemini-primary/Groq-fallback LLM review."*
* *"We separate concerns: deterministic static analysis (health score, risk level, `== None`, bare `except`) from LLM-generated prose. That way the system degrades gracefully when providers are unavailable."*
* *"The ML risk classifier is explicitly experimental (6 synthetic rows). We made the deterministic health score the primary signal and flag the classifier output — we don't overclaim ML."*
* *"Redis caches LLM responses crash-safe; failures are never cached; if Redis is down the app falls back to direct calls."*
* *"Gemini is primary, Groq is fallback, with a circuit breaker. In practice we saw Gemini timeouts and Groq fallback — we design for that."*
* *"It's not official MCP; it's MCP-style over HTTP. I'm happy to explain the distinction."*
* *"Markdown export makes zero LLM/MCP calls — it renders the payload directly, so it's safe even when providers are down."*

---

## 12. How to Run Locally (for the demo)

Terminal 1 — MCP Core Engine:
```bash
cd /Users/shahbaazkhan/Desktop/major_project_mcp_ai
. venv/bin/activate
uvicorn mcp_server:app --reload --port 8000
```

Terminal 2 — Web Gateway:
```bash
cd /Users/shahbaazkhan/Desktop/major_project_mcp_ai
. venv/bin/activate
python web_server.py   # serves on http://127.0.0.1:9000
```

Then open `http://127.0.0.1:9000`.

Or via Docker Compose (stub mode, no API keys):
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

**For a zero-quota offline demo:** set `LLM_STUB=1` and `LLM_ENABLED=0` — deterministic stub text, no network calls, no API keys needed.

---

*End of walkthrough. All technical details are verified against the source; no capabilities are claimed that are not implemented.*

