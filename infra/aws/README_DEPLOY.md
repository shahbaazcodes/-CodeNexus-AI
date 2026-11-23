# CodeNexus AI — AWS Fargate Deployment (Offline Artifacts Only)

This directory contains **reproducible, offline-prepared AWS deployment infrastructure**
for CodeNexus AI. No AWS resources have been created, and nothing is deployed.

**Target region:** `ap-south-1` (Mumbai)
**Architecture:** Single ECS Fargate task with two co-located containers
- `web-gateway` — FastAPI on port 9000 (public, receives user traffic)
- `mcp-core` — FastAPI on port 8000 (localhost-only, private; web reaches it via `127.0.0.1:8000`)

Only port 9000 is exposed externally. MCP is **not** reachable from outside the task.

## Files

| File | Purpose |
|---|---|
| `codenexus-fargate.yaml` | AWS CloudFormation template (VPC, subnets, security groups, ECR repos, IAM role, ECS cluster, task definition, service, CloudWatch log group) |
| `validate_cfn.py` | Offline structural validator (parses template with CFN tags stripped; runs ECS task-definition, port, ARM64, secret-safety checks) |
| `README_DEPLOY.md` | This file — prerequisites, deployment steps, cleanup, cost guidance |

## Architecture rationale (why one task, not two)

**Option A (two separate Fargate tasks + ECS Service Discovery)** was evaluated and rejected for this temporary demo because:
- Requires an ECS Service Connect / Cloud Map namespace and two separate services — higher complexity and more resources.
- Each task incurs its own Fargate vCPU/memory charge (2× compute minimum).
- MCP would need a VPC endpoint or security-group peering; more networking to manage.

**Option B (single task, two containers, localhost)** was selected because:
- **Lowest cost:** one Fargate task instead of two (halves compute minimum).
- **Simplest networking:** MCP reachable only via loopback (`127.0.0.1:8000`) inside the task; no public MCP endpoint, no service discovery, no NAT Gateway.
- **Compatible:** the app already supports `MCP_BASE` override; set to `http://127.0.0.1:8000` in the task definition.
- **Temporary-demo friendly:** scale `DesiredCount` to `0` when not demoing to stop all compute charges.

The existing application and Docker configurations are **unchanged**. `Dockerfile.web` and `Dockerfile.mcp` are used as-is to build the images.

## Stub mode vs Live AI mode

The deployment template defaults to **stub mode** for safety:
- `LLM_STUB=1` (default): Deterministic stub responses, **zero network calls** to any LLM provider.
- `LLM_ENABLED=0` (default): Live provider calls disabled.

**To enable live AI mode** (requires valid provider keys):
1. Create SSM SecureString parameters for Gemini and Groq API keys as described in Prerequisites.
2. Deploy with parameter overrides:
   ```bash
   aws cloudformation deploy \
     --template-file infra/aws/codenexus-fargate.yaml \
     --stack-name codenexus-demo \
     --parameter-overrides \
       LLMEnabled=1 \
       LLMStub=0 \
       DesiredCount=1 \
       ...
   ```
3. The task will then make live calls to Gemini (primary) and Groq (fallback).

**Important:** Never set `LLMEnabled=1` without valid `GEMINI_API_KEY` and `GROQ_API_KEY` in SSM — the task will fail health checks or return provider errors. Always verify keys exist before switching.

**Usage cost limit is an estimate, not a guaranteed billing cap.** The `AI_MAX_ESTIMATED_COST_PER_SESSION` guard uses static per-million-token pricing and **does not** control actual provider billing. Actual charges are determined by Gemini/Groq and may differ.

## Prerequisites (complete before deployment)

1. **AWS CLI** configured with a profile/credentials for the target account (not part of this repo).
2. **Docker** installed and logged in to Amazon ECR (`aws ecr get-login-password --region ap-south-1 | docker login ...`).
3. **SSM SecureString parameters** created in `ap-south-1` holding the API keys (values never committed to Git):
   - `/codenexus/demo/gemini-api-key` (Gemini API key)
   - `/codenexus/demo/groq-api-key` (Groq API key)
   Create via: `aws ssm put-parameter --name /codenexus/demo/gemini-api-key --type SecureString --value "..." --region ap-south-1`
4. The ECS task execution role in the template references these parameter names by SSM parameter name (via `AWS::SSM::Parameter::Name` parameters), not by value.

## Build and push images (offline preparation; do this before `aws cloudformation deploy`)

From the repository root:

```bash
# Build ARM64 images locally (multi-arch base image supports Graviton)
docker buildx build --platform linux/arm64 -t codenexus-web-gateway:latest -f Dockerfile.web .
docker buildx build --platform linux/arm64 -t codenexus-mcp-core:latest -f Dockerfile.mcp .

# Tag for ECR (replace ACCOUNT_ID and REGION as appropriate)
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com

aws ecr create-repository --repository-name codenexus-demo/web-gateway --region ap-south-1
aws ecr create-repository --repository-name codenexus-demo/mcp-core --region ap-south-1

docker tag codenexus-web-gateway:latest ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/codenexus-demo/web-gateway:latest
docker tag codenexus-mcp-core:latest ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/codenexus-demo/mcp-core:latest

docker push ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/codenexus-demo/web-gateway:latest
docker push ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/codenexus-demo/mcp-core:latest
```

> The CloudFormation template creates the ECR repositories automatically if they do not exist, but pre-creating and pushing images is required before the service can run.

## Deploy

```bash
aws cloudformation deploy \
  --template-file infra/aws/codenexus-fargate.yaml \
  --stack-name codenexus-demo \
  --parameter-overrides \
    EnvironmentName=codenexus-demo \
    ContainerImageTag=latest \
    DesiredCount=1 \
    TaskCpu=512 \
    TaskMemory=1024 \
    GeminiApiKeyParameterName=/codenexus/demo/gemini-api-key \
    GroqApiKeyParameterName=/codenexus/demo/groq-api-key \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ap-south-1
```

- `CAPABILITY_NAMED_IAM` is required because the template creates an IAM role.
- `DesiredCount=0` stops all tasks (stops compute charges) without destroying the stack.

## Verify after deployment (read-only)

```bash
aws ecs describe-services --cluster codenexus-demo-cluster --services codenexus-demo-service --region ap-south-1
aws ecs list-tasks --cluster codenexus-demo-cluster --region ap-south-1
aws logs describe-log-groups --log-group-name-prefix "/ecs/codenexus-demo/codenexus" --region ap-south-1
```

The public Web Gateway URL is the task's public IP on port 9000 (find via the EC2/ENI or ECS task details). MCP is not publicly accessible.

## Stop / cleanup

- **Temporarily stop (recommended when not demoing):**
  ```bash
  aws cloudformation deploy --template-file infra/aws/codenexus-fargate.yaml --stack-name codenexus-demo --parameter-overrides DesiredCount=0 ... --region ap-south-1
  ```
  Compute charges stop; only ECR storage + CloudWatch log retention remain (~$0.60–1.20/month).

- **Destroy everything:**
  ```bash
  aws cloudformation delete-stack --stack-name codenexus-demo --region ap-south-1
  ```
  Then delete the SSM parameters and ECR images manually if no longer needed.

## Cost guidance (estimates, ap-south-1, verify in AWS Pricing Calculator)

| Component | Running (1 task, 0.5 vCPU/1 GB) | Stopped (DesiredCount=0) |
|---|---|---|
| Fargate compute | ~$16–19/month | $0 |
| ECR storage (~1 GB) | ~$0.10/month | ~$0.10/month |
| CloudWatch logs (7-day retention, minimal) | ~$0.50–1/month | ~$0.50–1/month |
| SSM Parameter Store (Standard, free tier) | $0 | $0 |
| **Total (est.)** | **~$17–20/month** | **~$0.60–1.20/month** |

**Important:** AWS resources can incur charges even when `DesiredCount=0` and no Fargate task is running — specifically ECR image storage and CloudWatch log retention/ingestion continue billing. To stop **all** recurring charges, delete ECR images and the CloudWatch log group, or destroy the stack entirely.

**Key cost controls:**
- **`DesiredCount=0` is the deployment default.** Compute charges only start when you explicitly set `DesiredCount=1` for demonstration. Set back to `0` when not demoing.
- No NAT Gateway, no ALB, no ElastiCache (Redis optional; app degrades gracefully).
- No paid domain or HTTPS for this temporary demo.
- Usage guard (`AI_MAX_REQUESTS_PER_SESSION`, `AI_MAX_ESTIMATED_COST_PER_SESSION`, `AI_MAX_TOKENS_PER_REQUEST`) is **per-task, in-memory, best-effort estimate** — not a guaranteed provider billing cap.



## Security & networking design

- **Public:** Web Gateway on port 9000 via public IP (no ALB). Security group allows inbound 9000 from `0.0.0.0/0`, outbound HTTPS (443) to Gemini/Groq APIs, ECR, CloudWatch, SSM.
- **Private:** MCP on port 8000 reachable only via loopback (`127.0.0.1/32`) from the co-located web container. No public MCP endpoint, no VPC endpoint required.
- **Outbound HTTPS:** Required for Gemini/Groq APIs, ECR image pull, CloudWatch logs, SSM secret retrieval. Provided by the public subnet's internet gateway; **no NAT Gateway needed**.
- **Secrets:** Gemini/Groq API keys stored in SSM SecureString parameters and injected at runtime via `ValueFrom`. Never committed to Git, never baked into images (`.dockerignore` excludes `.env*`).
- **IAM:** Task execution role has `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch logs) plus a scoped `ssm:GetParameters` policy limited to the two secret parameter ARNs.

## Offline validation

Run before any deployment:

```bash
python3 infra/aws/validate_cfn.py
```

Checks performed:
- CloudFormation YAML parses cleanly (CFN intrinsic tags pre-processed)
- All required resources present with correct types
- Task definition: FARGATE, awsvpc, ARM64, 2 containers
- web-gateway exposes port 9000; MCP exposes port 8000
- web-gateway `MCP_BASE` points to `http://127.0.0.1:8000` (co-located)
- MCP container has no secrets
- Service launch type FARGATE, public IP enabled
- MCP security group does not open 8000 to `0.0.0.0/0`
- No hardcoded API keys / placeholder credential values

## Existing test suite

Run to confirm application health is unchanged:

```bash
python -m pytest tests/
```

(Previously: 97 passed, 0 failed.)

