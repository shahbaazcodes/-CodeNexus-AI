"""
CodeNexus AI — Web Gateway (FastAPI)

Pydantic v2 request/response schemas, validated input, consistent error
formats, safe provider timeouts, single bounded repair, resilient quarantine,
and health endpoints that separate application health from optional provider
availability.

Phase 3 LLM reliability behaviour (agents.py) is preserved unchanged:
LLM_ENABLED=0 / LLM_STUB=1, bounded retries, Groq fallback, no caching of
failures, no fabricated findings, Redis crash-safe.
"""
import os
import json
import re
import httpx
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from agents import run_pipeline, LLM_ENABLED, LLM_STUB

app = FastAPI(title="CodeNexus AI — Web Gateway", version="2.1.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ─── Configuration ──────────────────────────────────────────────────────
MCP_BASE = os.getenv("MCP_BASE", "http://127.0.0.1:8000").rstrip("/")
QUARANTINE_PATH = Path(os.getenv("QUARANTINE_PATH", "logs/quarantine.jsonl"))

SUPPORTED_LANGUAGES = {
    "python", "java", "cpp", "c++", "javascript", "js", "typescript", "ts",
}

MAX_FILENAME_LEN = 120
MAX_CONTENT_LEN = 64 * 1024  # 64 KiB
MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "5.0"))

SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# ─── Quarantine (resilient, minimal metadata) ───────────────────────────
class QuarantineStore:
    """Append-only JSONL quarantine that never crashes the app.

    Only minimal, safe metadata is persisted: no source code, no secrets,
    no stack traces. Directory/permission/malformed-file problems are
    swallowed and logged to stderr instead of raising.
    """

    def __init__(self, path: Path):
        self.path = path

    def record(self, code: str, message: str,
               meta: Optional[Dict[str, Any]] = None) -> None:
        entry: Dict[str, Any] = {
            "error_code": code,
            "message": message,
        }
        if meta:
            entry.update(meta)
        line = json.dumps(entry, ensure_ascii=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:  # pragma: no cover - defensive
            import sys
            print(f"[quarantine] unable to write record: {exc}", file=sys.stderr)


quarantine = QuarantineStore(QUARANTINE_PATH)

# ─── Pydantic v2 schemas ────────────────────────────────────────────────
class CodeMetrics(BaseModel):
    cyclomatic_complexity: int
    line_count: int
    bare_excepts_or_unfreed: int
    todo_count: int
    nesting_depth: int
    health_score: int
    risk_level: str
    parse_error: Optional[str] = None
    none_eq_count: Optional[int] = None
    ml_classifier: Optional[str] = None


class CodeInput(BaseModel):
    filename: str = Field(default="main.py", max_length=MAX_FILENAME_LEN)
    language: str = Field(default="python", max_length=32)
    content: str = Field(max_length=MAX_CONTENT_LEN)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("filename must not be empty")
        name = v.strip()
        if len(name) > MAX_FILENAME_LEN:
            raise ValueError(f"filename exceeds {MAX_FILENAME_LEN} characters")
        if not SAFE_FILENAME_RE.match(name):
            raise ValueError(
                "filename may only contain letters, digits, dots, dashes "
                "and underscores"
            )
        if name in {".", ".."} or name.startswith("."):
            raise ValueError(
                "filename must not be '.' , '..' or a hidden file"
            )
        return name

    @field_validator("language")
    @classmethod
    def _validate_language(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("language must not be empty")
        lang = v.strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported language '{v}'. Supported: "
                f"{sorted(SUPPORTED_LANGUAGES)}"
            )
        alias = {"c++": "cpp", "js": "javascript", "ts": "typescript"}
        return alias.get(lang, lang)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "content must not be empty or whitespace-only"
            )
        if len(v) > MAX_CONTENT_LEN:
            raise ValueError(f"content exceeds {MAX_CONTENT_LEN} bytes")
        return v

class AgentAnalysisResponse(BaseModel):
    review: str
    bugs: str
    refactored: str
    docs: str
    health_score: int
    risk_level: str
    metrics: Optional[CodeMetrics] = None

    @model_validator(mode="after")
    def _check_fields(self) -> "AgentAnalysisResponse":
        for field in ("review", "bugs", "refactored", "docs"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"field '{field}' must be a non-empty string")
        if not isinstance(self.health_score, int) or not (0 <= self.health_score <= 100):
            raise ValueError("field 'health_score' must be an integer 0-100")
        if not isinstance(self.risk_level, str) or not self.risk_level.strip():
            raise ValueError("field 'risk_level' must be a non-empty string")
        return self


class ErrorResponse(BaseModel):
    error: Dict[str, Any]


class ExportRequest(BaseModel):
    filename: str = Field(max_length=MAX_FILENAME_LEN)
    language: str = Field(max_length=32)
    content: str = Field(max_length=MAX_CONTENT_LEN)
    review: str = ""
    bugs: str = ""
    refactored: str = ""
    docs: str = ""
    health_score: int = Field(default=85, ge=0, le=100)
    risk_level: str = Field(default="Low")

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("filename must not be empty")
        name = v.strip()
        if len(name) > MAX_FILENAME_LEN:
            raise ValueError(f"filename exceeds {MAX_FILENAME_LEN} characters")
        if not SAFE_FILENAME_RE.match(name):
            raise ValueError(
                "filename may only contain letters, digits, dots, dashes "
                "and underscores"
            )
        if name in {".", ".."} or name.startswith("."):
            raise ValueError(
                "filename must not be '.' , '..' or a hidden file"
            )
        return name

    @field_validator("language")
    @classmethod
    def _validate_language(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("language must not be empty")
        lang = v.strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"unsupported language '{v}'. Supported: "
                f"{sorted(SUPPORTED_LANGUAGES)}"
            )
        alias = {"c++": "cpp", "js": "javascript", "ts": "typescript"}
        return alias.get(lang, lang)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v: str) -> str:
        if len(v) > MAX_CONTENT_LEN:
            raise ValueError(f"content exceeds {MAX_CONTENT_LEN} bytes")
        return v

quarantine = QuarantineStore(QUARANTINE_PATH)


# ─── Helper: consistent error responses ─────────────────────────────────
def _error_response(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


async def _safe_metrics(mcp_payload: Dict[str, Any]) -> tuple:
    """Call MCP /code/metrics with a bounded timeout; never leak internals.

    Returns (health_score, risk_level, metrics_or_None). On any failure
    falls back to safe defaults so the gateway stays online.
    """
    health_score = 88
    risk_level = "Low"
    metrics: Optional[CodeMetrics] = None
    try:
        async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as client:
            resp = await client.post(
                f"{MCP_BASE}/code/metrics",
                json=mcp_payload,
            )
        if resp.status_code == 200:
            data = resp.json()
            health_score = int(data.get("health_score", 88))
            risk_level = str(data.get("risk_level", "Low"))
            try:
                metrics = CodeMetrics.model_validate(data)
            except Exception:
                metrics = None
    except httpx.TimeoutException:
        pass
    except httpx.HTTPError:
        pass
    except Exception:
        pass
    return health_score, risk_level, metrics


def _validate_pipeline_output(raw: Any) -> AgentAnalysisResponse:
    """Validate pipeline output and report schema locations without input data."""
    if not isinstance(raw, dict):
        raise ValueError("pipeline output is not an object")
    try:
        return AgentAnalysisResponse.model_validate(raw)
    except ValidationError as exc:
        # Log only field locations and error types, never model output or source code.
        issues = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ())),
                "type": item.get("type", "validation_error"),
            }
            for item in exc.errors(include_input=False)
        ]
        raise ValueError(f"pipeline schema validation failed: {issues}") from exc

# ─── Routes ─────────────────────────────────────────────────────────────
@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health() -> Dict[str, Any]:
    """Application health. Always reachable; does not depend on Redis/MCP/LLM."""
    return {
        "status": "online",
        "service": "CodeNexus Web Gateway",
        "version": "2.1.0",
        "llm_enabled": LLM_ENABLED,
        "llm_stub": LLM_STUB,
    }


@app.get("/health/providers")
def health_providers() -> Dict[str, Any]:
    """Optional provider availability. Does not gate application health."""
    providers: Dict[str, Any] = {
        "mcp": {"configured": bool(MCP_BASE), "reachable": None},
        "gemini": {"configured": False, "reachable": None},
        "groq": {"configured": False, "reachable": None},
        "redis": {"configured": False, "reachable": None},
    }
    try:
        import socket
        host = MCP_BASE.replace("http://", "").replace("https://", "").split(":")[0]
        port = 8000
        with socket.create_connection((host, port), timeout=1.5):
            providers["mcp"]["reachable"] = True
    except Exception:
        providers["mcp"]["reachable"] = False

    from agents import GEMINI_API_KEY, GROQ_API_KEY  # type: ignore
    providers["gemini"]["configured"] = bool(GEMINI_API_KEY)
    providers["groq"]["configured"] = bool(GROQ_API_KEY)

    providers["redis"]["configured"] = True
    return {"providers": providers}


@app.post("/analyze", response_model=AgentAnalysisResponse)
async def analyze_code(payload: CodeInput) -> AgentAnalysisResponse:
    """Analyze code via MCP metrics + LangGraph agent pipeline.

    MCP metrics are fetched once with a bounded timeout; on failure the
    gateway degrades to safe defaults. The LangGraph pipeline is invoked
    exactly once and is never retried merely because it raised. If the
    pipeline returns malformed structured output (not a dict), at most one
    bounded repair attempt is made. If output remains invalid, a 422 is
    returned with only minimal metadata quarantined (no source, no keys,
    no paths).
    """
    mcp_payload = {
        "filename": payload.filename,
        "content": payload.content,
        "language": payload.language,
    }

    # MCP metrics: one bounded call, degrade gracefully on failure.
    health_score, risk_level, metrics = await _safe_metrics(mcp_payload)

    # LangGraph pipeline: invoke exactly once; never retry on exception.
    raw: Any = None
    pipeline_error: Optional[str] = None
    try:
        raw = await run_pipeline(
            payload.filename, payload.language, payload.content
        )
    except Exception as exc:
        pipeline_error = f"{type(exc).__name__}: {exc}"

    # If pipeline raised — degrade gracefully, do NOT retry.
    if pipeline_error is not None:
        quarantine.record(
            code="PIPELINE_ERROR",
            message=pipeline_error,
        )
        result = AgentAnalysisResponse(
            review="### ⚠️ Review Unavailable\n\nPipeline execution failed.",
            bugs="### ⚠️ Bug Analysis Unavailable\n\nPipeline execution failed.",
            refactored=payload.content,
            docs=payload.content,
            health_score=health_score,
            risk_level=risk_level,
        )
        if metrics is not None:
            result.metrics = metrics
        return result

    # Pipeline returned — now validate output type.
    # At most one repair attempt for malformed (non-dict) output.
    repair_attempted = False
    if not isinstance(raw, dict):
        # First attempt: repair by requesting complete structured output.
        try:
            repair_content = (
                payload.content
                + "\n\n# REPAIR: Ensure review, bugs, refactored, docs, "
                  "health_score, risk_level are all present and non-empty."
            )
            repaired = await run_pipeline(
                payload.filename, payload.language, repair_content
            )
            if isinstance(repaired, dict):
                raw = repaired
                repair_attempted = True
            # else: fall through to 422 below
        except Exception:
            pass

    # If after repair attempt, raw is still not a dict, return 422.
    if not isinstance(raw, dict):
        quarantine.record(
            code="INVALID_PIPELINE_OUTPUT",
            message="LangGraph pipeline returned invalid or missing structured output "
            "(repair_attempted=%s)" % repair_attempted,
        )
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_PIPELINE_OUTPUT",
                              "message": "Analysis pipeline returned invalid structured output. "
                                      "Please retry with a simpler code sample."}},
        )

    # Check for missing required fields in dict output — return 422 if any are missing.
    required_fields = ["review", "bugs", "refactored", "docs"]
    if any(not raw.get(f) for f in required_fields):
        quarantine.record(
            code="INVALID_PIPELINE_OUTPUT",
            message="LangGraph pipeline returned dict with missing required fields",
        )
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_PIPELINE_OUTPUT",
                              "message": "Analysis pipeline returned incomplete output. "
                                      "Missing required fields."}},
        )

    # At most one repair attempt for malformed structured output only
    # (a non-dict return), never for exceptions.
    repair_attempted = False

    # Build the result from pipeline output, adding health metrics.
    # The pipeline returns review, bugs, refactored, docs; we add health_score
    # and risk_level from the MCP metrics call.
    if metrics is not None:
        result = AgentAnalysisResponse(
            review=raw.get("review"),
            bugs=raw.get("bugs"),
            refactored=raw.get("refactored", payload.content),
            docs=raw.get("docs", payload.content),
            health_score=metrics.health_score,
            risk_level=metrics.risk_level,
            metrics=metrics,
        )
    else:
        result = AgentAnalysisResponse(
            review=raw.get("review"),
            bugs=raw.get("bugs"),
            refactored=raw.get("refactored", payload.content),
            docs=raw.get("docs", payload.content),
            health_score=health_score,
            risk_level=risk_level,
        )

    # Validate the complete structured output at the API boundary.
    try:
        result = AgentAnalysisResponse.model_validate(result.model_dump())
    except ValidationError as exc:
        # Log only field locations and error types, never model output or source code.
        issues = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ())),
                "type": item.get("type", "validation_error"),
            }
            for item in exc.errors(include_input=False)
        ]
        raise ValueError(f"pipeline schema validation failed: {issues}") from exc

    return result


@app.post("/export/markdown")
async def export_markdown(payload: ExportRequest) -> Dict[str, str]:
    """Export the *actual* analysis result as Markdown.

    No LLM or MCP calls are made — the supplied analysis payload is rendered
    directly, so export is safe even when providers are unavailable.
    """
    language_display = payload.language.capitalize()
    fence_lang = payload.language
    if fence_lang == "c++":
        fence_lang = "cpp"

    md = (
        f"# 🛡️ CodeNexus AI — Code Audit Report\n\n"
        f"**Filename:** `{payload.filename}`\n"
        f"**Language:** `{language_display}`\n"
        f"**Code Health Score:** `{payload.health_score}/100`\n"
        f"**ML Risk Level:** `{payload.risk_level}`\n\n"
        f"---\n\n"
        f"## 🔍 Architecture & Performance Review\n\n"
        f"{payload.review or 'No issues identified.'}\n\n"
        f"---\n\n"
        f"## ✳ Bug & Vulnerability Inspection\n\n"
        f"{payload.bugs or 'No bugs detected.'}\n\n"
        f"---\n\n"
        f"## ⚡ Production Refactored Code\n\n"
        f"```{fence_lang}\n{payload.refactored or payload.content}\n```\n\n"
        f"---\n\n"
        f"## 📄 Documented Implementation\n\n"
        f"```{fence_lang}\n{payload.docs or payload.content}\n```"
    )
    return {"content": md, "filename": "CodeNexus_Audit_Report.md"}


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    quarantine.record(
        code="VALUE_ERROR",
        message=str(exc)[:200],
    )
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "INVALID_REQUEST", "message": "Invalid request payload."}},
    )


@app.exception_handler(Exception)
async def _unexpected_handler(request: Request, exc: Exception):
    quarantine.record(
        code="UNEXPECTED_ERROR",
        message=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9000)