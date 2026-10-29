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
* A Google Gemini API Key (Free)

### 2. Installation
```bash
# Clone the repository
git clone [https://github.com/yourusername/ai-code-reviewer.git](https://github.com/yourusername/ai-code-reviewer.git)
cd ai-code-reviewer

# Create Virtual Environment
python3 -m venv venv
source venv/bin/activate

# Install Dependencies
pip install fastapi uvicorn httpx pydantic python-dotenv

##3. Configuration

Create a .env file in the root directory and add your key:

Ini, TOML
GEMINI_API_KEY="_YOUR_API_KEY_HERE"

##4. Start the System

You need to run the Tool Engine and the Web UI in separate terminals.

Terminal 1 (MCP Tool Server):

Bash
uvicorn mcp_server:app --reload --port 8000
Terminal 2 (Web Dashboard):

Bash
python3 web_server.py

##5. Usage

Open your browser to http://127.0.0.1:8001 and start analyzing code!

🛡️ Security Features
No Hardcoded Secrets: API keys are managed via .env and excluded from version control via .gitignore.

Input Sanitization: The Web Server validates all incoming code requests using Pydantic models.

🔮 Future Improvements
Add support for Java and C++ analysis.

Integrate directly with GitHub Webhooks for automatic PR reviews.

Add "Download Report" feature to export findings as PDF.

Built by [ SHAHBAAZ KHAN ] | Class of 2022-2026