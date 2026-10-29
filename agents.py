import asyncio
import os
import hashlib
import time
import httpx
import redis
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional, TypedDict
from langgraph.graph import StateGraph, END

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

MCP_BASE = os.getenv("MCP_BASE", "http://127.0.0.1:8000")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip(" '\"\t\r\n")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip(" '\"\t\r\n")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip(" '\\\"\\t\\r\\n")

# ── LLM_ENABLED: when "0", skip all LLM network requests ───────
LLM_ENABLED = os.getenv("LLM_ENABLED", "1").strip() == "1"

# Preserve deterministic stub mode (no network calls)
LLM_STUB = os.getenv("LLM_STUB", "0").lower() == "1"

# Model names configurable through environment variables
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Bounded retry configuration with exponential backoff
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "0.5"))


def _is_retryable_http_status(code: int) -> bool:
    """Return True only for transient HTTP errors: 429 and 5xx."""
    return code == 429 or 500 <= code < 600




# ─── Redis Setup ──────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    redis_client.ping()
    REDIS_AVAILABLE = True
    print("⚡ CodeNexus AI: Redis Active (AST Caching + Multi-LLM Router Enabled)")
except Exception:
    REDIS_AVAILABLE = False
    redis_client = None
    print("ℹ️ CodeNexus AI: Redis Offline — Direct LLM Fallback Active")

class AgentState(TypedDict):
    filename: str
    content: str
    language: str
    review: str
    bugs: str
    refactored: str
    docs: str

async def call_groq(prompt: str) -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "You are a Senior Staff Software Engineer and Security Auditor. Provide thorough, detailed code analysis with deep explanations and clear markdown formatting."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 2048
    }

    last_response = None
    for attempt in range(MAX_RETRIES):
        backoff = BASE_BACKOFF * (2 ** attempt)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, headers=headers, json=payload)
                last_response = r
                if r.status_code == 200:
                    print(f"⚡ [Groq Success] GPT OSS 120B response generated!")
                    return r.json()["choices"][0]["message"]["content"]
                elif _is_retryable_http_status(r.status_code):
                    if attempt < MAX_RETRIES - 1:
                        print(f"⚠️ [Groq Retry] HTTP {r.status_code} on attempt {attempt + 1}/{MAX_RETRIES}, retrying in {backoff:.1f}s...")
                        await asyncio.sleep(backoff)
                        continue
                else:
                    print(f"⚠️ [Groq Warning] HTTP {r.status_code} (not retrying - permanent error)")
                    return None
        except Exception as e:
            print(f"⚠️ [Groq Warning]: {e}")
            if attempt < MAX_RETRIES - 1:
                print(f"⚠️ [Groq Retry] Attempt {attempt + 1}/{MAX_RETRIES} failed, retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                continue
    print(f"❌ [Groq Failed] All {MAX_RETRIES} attempts exhausted")
    return None

# ─── Gemini API Call ─────────────────────────────────────────
async def call_gemini_raw(prompt: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None
    models = [PRIMARY_MODEL, FALLBACK_MODEL]
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(MAX_RETRIES):
            backoff = BASE_BACKOFF * (2 ** attempt)
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    r = await client.post(url, headers=headers, json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        res = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                        if res.strip():
                            print(f"✅ [Gemini Success] {model} response generated!")
                            return res
                    elif _is_retryable_http_status(r.status_code):
                        if attempt < MAX_RETRIES - 1:
                            print(f"⚠️ [Gemini Retry] HTTP {r.status_code} on attempt {attempt + 1}/{MAX_RETRIES} for model {model}, retrying in {backoff:.1f}s...")
                            await asyncio.sleep(backoff)
                            continue
                    else:
                        print(f"⚠️ [Gemini Warning] HTTP {r.status_code} (not retrying - permanent error) for model {model}")
                        break  # Try next model
            except Exception as e:
                print(f"⚠️ [Gemini Warning]: {e}")
                if attempt < MAX_RETRIES - 1:
                    print(f"⚠️ [Gemini Retry] Attempt {attempt + 1}/{MAX_RETRIES} failed for model {model}, retrying in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    continue
        print(f"❌ [Gemini Failed] All {MAX_RETRIES} attempts exhausted for model {model}")
    return None

# ─── Universal Multi-LLM Router ─────────────────────────────
async def call_llm(prompt: str, language: str = "") -> str:
    # Honor deterministic stub mode (no network calls)
    if LLM_STUB:
        return generate_deep_fallback(prompt, language)

    # LLM_DISABLED: skip all network requests, return clearly marked unavailable
    if not LLM_ENABLED:
        return _unavailable_result(prompt, language, "LLM_DISABLED")

    # 1. Check Redis Cache
    prompt_hash = hashlib.sha256(f"{language}:{prompt}".encode("utf-8")).hexdigest()
    cache_key = f"codenexus:cache:{prompt_hash}"

    if REDIS_AVAILABLE:
        try:
            cached_val = redis_client.get(cache_key)
            if cached_val:
                print(f"⚡ [Redis Hit] Hash {prompt_hash[:8]}... served instantly.")
                return cached_val
        except Exception:
            pass

    provider_used = None

    # 2. Try Gemini Primary
    res = await call_gemini_raw(prompt)
    if res:
        provider_used = "gemini"
    else:
        # 3. Fallback to Groq API if Gemini fails
        print("🔄 [Failover] Gemini unavailable! Calling Groq...")
        res = await call_groq(prompt)
        if res:
            provider_used = "groq"

    # 4. If all providers fail or LLM disabled, return honest unavailable result
    if not res:
        print("⚠️ [Offline] All LLM providers unavailable.")
        return _unavailable_result(prompt, language, "PROVIDERS_FAILED")

    # Cache only successful provider responses
    if REDIS_AVAILABLE:
        try:
            redis_client.setex(cache_key, 86400, res)
        except Exception:
            pass

    return res


def _unavailable_result(prompt: str, language: str, reason: str) -> str:
    """Return a clearly marked unavailable result that downstream code can detect."""
    p_lower = prompt.lower()
    lang = language.capitalize() or "Python"

    if "review" in p_lower:
        section = "Review"
    elif "bug" in p_lower or "vulnerability" in p_lower:
        section = "Bug Analysis"
    elif "refactor" in p_lower:
        section = "Refactoring"
    else:
        section = "Documentation"

    reason_map = {
        "LLM_DISABLED": "LLM analysis disabled via LLM_ENABLED=0",
        "PROVIDERS_FAILED": "All LLM providers failed or returned errors"
    }
    reason_text = reason_map.get(reason, reason)

    return (
        f"### ⚠️ {section} Unavailable ({lang})\n\n"
        f"**Reason**: {reason_text}\n\n"
        f"**Static analysis findings** (from local scanners) are still available in the MCP response.\n"
        f"LLM-enhanced {section.lower()} could not be generated.\n\n"
        f"---"
    )

# ─── Deep Structured Fallback Generator ────────────────────
def generate_deep_fallback(prompt: str, language: str) -> str:
    """Deterministic stub response when LLM_STUB=1 (no network calls).

    Returns clearly marked stub output so downstream code can distinguish
    it from actual LLM-generated analysis.
    """
    p_lower = prompt.lower()
    lang = language.capitalize() or "Python"

    if "review" in p_lower:
        return (
            f"### 🛡️ {lang} Review — Stub Mode (No LLM)\n\n"
            f"*LLM analysis disabled. Run with LLM_ENABLED=1 and valid API keys for real analysis.*\\n\\n"
            f"- Enable LLM connectivity for architectural recommendations.\n"
        )
    elif "bug" in p_lower or "vulnerability" in p_lower:
        return (
            f"### ✳ {lang} Bug Inspection — Stub Mode (No LLM)\n\n"
            f"*LLM analysis disabled. Run with LLM_ENABLED=1 and valid API keys for real analysis.*\n\n"
            f"- Static analysis from scanners is available via MCP.\n"
            f"- Enable LLM connectivity for detailed vulnerability reasoning.\n"
        )
    elif "refactor" in p_lower:
        return (
            f"// {lang} Refactored Code — Stub Mode (No LLM)\n"
            f"// *LLM refactoring disabled. Enable LLM_ENABLED=1 for real analysis.*\n"
            f"// Production refactoring will populate when API connectivity is restored.\n"
        )
    else:
        return (
            f"/** {lang} Documentation — Stub Mode (No LLM)\n"
            f" * *LLM documentation disabled. Enable LLM_ENABLED=1 for real analysis.*\n"
            f" * Detailed contract descriptions and inline comments will populate when API connectivity is restored.\n"
            f" */"
        )


# ─── MCP Tool Call Helper ────────────────────────────────────
async def call_mcp(endpoint: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{MCP_BASE}/code/{endpoint}", json=payload)
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

# ─── LangGraph Agent Nodes ─────────────────────────────────
async def reviewer_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("analyze", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    raw_issues = mcp_res.get("issues", ["No static syntax issues found."])
    issues_str = "\n- ".join(raw_issues)

    prompt = (
        f"You are a Senior Software Architect conducting an in-depth code review.\n"
        f"Language: {state['language']}\n"
        f"Source Code:\n```{state['language']}\n{state['content']}\n```\n\n"
        f"Static Analysis Findings:\n- {issues_str}\n\n"
        f"Provide a comprehensive, detailed architectural review. Cover:\n"
        f"1. Code Health & Design Patterns\n"
        f"2. Performance & Algorithmic Complexity\n"
        f"3. Actionable Engineering Improvements."
    )
    review = await call_llm(prompt, state["language"])
    return {"review": review}

async def bug_hunter_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("find_bugs", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    raw_bugs = mcp_res.get("bugs", ["No static bugs found."])
    bugs_str = "\n- ".join(raw_bugs)

    prompt = (
        f"You are a Lead Security Engineer & Bug Hunter.\n"
        f"Language: {state['language']}\n"
        f"Source Code:\n```{state['language']}\n{state['content']}\n```\n\n"
        f"Static Scanner Hints:\n- {bugs_str}\n\n"
        f"Perform a thorough logical bug inspection. For each issue:\n"
        f"- Explain WHY the bug occurs\n"
        f"- Point out the exact code flaw\n"
        f"- Detail how to fix it step by step."
    )
    res_bugs = await call_llm(prompt, state["language"])
    return {"bugs": res_bugs}

async def refactor_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("refactor", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    ref_base = mcp_res.get("refactored_code", state["content"])
    prompt = (
        f"Refactor this {state['language']} code to production standards.\n"
        f"Return ONLY valid refactored {state['language']} code inside code blocks:\n"
        f"```{state['language']}\n{ref_base}\n```"
    )
    refactored = await call_llm(prompt, state["language"])
    return {"refactored": refactored if len(refactored) > 10 else ref_base}

async def docs_node(state: AgentState) -> dict:
    mcp_res = await call_mcp("document", {"filename": state["filename"], "content": state["content"], "language": state["language"]})
    doc_base = mcp_res.get("documented_code", state["content"])
    prompt = (
        f"Add production docstrings and inline commentary to this {state['language']} code:\n"
        f"```{state['language']}\n{doc_base}\n```"
    )
    docs = await call_llm(prompt, state["language"])
    return {"docs": docs if len(docs) > 10 else doc_base}

# ─── LangGraph Graph Setup ──────────────────────────────────
workflow = StateGraph(AgentState)
workflow.add_node("reviewer", reviewer_node)
workflow.add_node("bug_hunter", bug_hunter_node)
workflow.add_node("refactor", refactor_node)
workflow.add_node("documentation", docs_node)

workflow.set_entry_point("reviewer")
workflow.add_edge("reviewer", "bug_hunter")
workflow.add_edge("bug_hunter", "refactor")
workflow.add_edge("refactor", "documentation")
workflow.add_edge("documentation", END)

graph_engine = workflow.compile()

async def run_pipeline(filename: str, language: str, content: str) -> dict:
    initial_state = {
        "filename": filename,
        "content": content,
        "language": language,
        "review": "",
        "bugs": "",
        "refactored": "",
        "docs": ""
    }
    return await graph_engine.ainvoke(initial_state)
