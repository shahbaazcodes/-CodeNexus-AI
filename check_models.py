"""Small pre-flight checks for CodeNexus AI providers and MCP health."""

import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()  # Do not override environment variables already set by the shell.

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
MCP_BASE = os.getenv("MCP_BASE", "http://127.0.0.1:8000").rstrip("/")


def classify_error(status_code: int) -> str:
    if status_code in (401, 403):
        return "AUTH_FAILED"
    if status_code == 404:
        return "MODEL_OR_ENDPOINT_NOT_FOUND"
    if status_code == 429:
        return "RATE_LIMITED_OR_QUOTA_EXHAUSTED"
    if status_code in (500, 502, 503, 504):
        return "SERVICE_UNAVAILABLE"
    if 400 <= status_code < 500:
        return "BAD_REQUEST_OR_UNSUPPORTED_CONFIGURATION"
    return f"HTTP_{status_code}"


async def test_gemini(client: httpx.AsyncClient) -> str:
    print(f"Testing Gemini primary model: {GEMINI_MODEL}")
    if not GEMINI_API_KEY:
        print("SKIPPED: GEMINI_API_KEY is not configured")
        return "SKIPPED"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": "Reply with OK."}]}],
        "generationConfig": {"maxOutputTokens": 5, "temperature": 0},
    }
    try:
        response = await client.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=10.0,
        )
    except httpx.TimeoutException:
        print("NETWORK_ERROR: Gemini request timed out")
        return "NETWORK_ERROR"
    except httpx.HTTPError as exc:
        print(f"NETWORK_ERROR: Gemini request failed ({type(exc).__name__})")
        return "NETWORK_ERROR"
    if response.is_success:
        print("PASS: Gemini returned a successful response")
        return "PASS"
    result = classify_error(response.status_code)
    print(f"{result}: Gemini returned HTTP {response.status_code}")
    return result


async def test_groq(client: httpx.AsyncClient) -> str:
    print(f"Testing Groq fallback model: {GROQ_MODEL}")
    if not GROQ_API_KEY:
        print("SKIPPED: GROQ_API_KEY is not configured")
        return "SKIPPED"
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 5,
        "temperature": 0,
    }
    try:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
            timeout=10.0,
        )
    except httpx.TimeoutException:
        print("NETWORK_ERROR: Groq request timed out")
        return "NETWORK_ERROR"
    except httpx.HTTPError as exc:
        print(f"NETWORK_ERROR: Groq request failed ({type(exc).__name__})")
        return "NETWORK_ERROR"
    if response.is_success:
        print("PASS: Groq returned a successful response")
        return "PASS"
    result = classify_error(response.status_code)
    print(f"{result}: Groq returned HTTP {response.status_code}")
    return result


async def test_mcp(client: httpx.AsyncClient) -> str:
    print(f"Testing MCP health endpoint: {MCP_BASE}")
    for path in ("/health", "/healthz", "/"):
        try:
            response = await client.get(f"{MCP_BASE}{path}", timeout=3.0)
        except httpx.TimeoutException:
            print("NETWORK_ERROR: MCP health request timed out")
            return "NETWORK_ERROR"
        except httpx.HTTPError as exc:
            print(f"NETWORK_ERROR: MCP request failed ({type(exc).__name__})")
            return "NETWORK_ERROR"
        if response.is_success:
            print(f"PASS: MCP responded successfully at {path}")
            return "PASS"
        if response.status_code != 404:
            result = classify_error(response.status_code)
            print(f"{result}: MCP returned HTTP {response.status_code} at {path}")
            return result
    print("UNVERIFIED: No common health endpoint found; MCP health route may differ")
    return "UNVERIFIED"


async def main() -> None:
    print("--- CodeNexus AI Pre-flight Check ---")
    print(f"Gemini model: {GEMINI_MODEL}")
    print(f"Groq model: {GROQ_MODEL}")
    print(f"MCP base: {MCP_BASE}")
    print("Each configured provider check makes one minimal API request.\n")
    async with httpx.AsyncClient() as client:
        results = {
            "Gemini primary": await test_gemini(client),
            "Groq fallback": await test_groq(client),
            "MCP health": await test_mcp(client),
        }
    print("\n--- Summary ---")
    for name, result in results.items():
        print(f"{name}: {result}")


if __name__ == "__main__":
    asyncio.run(main())
