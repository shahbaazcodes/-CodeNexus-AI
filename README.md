# 🚀 Agentic AI Code Reviewer

**A Multi-Agent System for Automated Code Analysis, Debugging, and Refactoring.**

Powered by **Model Context Protocol (MCP)**, **Google Gemini 2.5**, and **FastAPI**.

---

## 📌 Project Overview
This project allows developers to upload Python code and receive instant, professional-grade feedback from a team of AI Agents. Unlike standard chatbots, this system uses an **Agentic Workflow** where specialized AI agents collaborate to solve complex problems.

### 🤖 The Agent Team
1.  **🔍 Reviewer Agent:** Analyzes code for style violations, technical debt, and bad practices.
2.  **🐞 Bug Hunter Agent:** Detects logical errors, security risks (like bare `except`), and Python-specific traps (like mutable default args).
3.  **🛠 Refactor Agent:** Automatically rewrites the code to be production-ready, cleaner, and more efficient.
4.  **📝 Documentation Agent:** Generates Google-Style Docstrings and adds professional comments to the fixed code.

---

## ⚙️ Tech Stack
* **Backend:** Python 3.12+, FastAPI (Async Web Server)
* **Frontend:** HTML5, CSS3 (Dark Mode), Vanilla JS
* **AI Model:** Google Gemini 2.5 Flash (via Google Generative AI API)
* **Architecture:** Client-Server (MCP Pattern)
* **Security:** Environment Variable Management (`python-dotenv`) for API Keys

---

## 🚀 How to Run Locally

### 1. Prerequisites
* Python 3.10 or higher
* A Google Gemini API Key (Free) — only needed for live LLM analysis

### 2. Installation
```bash
# Clone the repository
git clone [https://github.com/yourusername/ai-code-reviewer.git](https://github.com/yourusername/ai-code-reviewer.git)
cd ai-code-reviewer

# Create Virtual Environment
python3 -m venv venv
source venv/bin/activate

# Install Dependencies
pip install -r requirements.txt
```

### 3. Configuration
Create a `.env` file in the root directory and add your key:

```
GEMINI_API_KEY="_YOUR_API_KEY_HERE"
MCP_BASE=http://127.0.0.1:8000
LLM_STUB=0
LLM_ENABLED=1
```

> **Stub mode (no API key required):** Set `LLM_ENABLED=0` and `LLM_STUB=1` in `.env` (or pass them at runtime) to run offline without calling Gemini/Groq. The pipeline then returns deterministic stub text while still performing real MCP static-analysis metrics.

### 4. Start the System
You need to run the Tool Engine and the Web UI in separate terminals.

Terminal 1 (MCP Tool Server):

```bash
uvicorn mcp_server:app --reload --port 8000
```

Terminal 2 (Web Dashboard):

```bash
python3 web_server.py
```

### 5. Usage
Open your browser to http://127.0.0.1:9000 and start analyzing code!

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

🛡️ Security Features
No Hardcoded Secrets: API keys are managed via `.env` and excluded from version control via `.gitignore`.

Input Sanitization: The Web Server validates all incoming code requests using Pydantic models.

🔮 Future Improvements
Add support for Java and C++ analysis.

Integrate directly with GitHub Webhooks for automatic PR reviews.

Add "Download Report" feature to export findings as PDF.

Built by [ SHAHBAAZ KHAN ] | Class of 2022-2026