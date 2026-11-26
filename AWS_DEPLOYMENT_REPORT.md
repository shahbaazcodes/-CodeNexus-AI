# CodeNexus AI — AWS Deployment Preparation & Cost-Safe Architecture Report

**Prepared for:** Shahbaaz Khan  
**Project:** CodeNexus AI — Multi-Agent Code Review System  
**Repository:** ~/Desktop/major_project_mcp_ai  
**GitHub:** https://github.com/shahbaazcodes/-CodeNexus-AI  
**AWS Account Status:** Newly registered, $100 promotional credits, 182 days remaining, credit expiry May 21, 2027  
**Current month usage:** $0  
**Report date:** 21 November 2026  
**Status:** Inspection, planning and preparation ONLY — no resources created, no changes made

---

## NOTICE TO READER

This report is an **inspection, planning and preparation** document. Per the task rules:

- No AWS resources were created or modified.
- No AWS credentials were connected to Cline.
- No Terraform files were created.
- No application source code, Dockerfiles, or docker-compose files were modified.
- No changes were committed or pushed to GitHub.
- No charges were incurred.

All cost figures are **ESTIMATED** based on current public AWS pricing as of 21 November 2026. You must verify actual pricing in the AWS Pricing Calculator before spending anything. USD→INR conversion uses an approximate rate of **1 USD = 88 INR** (markets fluctuate; check current rate).

## 1. Existing Architecture Findings

### VERIFIED — confirmed from repository inspection

| Component | Technology | Port | Role |
|-----------|-----------|------|------|
| **MCP Core Engine** | FastAPI + Scikit-Learn + Python AST | 8000 | Static analysis (AST metrics), experimental RandomForest ML risk classifier, deterministic health scoring, /code/* endpoints |
| **Web Gateway** | FastAPI + Jinja2 + httpx | 9000 | HTTP frontend, API gateway, MCP router, input validation, Markdown export, serves HTML/CSS/JS frontend |
| **Agent Graph** | LangGraph + Gemini (primary) / Groq (fallback) | N/A | Four agents: Reviewer → Bug Hunter → Refactor → Documentation, run **concurrently** via `asyncio.gather(return_exceptions=True)` |
| **Redis** (optional) | Redis 7-alpine | 6379 | Response caching (crash-safe), content/prompt hashing, provider/model-aware cache keys |
| **Frontend** | HTML5 + CSS3 + Vanilla JS | Served by Web Gateway | Dark-themed code review UI, served by Web Gateway at / |

### Key verified implementation facts

1. **Two separate FastAPI services.** `mcp_server.py` (port 8000) and `web_server.py` (port 9000) are distinct services with separate Dockerfiles (`Dockerfile.mcp`, `Dockerfile.web`).

2. **Frontend served together with Web Gateway.** The vanilla HTML/CSS/JS frontend (`templates/index.html`, `static/`) is served by the Web Gateway, **not** as a separate service. This means the frontend and backend are co-located in one container.

3. **MCP service can operate privately.** In `docker-compose.yml`, `mcp-server` reaches MCP via Compose DNS `MCP_BASE=http://mcp-server:8000`. In production, the host port mapping on mcp-server can be removed so MCP is accessible only within the VPC.

4. **Redis is optional / crash-safe.** `agents.py` uses Redis for caching but gracefully degrades if Redis is unavailable. Redis is reachable via `REDIS_HOST=redis` (Compose DNS), not exposed to host.

5. **Health endpoints exist on both services.**
   - MCP: `GET /health` returns `{"status": "online", "engine": "CodeNexus MCP 2.0..."}`
   - Web Gateway: `GET /health` (checked in Docker HEALTHCHECK)
   - Both have Docker HEALTHCHECK instructions using `urllib.request` to hit `http://127.0.0.1:{port}/health`.

6. **Container health checks exist.** Both `Dockerfile.web` and `Dockerfile.mcp` define HEALTHCHECK. `docker-compose.yml` also defines healthchecks for redis, mcp-server, web-server with `start_period`, `interval`, `timeout`, `retries`.

7. **Non-root containers.** Both production Dockerfiles create `appuser` (uid 1000) and `USER appuser`.

8. **No secrets in images.** `.env` and `.env*` are in `.dockerignore`. Configuration is supplied at runtime via environment variables.

9. **LLM stub mode for safe demo.** `docker-compose.yml` defaults to `LLM_STUB=1`, `LLM_ENABLED=0`. `docker-compose.live.yml` switches to live AI mode requiring `GEMINI_API_KEY` and `GROQ_API_KEY`.

10. **Environment variables required (values NOT revealed):** GEMINI_API_KEY, GROQ_API_KEY, GEMINI_MODEL, GROQ_MODEL, MCP_BASE, REDIS_HOST, REDIS_PORT, LLM_ENABLED, LLM_STUB, MCP_TIMEOUT, QUARANTINE_PATH, plus Gemini/Groq quota controls.

### Assumptions (UNKNOWN — not verified from repo alone)
- Whether port 8000 mapping on mcp-server is needed in production (can be removed for private networking).
- Exact memory/CPU requirements under load (not benchmarked).
- Whether the quarantine log volume is significant.



## 2. Docker and Service Dependency Findings

### VERIFIED

**Docker images:**
- `Dockerfile.web` — builds Web Gateway on `python:3.11-slim`, installs from `requirements.txt`, copies only `web_server.py`, `agents.py`, `static/`, `templates/`, runs `uvicorn web_server:app --host 0.0.0.0 --port 9000`, EXPOSE 9000, HEALTHCHECK on `/health`.
- `Dockerfile.mcp` — builds MCP Core Engine on `python:3.11-slim`, installs from `requirements.txt`, copies only `mcp_server.py`, runs `uvicorn mcp_server:app --host 0.0.0.0 --port 8000`, EXPOSE 8000, HEALTHCHECK on `/health`.
- Legacy `Dockerfile` (root image, runs both servers on 8000 & 8001) — **not used by compose**; compose uses the two service-specific Dockerfiles.

**docker-compose.yml services:**
| Service | Image/Build | Depends On | Network | Exposed to host |
|---------|------------|------------|---------|-----------------|
| redis | `redis:7-alpine` | — | Compose DNS `redis:6379` | No |
| mcp-server | `Dockerfile.mcp` | — | Compose DNS `mcp-server:8000` | Yes (8000:8000) — removable |
| web-server | `Dockerfile.web` | mcp-server (healthy), redis (healthy) | Compose DNS `web-server:9000` | Yes (9000:9000) |

**Dependency chain (verified from compose):**
```
redis (6379) ──┐
               ├──► mcp-server (8000) ──┐
                                           ├──► web-server (9000) ──┐
                                                                         └──► Frontend (served by web-server)
```
Web Gateway depends on both mcp-server and redis being healthy before starting.

**Network:** Default Compose bridge network. Services communicate via service-name DNS. Redis and MCP are NOT exposed to host by design (only web-server is).

**Persistent storage:**
- Redis: in-memory only in compose (no volume mounted). Cache is volatile.
- Quarantine log: `QUARANTINE_PATH=/app/logs/quarantine.jsonl` — append-only JSONL, minimal metadata, no source code/secrets.
- No database volume; no persistent Redis volume.

**Service separation & privacy:**
- MCP Core Engine can be made **private** (remove host port mapping; accessible only within VPC).
- Web Gateway must be **public** (receives traffic from portfolio visitors).
- Frontend + Web Gateway are **co-located** (single container, single port 9000).

**Do-not-modify constraint:** The task rules prohibit modifying Dockerfiles, docker-compose, source code, dependencies, or Redis config. The architecture must remain compatible with current APIs.


## 3. AWS Architecture Comparison

### VERIFIED architecture constraints
- Two containerized FastAPI services (Web Gateway port 9000, MCP port 8000).
- Web Gateway must be public; MCP must be private.
- Redis used for caching (optional, crash-safe).
- Frontend co-located with Web Gateway.
- Health checks on `/health` for both services.
- Non-root containers, secrets via env vars.

### Option A — AWS ECS Fargate (RECOMMENDED)

**Architecture:**
- ECS cluster with Fargate (serverless containers, no EC2 to manage).
- Two task definitions: `web-gateway` (public, port 9000) and `mcp-core` (private, port 8000).
- Redis as a third Fargate task (private) OR ElastiCache.
- Application Load Balancer (ALB) in front of web-gateway only.
- mcp-core has **no public load balancer**; reachable only via VPC private DNS from web-gateway.
- CloudWatch Logs for container logs.
- ECR for storing container images.
- Secrets in AWS Secrets Manager or Systems Manager Parameter Store.

**Why it fits:**
- No EC2 instances to patch/manage — lowest operational overhead for a beginner.
- Fargate tasks scale to zero (stop when not needed) — important for cost control.
- Private networking via VPC subnets and security groups — MCP never touches public internet.
- ALB provides HTTPS termination (with ACM certificate) and health-check-based routing.
- Container health checks (`/health`) integrate with ECS service health.

**Trade-offs:**
- Fargate has **no free tier** — you pay per vCPU/GB per second while tasks run.
- ALB has a fixed hourly charge (~$23–24/month) even with zero traffic.
- NAT Gateway needed if private subnets need internet access (e.g., to pull images or call Gemini/Groq) — ~$41–43/month just to exist, plus data processing.
- Cost can be reduced by stopping tasks when not demonstrating.

### Option B — AWS EC2

**Architecture:**
- One or more EC2 instances (e.g., t3.micro or t4g.micro).
- Install Docker, run docker-compose with all three services.
- Security groups control port exposure (9000 public, 8000 & 6379 private to the instance/VPC).
- Optional ALB for HTTPS, or direct Elastic IP + self-managed HTTPS.

**Why it might seem attractive:**
- One t3.micro instance ~$8.50/month (on-demand) could host everything.
- Can stop the instance when not in use (stops compute charges; EBS storage still incurs small cost).

**Trade-offs (NOT recommended for this case):**
- You manage OS patching, Docker, container restarts, security — higher operational burden for a beginner.
- To make MCP private while Web Gateway public on a single instance, you need careful firewall/security-group design (doable but more complex than ECS).
- If using one instance for everything, MCP is not truly isolated from the public network at the infrastructure level.
- t3.micro is burstable; sustained AI workloads may exhaust CPU credits.
- No free tier for EC2 beyond 12-month 750 hours/month t2/t3.micro (new accounts only) — need to verify eligibility.

### Option C — AWS App Runner

**Architecture:**
- Deploy container from ECR directly via App Runner.
- App Runner builds and scales automatically.

**Compatibility check (UNKNOWN / likely NOT suitable):**
- App Runner typically runs **one service per App Runner service**. Running two services (web-gateway + mcp-core) with private communication between them is not App Runner's natural model.
- Making MCP truly private (no public endpoint) while web-gateway is public is awkward in App Runner — both App Runner services are publicly addressable by default.
- App Runner does not natively support the private-service-behind-public-gateway pattern as cleanly as ECS + ALB + security groups.

**Verdict:** Not recommended because it doesn't cleanly satisfy "MCP must NOT be exposed directly to the public internet" while preserving service separation.

### Option D — Lower-cost alternatives (outside AWS)

Since you want to preserve AWS credits, consider whether a portfolio project needs AWS at all:
- **Render / Railway / Fly.io:** Cheap container hosting, but MCP privacy pattern harder; not AWS (no IaC demo value).
- **AWS Lightsail:** Simpler than EC2, fixed monthly pricing, containers supported. But Lightsail doesn't cleanly demonstrate ECS/IaC skills, and private networking between two containers is limited.
- **Local demo only (no cloud):** Zero cost, but doesn't demonstrate cloud deployment skills.

**RECOMMENDATION:** AWS ECS Fargate is the simplest technically appropriate architecture. It satisfies all constraints (public web-gateway, private MCP, containerization, health checks, IaC-friendly) with the least operational overhead. Cost is controlled by stopping tasks when not demonstrating.


## 4. Redis Hosting Comparison

**CONSTRAINT:** Do not replace Redis or modify application code. The app expects Redis at `REDIS_HOST`/`REDIS_PORT` via standard Redis protocol.

### Option 1 — Amazon ElastiCache for Redis (cache.t4g.micro)

| Factor | Details |
|--------|---------|
| Monthly cost (ap-south-1) | ~$0.02/hr × 730 = ~$14.60/month |
| Monthly cost (ap-southeast-2) | Slightly higher (~$15–16/month) |
| Reliability | High (managed, optional Multi-AZ) |
| Persistence | Optional (RDB/AOF snapshots) |
| Network | VPC, security groups, private |
| Security | Encryption in transit/at rest available |
| Maintenance | Fully managed by AWS |
| Compatibility | ✅ Standard Redis protocol — compatible |
| Free tier | 12 months, cache.t2/t3.micro, 750 hrs/month (new accounts) — verify eligibility |

**Verdict:** Best compatibility and reliability, but ~$15/month recurring even when idle. Multi-AZ doubles cost — not needed for portfolio.

### Option 2 — Redis as a private Fargate container (reuse `redis:7-alpine`)

| Factor | Details |
|--------|---------|
| Monthly cost | Fargate task cost (~$20–36/month for small task, always on unless stopped) |
| Reliability | Lower (single container, no managed failover) |
| Persistence | None unless volume attached (EFS adds cost/complexity) |
| Network | VPC private, same as app |
| Security | Security groups only |
| Maintenance | You manage container (image, restarts) |
| Compatibility | ✅ Identical to local compose |

**Verdict:** Compatible but more expensive than ElastiCache micro due to Fargate pricing, and less reliable (self-managed). Not recommended unless you want to avoid ElastiCache entirely.

### Option 3 — Redis on EC2

| Factor | Details |
|--------|---------|
| Monthly cost | EC2 instance (~$8.50/month t3.micro) + EBS |
| Reliability | Lower (self-managed, single point of failure) |
| Persistence | You configure (EBS volume) |
| Network | VPC private |
| Security | You manage OS + Redis security |
| Maintenance | High (patch OS, manage Redis) |
| Compatibility | ✅ Standard Redis |

**Verdict:** Cheapest raw compute but highest operational burden and least reliable. Not appropriate for a beginner portfolio project.

### Option 4 — Upstash / Redis Cloud (serverless Redis, outside AWS)

| Factor | Details |
|--------|---------|
| Monthly cost | Free tier available (limited data/requests); paid beyond |
| Reliability | High (managed) |
| Persistence | Depends on plan |
| Network | Public endpoint (unless VPC peering, which adds complexity/cost) |
| Security | TLS, password auth |

## 5. Detailed Cost Estimation

**Pricing date:** 21 November 2026  
**Regions:** ap-south-1 (Mumbai), ap-southeast-2 (Sydney)  
**USD→INR:** ~1 USD = 88 INR (approximate, verify at time of spending)  
**Pricing sources:** AWS public price lists via usage.ai and cloud-credits.org (updated Nov 2026); AWS pricing pages.

### Unit prices used

| Service | ap-south-1 (Mumbai) | ap-southeast-2 (Sydney) |
|---------|---------------------|-------------------------|
| Fargate vCPU-hour (x86) | $0.04256 | $0.04856 |
| Fargate GB-hour (x86) | $0.004655 | $0.00532 |
| Fargate vCPU-hour (Arm) | $0.02383 | $0.03885 |
| Fargate GB-hour (Arm) | $0.00261 | $0.00426 |
| ALB $/hour | $0.0239 | $0.0252 |
| ALB $/LCU-hour | $0.008 | $0.008 |
| ElastiCache cache.t4g.micro /hr | $0.020 | ~$0.021–0.022 |
| NAT Gateway $/hour | $0.056 | $0.059 |
### Assumptions
- Two Fargate tasks: web-gateway (0.25 vCPU / 0.5 GB) and mcp-core (0.25 vCPU / 0.5 GB), small Arm (Graviton) to minimize cost.
- Redis: cache.t4g.micro (ElastiCache) for Plans B/C; skipped or containerized for Plan A.
- ALB: 1 LCU average (low traffic portfolio site).
- NAT Gateway: only needed if private subnets require internet (e.g., calling Gemini/Groq APIs). Can be avoided by placing tasks in public subnets (security via security groups) — discussed in plans.
- ECR: ~1 GB image storage.
- CloudWatch: minimal logs (~1 GB/month ingestion), basic metrics, 2–3 alarms.
- Secrets: 4 secrets (GEMINI_API_KEY, GROQ_API_KEY, MCP_BASE, REDIS_HOST) — use SSM Parameter Store Standard (free) instead of Secrets Manager to save ~$1.60/month.
- Data transfer: minimal (< 1 GB/month) — within free tier.
- Month = 730 hours.
### Cost Table — Plan A: Lowest Possible Cost (stopped when not in use)

| Item | Mumbai (USD) | Sydney (USD) | Mumbai (INR) | Sydney (INR) | Notes |
|------|-------------|--------------|--------------|--------------|-------|
| Fargate compute (0.5 vCPU/1GB total, 730 hrs) | ~$15–20 | ~$18–23 | ~1,320–1,760 | ~1,584–2,024 | Only when running |
| ALB | ~$23.29 | ~$24.24 | ~2,050 | ~2,133 | Fixed monthly if ALB exists |
| NAT Gateway | ~$40.88 | ~$43.07 | ~3,597 | ~3,790 | Fixed monthly if exists — CAN AVOID via public subnets |
| ElastiCache t4g.micro | $0 (skipped) | $0 | $0 | $0 | App degrades gracefully without Redis |
| ECR storage (1 GB) | $0.10 | $0.10 | ~$9 | ~$9 | |
| CloudWatch logs/metrics | ~$2–3 | ~$2–3 | ~176–264 | ~176–264 | |
| Secrets (SSM Parameter Store, free tier) | $0 | $0 | $0 | $0 | Standard parameters free |
| Data transfer (<1 GB) | $0 | $0 | $0 | $0 | Within free tier |
| Public IPv4 | $0 (use ALB) | $0 (use ALB) | $0 | $0 | ALB provides public endpoint |
| **TOTAL (running continuously)** | **~$41–48** | **~$45–52** | **~3,608–4,224** | **~3,960–4,576** | Excludes NAT if avoided |
| **TOTAL (stopped/scaled to zero)** | **~$23–24** | **~$24–25** | **~2,050–2,133** | **~2,133–2,200** | ALB + ECR + logs still charge |
| **TOTAL (ALB also deleted when not in use)** | **~$0–3** | **~$0–3** | **~0–264** | **~0–264** | Only ECR + minimal logs |

### Cost Table — Plan B: Balanced (running continuously during demo period)

| Item | Mumbai (USD) | Sydney (USD) | Mumbai (INR) | Sydney (INR) |
|------|-------------|--------------|--------------|--------------|
| Fargate compute | ~$15–20 | ~$18–23 | ~1,320–1,760 | ~1,584–2,024 |
| ALB | ~$23.29 | ~$24.24 | ~2,050 | ~2,133 |
| NAT Gateway (or avoid via public subnets) | ~$0–40.88 | ~$0–43.07 | $0–3,597 | $0–3,790 |
| ElastiCache t4g.micro | ~$14.60 | ~$15.50 | ~1,285 | ~1,364 |
| ECR storage | $0.10 | $0.10 | ~$9 | ~$9 |
| CloudWatch logs/metrics | ~$2–3 | ~$2–3 | ~176–264 | ~176–264 |
| Secrets (SSM free) | $0 | $0 | $0 | $0 |
| Data transfer | $0 | $0 | $0 | $0 |
| **TOTAL (with NAT)** | **~$96–102** | **~$100–108** | **~8,448–8,976** | **~8,800–9,504** |
| **TOTAL (no NAT — public subnets)** | **~$55–61** | **~$58–65** | **~4,840–5,368** | **~5,104–5,720** |

### Cost Table — Plan C: Production-Oriented

| Item | Mumbai (USD) | Sydney (USD) | Mumbai (INR) | Sydney (INR) |
|------|-------------|--------------|--------------|--------------|
| Fargate compute (2 tasks, larger, always on) | ~$35–45 | ~$40–50 | ~3,080–3,960 | ~3,520–4,400 |
| ALB (2 LCU) | ~$30–35 | ~$31–36 | ~2,640–3,080 | ~2,728–3,168 |
| NAT Gateway (2 AZs for HA) | ~$82–120 | ~$86–126 | ~7,216–10,560 | ~7,568–11,088 |
| ElastiCache t4g.micro Multi-AZ (2 nodes) | ~$29 | ~$31 | ~2,552 | ~2,728 |
| ECR storage | $0.10 | $0.10 | ~$9 | ~$9 |
| CloudWatch (enhanced metrics, more logs) | ~$5–8 | ~$5–8 | ~440–704 | ~440–704 |
| Secrets Manager (paid) | ~$1.60 | ~$1.60 | ~141 | ~141 |
| Data transfer | $0–10 | $0–10 | $0–880 | $0–880 |
| **TOTAL (running continuously)** | **~$184–248** | **~$199–266** | **~16,192–21,824** | **~17,512–23,408** |

### Key findings (VERIFIED from pricing sources)

1. **NAT Gateway is expensive just to exist:** ~$41–43/month per NAT Gateway even with zero data processing. **Avoidable** by placing Fargate tasks in public subnets and controlling access via security groups (not private subnets + NAT). This is a critical cost-saving insight.

2. **ALB charges even with zero traffic:** ~$23–24/month fixed. You pay for the ALB existing, not just for traffic.

## 6. AWS Credit Eligibility and Cost Risks

### AWS Promotional Credits — what you know
- $100 promotional credits, 182 days remaining, expiry **May 21, 2027**.
- Current month usage: $0.

### ESTIMATED / RECOMMENDATION — credit eligibility

| Charge type | Typically covered by AWS promotional credits? | Notes |
|-------------|:--------------------------------------------:|-------|
| EC2 / Fargate compute | Usually ✅ | Most compute credits cover EC2, Fargate, Lambda |
| ALB / Load Balancer | Usually ✅ | Often included |
| ElastiCache | Usually ✅ | Often included |
| ECR storage | Usually ✅ | Often included |
| CloudWatch | Usually ✅ | Often included |
| NAT Gateway | Usually ✅ | Often included |
| Data transfer out | Usually ✅ | Often included |
| Route 53 / domain | ⚠️ Varies | Sometimes excluded |
| ACM certificates | Usually ✅ | Public certs are free anyway |
| Support plans | ❌ Usually excluded | Developer/Business support often not covered |
| Third-party marketplace | ❌ Usually excluded | |
| Reserved Instances / Savings Plans | ⚠️ Varies | Often excluded |

**Do not assume credits cover everything.** Some promotional credit programs exclude:
- Support plans
- Certain marketplace products
- Reserved Instances / Savings Plans
- Some specialized services

**VERIFY:** Check the exact terms of your credit offer in the AWS Billing Console → Credits. Do not rely on assumptions.

### Key facts (RECOMMENDATION)

1. **Credits expire May 21, 2027.** Unused credits after that date are lost.
2. **After credits are exhausted or expire, you pay full price.** Your normal payment method is charged.
3. **Charges can occur even with $0 current usage** if you create resources that have fixed hourly/monthly fees (ALB, NAT Gateway, ElastiCache, ECR storage).
4. **A budget alert is NOT a hard spending limit.** AWS Budgets sends notifications when you approach/exceed a threshold, but it does NOT automatically stop resources (unless you separately configure an action, which is advanced). You can still overspend.
5. **Safeguards available (instructions only — do not create):**
   - AWS Budgets: create a monthly budget (e.g., $5 or $10) with email alerts at 50%, 80%, 100%.
   - Billing alerts via CloudWatch: alarm on estimated charges.
   - AWS Free Tier usage alerts.
   - Cost Explorer: review actual spend daily during demo.
   - IAM billing permissions: restrict who can create resources.
   - Service Quotas: keep defaults low.
   - Tag resources for cost allocation.

**Do not create billing resources or change billing settings yet.** These are instructions for you to review and implement when you're ready to deploy.



## 7. Three Deployment Plans

### PLAN A — Lowest Possible Cost

**Architecture:**
- ECS Fargate, two tasks (web-gateway public, mcp-core private).
- No ALB: expose web-gateway directly via Fargate public IP / Elastic IP (or use ALB only during live demo windows).
- No ElastiCache: Redis skipped (app degrades gracefully — VERIFIED crash-safe).
- No NAT Gateway: tasks in public subnets, security groups restrict access.
- SSM Parameter Store (free tier) for secrets.
- CloudWatch logs minimal.
- **Stop ECS tasks when not demonstrating.** Scale to 0 desired count.
- **Delete ALB when not in use** to eliminate the ~$23/month fixed charge.

| | Mumbai | Sydney |
|---|--------|--------|
| Running continuously | ~$41–48/mo | ~$45–52/mo |
| Tasks stopped (ALB kept) | ~$23–24/mo | ~$24–25/mo |
| Everything stopped/deleted | ~$0–3/mo | ~$0–3/mo |

**Operational limitations:**
- No managed Redis (cache unavailable; app still works).
- No ALB means less robust HTTPS/routing (can use ACM + direct endpoint or add ALB only during demos).
- Manual start/stop of tasks for demo windows.
- Lower reliability (single task per service, no Multi-AZ).

**Security considerations:**
- Security groups restrict mcp-core to web-gateway only.
- Web-gateway public — ensure input validation (VERIFIED: Pydantic v2 validation present).
- Secrets in SSM (not .env).

**Appropriate for portfolio?** Yes — demonstrates ECS, VPC, security groups, containerization, cost-aware design. Lowest risk of burning credits.

### PLAN B — Balanced (RECOMMENDED)

**Architecture:**
- ECS Fargate, two tasks (web-gateway public, mcp-core private).
- ALB in front of web-gateway (HTTPS via ACM, fixed ~$23–24/mo).
- No NAT Gateway (public subnets + security groups — saves ~$41–43/mo).
- ElastiCache cache.t4g.micro single node (~$15/mo) — OR skip Redis for further savings.
- SSM Parameter Store (free) for secrets.
- CloudWatch logs + basic alarms.
- Tasks run only during demo windows; stop when not needed.

| | Mumbai | Sydney |
|---|--------|--------|
| Running continuously (no NAT) | ~$55–61/mo | ~$58–65/mo |
| Tasks stopped (ALB + ElastiCache kept) | ~$40–42/mo | ~$41–43/mo |

**Operational limitations:**
- Still need to stop tasks to save compute cost when idle.
- ElastiCache is a fixed cost even when app unused.

**Security considerations:**
- ALB provides HTTPS termination and WAF-readiness.
- mcp-core private (no public endpoint).
- Security groups: ALB → web-gateway (9000), web-gateway → mcp-core (8000), web-gateway → redis (6379).
- Secrets in SSM.

## 8. Terraform and Infrastructure as Code

**Is Terraform appropriate?** Yes — it's practical and valuable for a portfolio. It demonstrates IaC skills, reproducibility, and clean resource management. However, it adds complexity and a learning curve.

**DO NOT create Terraform files yet.** Per instructions, wait for your approval.

### Proposed structure (for after approval)

```
infra/
  terraform/
    providers.tf          # AWS provider, region config
    variables.tf          # Input variables (region, instance sizes, env names)
    networking.tf         # VPC, subnets (public/private), internet gateway, route tables
    security_groups.tf    # SG for ALB, web-gateway, mcp-core, redis
    ecr.tf                # ECR repositories for web-gateway and mcp-core images
    ecs.tf                # ECS cluster, task definitions, services, capacity providers
    alb.tf                # ALB, target group, listener, ACM cert reference
    redis.tf              # ElastiCache cache cluster (or skip for Plan A)
    outputs.tf            # ALB DNS name, ECR repo URLs, etc.
    terraform.tfvars.example  # Example values (no real secrets)
```

**What each file manages:**
- `providers.tf`: AWS provider, region (ap-south-1 recommended).
- `variables.tf`: region, container CPU/memory, image tags, secret names.
- `networking.tf`: VPC, public/private subnets, IGW, route tables (public subnets to avoid NAT cost in Plan A/B).
- `security_groups.tf`: ALB→web-gateway (9000), web-gateway→mcp-core (8000), web-gateway→redis (6379), mcp-core has NO inbound from internet.
- `ecr.tf`: Private repos for the two container images.
- `ecs.tf`: Cluster, Fargate task definitions, services with desired count (start at 0 or 1), health checks.
- `alb.tf`: ALB, target group on port 9000, HTTPS listener (ACM cert), health check path `/health`.
- `redis.tf`: ElastiCache cache.t4g.micro single node (Plan B/C) or omitted (Plan A).
- `outputs.tf`: ALB DNS, ECR URIs, service endpoints.
- `terraform.tfvars.example`: Example variable values (no secrets).

**Important:** Even after approval to create the scaffold:
- Do NOT run `terraform apply`.
- Do NOT create AWS resources.
- Do NOT connect AWS credentials.
- Do NOT deploy anything.


**Appropriate for portfolio?** Yes — best balance of credibility, security, and cost. Demonstrates ALB, private networking, managed Redis, ECS.

### PLAN C — Production-Oriented

**Architecture:**
- ECS Fargate, Multi-AZ (2+ tasks per service for HA).
- ALB with 2+ LCU.
- NAT Gateway in each AZ for HA (~$82–126/mo just for NATs).

## 9. Step-by-Step Deployment Roadmap

| Step | Task | Can incur charges? | Notes |
|------|------|:------------------:|-------|
| 1 | AWS account & region preparation (choose ap-south-1) | No | Review credit terms in Billing Console |
| 2 | Billing safeguards (Budgets, alerts) | No | Set $5–10 budget, email alerts; not a hard limit |
| 3 | IAM & least-privilege access | No | Create IAM user/role with limited permissions; no access keys for Cline |
| 4 | Container image preparation (Docker build locally) | No | Build web-gateway + mcp-core images locally; test |
| 5 | ECR (create repos, push images) | Yes (storage ~$0.10/GB/mo) | Small storage cost |
| 6 | Networking (VPC, subnets, security groups) | Yes (NAT only if used; avoid NAT via public subnets) | VPC itself is free; NAT Gateway is not |
| 7 | Compute deployment (ECS Fargate services) | Yes (Fargate per-second while running) | Start with desired count 0; scale to 1 only for demo |
| 8 | Private MCP communication (security groups, no public LB) | No | mcp-core has no public endpoint |
| 9 | Redis (ElastiCache t4g.micro or skip) | Yes (~$15/mo if ElastiCache; $0 if skipped) | Plan B uses ElastiCache; Plan A skips |
| 10 | Secrets configuration (SSM Parameter Store) | No (Standard tier free) | Store GEMINI_API_KEY, GROQ_API_KEY etc. |
| 11 | HTTPS (ACM certificate + ALB listener) | No (ACM public certs are free; ALB hourly ~$23–24) | ALB is the cost, not the cert |
| 12 | Health checks (ECS service health, /health endpoints) | No | Uses existing app health endpoints |
| 13 | Logging (CloudWatch Logs) | Yes (~$1–3/mo minimal) | Keep retention short |
| 14 | Live validation | Yes (while tasks running) | Test /analyze endpoint, verify MCP private |
| 15 | Cleanup & cost-control | No (stopping saves compute; deleting ALB/NATs saves fixed costs) | Stop tasks, delete ALB/NAT when not demoing |

### Free preparation steps (No charges):
Steps 1, 2, 3, 4, 10 (SSM free tier), 12, 15 (stopping/deleting).

### Steps that can incur charges:
Steps 5 (ECR storage), 6 (NAT if used), 7 (Fargate compute), 9 (ElastiCache), 11 (ALB hourly), 13 (CloudWatch), 14 (Fargate while running).

### How to stop/remove safely:
- Set ECS service desired count to 0 (stops Fargate tasks; saves compute).
- Delete ALB (saves ~$23–24/mo fixed).
- Delete NAT Gateway (saves ~$41–43/mo fixed) — only if you created one.
- Delete ElastiCache (saves ~$15/mo) — only if you created one.
- ECR storage (~$0.10/GB/mo) and CloudWatch log storage continue until images/logs are deleted.
- **Resources that continue charging even when app stopped:** ALB, NAT Gateway, ElastiCache, ECR storage, CloudWatch log storage, Elastic IP/Public IPv4.

- ElastiCache Multi-AZ (2 nodes, ~$29–31/mo).
- Secrets Manager (paid, ~$1.60/mo).
- Enhanced CloudWatch metrics, dashboards, alarms.
- Auto Scaling based on CPU/memory.
- Running continuously.


## 10. Portfolio and Resume Value

### What this genuinely demonstrates

| Skill | Evidence in project |
|-------|-------------------|
| AWS fundamentals | ECS Fargate, VPC, security groups, ALB, ECR, CloudWatch, SSM |
| Containerization | Docker, multi-service Dockerfiles, ECR, Fargate |
| Cloud deployment | ECS service deployment, ALB, health checks, private networking |
| Networking | Public web-gateway, private MCP, security group rules, no NAT (cost-aware) |
| IAM & security | SSM Parameter Store for secrets, non-root containers, no secrets in images, least-privilege |
| Infrastructure as Code | Terraform (planned), reproducible infrastructure |
| Monitoring | CloudWatch logs, metrics, alarms, health checks |
| Backend architecture | Two-service FastAPI separation, API contracts, async pipeline |
| AI integration | Gemini primary / Groq fallback, LangGraph multi-agent, Redis caching |
| Reliability engineering | Retries, circuit breakers, fallback handling, safe failures, crash-safe Redis |

### Suggested resume wording

> **CodeNexus AI — Multi-Agent Code Review System** | AWS Portfolio Project
> - Deployed a two-service FastAPI application (public Web Gateway + private MCP Core Engine) on AWS ECS Fargate with Application Load Balancer, VPC private networking, and ElastiCache Redis.
> - Implemented infrastructure-as-code (Terraform) for reproducible cloud infrastructure; managed secrets via AWS Systems Manager Parameter Store.
> - Designed cost-aware architecture: resources scaled to zero when not demonstrating, NAT Gateway avoided, preserving ~$100 AWS promotional credits.
> - Integrated multi-agent LangGraph pipeline with Gemini (primary) / Groq (fallback) LLMs, Redis response caching, and Python AST static analysis.
> - Implemented reliability patterns: retries, circuit breakers, fallback handling, and crash-safe caching.

### What NOT to claim (do not exaggerate)
- ❌ "Production-scale usage" — it's a portfolio demo.
- ❌ "Enterprise adoption" — no real users.
- ❌ "Validated ML accuracy" — the RandomForest is trained on 6 synthetic rows (VERIFIED limitation in repo).
- ❌ "High availability" — Plan A/B are single-task; only Plan C is Multi-AZ.
- ❌ Specific uptime/throughput figures — not measured.

## 11. Risks, Unknowns and Decisions Requiring My Approval

### RISKS
1. **NAT Gateway cost surprise:** If you place tasks in private subnets, a NAT Gateway (~$41–43/mo fixed) is required for outbound internet (calling Gemini/Groq). **Mitigation:** Use public subnets + security groups in Plan A/B.
2. **ALB fixed charge:** ~$23–24/mo even with zero traffic. **Mitigation:** Delete ALB when not demoing (Plan A), or accept as fixed cost (Plan B).
3. **Credits may not cover all services:** Support plans, some marketplace items, Reserved Instances may be excluded. **Verify** credit terms in Billing Console.
4. **Budget alerts are not hard limits:** You can overspend past a budget alert. **Mitigation:** Check Cost Explorer regularly; set low budget ($5–10).
5. **After credits expire (May 21, 2027) or exhaust:** You pay full price on your payment method.

### UNKNOWN — requires your verification
1. Exact terms of your $100 promotional credit (which services are covered/excluded).
2. Whether your new AWS account qualifies for 12-month free tier (e.g., t3.micro 750 hrs/month) — could further reduce cost if using EC2 instead of Fargate.
3. Current USD→INR exchange rate at time of spending.
4. Whether ElastiCache free tier (cache.t2/t3.micro, 750 hrs/mo for 12 months) applies to your account — could make Redis free.
5. Actual memory/CPU requirements under load (not benchmarked) — Fargate task sizing may need adjustment.

### DECISIONS REQUIRING YOUR APPROVAL
1. **Choose region:** ap-south-1 (Mumbai) — recommended (cheaper for Fargate/ALB, closer to you).
2. **Choose plan:** Plan A (lowest cost), Plan B (balanced, recommended), or Plan C (production).
3. **Redis:** Use ElastiCache (~$15/mo) or skip Redis (app degrades gracefully, saves $15/mo).
4. **NAT Gateway:** Avoid via public subnets (recommended for cost) vs. private subnets + NAT (more "best practice" but ~$41–43/mo).
5. **ALB:** Keep permanently (~$23–24/mo fixed) vs. create/delete per demo session.
6. **Terraform scaffold:** Approve creation of infra/terraform files (files only, no apply, no credentials, no deployment).

---

## REPORT COMPLETE

**No actions taken:** No AWS resources created, no credentials connected, no Terraform files created, no repository modifications, no commits/pushes, no charges incurred.

**Next step:** Please review this report and indicate:
1. Which region you prefer (Mumbai ap-south-1 recommended).
2. Which deployment plan (A/B/C) you want to proceed with.
3. Whether you approve creating the Terraform scaffold (files only, no apply).
4. Whether you want me to verify credit eligibility details or anything else before proceeding.

| | Mumbai | Sydney |
|---|--------|--------|


**Appropriate for portfolio?** Overkill. Only if you want to demonstrate production-grade HA. Not recommended given credit-preservation goal.

**RECOMMENDATION:** **PLAN B** — Balanced. It demonstrates real cloud skills (ECS Fargate, ALB, VPC private networking, ElastiCache, SSM secrets) while keeping cost ~$55–61/month running, and ~$0–3/month if you tear down the ALB and stop tasks when not demoing. Within your $100 credit budget for several months of demo use.

3. **Fargate has no free tier.** You pay for every second tasks run. Stop tasks when not demonstrating.

4. **Services that charge even with zero traffic:** ALB, NAT Gateway, Elastic IP / Public IPv4, ECR storage, CloudWatch log storage, ElastiCache (node-hours), Secrets Manager.

5. **"Seemingly free" architectures can still incur charges:** ECR storage, CloudWatch logs, and any resource that exists but is idle all bill hourly/monthly regardless of traffic.

6. **Mumbai vs Sydney:** Mumbai is slightly cheaper for Fargate (Arm: $0.02383 vs $0.03885 vCPU-hr) and ALB ($0.0239 vs $0.0252/hr). Mumbai is the better choice geographically and cost-wise. **VERIFIED:** Mumbai is NOT automatically cheaper on every service, but for Fargate and ALB it is modestly cheaper.


| NAT Gateway $/GB processed | $0.056 | $0.059 |
| ECR storage $/GB/month | $0.10 | $0.10 |
| Public IPv4 | ~$0.0043/hr (~$3.14/month) if not using ALB |
| CloudWatch Logs ingestion | ~$0.50/GB (varies by region) |
| CloudWatch Logs storage | ~$0.03/GB/month |
| CloudWatch custom metrics | ~$0.30/metric/month |
| CloudWatch alarms | ~$0.10/alarm/month |
| Secrets Manager | ~$0.40/secret/month + API calls |
| SSM Parameter Store (Standard) | Free (up to limits) |
| Data transfer out to internet | First 100 GB/month free, then ~$0.09/GB |

| Maintenance | Fully managed |
| Compatibility | ✅ Redis protocol |

**Verdict:** Could be cheapest (free tier), but introduces a non-AWS dependency and public endpoint — less aligned with "keep everything on AWS" and "MCP private" goals. Also adds external service to secure.

**RECOMMENDATION:** For Plan B/C, use **ElastiCache cache.t4g.micro** (single node, no Multi-AZ) — ~$15/month, managed, compatible, private. For Plan A (lowest cost), skip managed Redis entirely: run Redis as a lightweight container alongside the app on the same Fargate task or EC2, OR accept that Redis cache will be unavailable when tasks are stopped (app degrades gracefully — VERIFIED crash-safe in `agents.py`).

### Comparison Summary

| Criterion | ECS Fargate | EC2 | App Runner | Lightsail |
|-----------|-------------|-----|------------|-----------|
| MCP private to internet | ✅ Yes (security groups, no public LB) | ⚠️ Manual firewall | ❌ Hard (public by default) | ⚠️ Limited |
| Web gateway public | ✅ via ALB | ✅ via security group | ✅ | ✅ |
| Managed (no OS patching) | ✅ Yes | ❌ No | ✅ Yes | ✅ |
| Scale to zero / stop | ✅ Tasks stoppable | ✅ Instance stoppable | ✅ Auto-scales (min cost) | ✅ Instance stoppable |
| No free tier | ❌ No free tier | ⚠️ 12mo free t3.micro (new acct) | ⚠️ Limited free tier | ❌ No free tier |
| IaC / Terraform friendly | ✅ Excellent | ✅ Good | ✅ Good | ⚠️ Limited |
| Beginner-friendly | ✅ Moderate | ❌ Higher ops burden | ✅ Simple | ✅ Simple |
| Demonstrates cloud skills | ✅ Strong (ECS, VPC, ALB) | ⚠️ Moderate | ✅ Moderate | ⚠️ Weaker |
