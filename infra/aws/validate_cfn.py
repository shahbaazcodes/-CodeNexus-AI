"""Offline validation of the CloudFormation template (no AWS calls).

CloudFormation YAML uses intrinsic tags (!Ref, !Sub, !Equals, !Select,
!GetAZs) that are not valid YAML for a stock parser. This validator
pre-processes those tags into placeholder strings so the structure can be
parsed with PyYAML, then performs structural checks.
"""
import re
import sys
import yaml

TEMPLATE = "infra/aws/codenexus-fargate.yaml"


def strip_cfn_tags(text: str) -> str:
    """Replace CloudFormation intrinsic-function tags with placeholder text."""
    # !Equals [...] may span multiple lines (process BEFORE !Ref so the
    # brackets are not swallowed by the greedy !Ref substitution)
    text = re.sub(r"!Equals\s*\[.*?\]", "__CFN_EQUALS__", text, flags=re.DOTALL)
    text = re.sub(r"!Sub\b[^\n]*", "__CFN_SUB__", text)
    text = re.sub(r"!Ref\b[^\n]*", "__CFN_REF__", text)
    text = re.sub(r"!GetAZs\b[^\n]*", "__CFN_GETAZS__", text)
    text = re.sub(r"!Select\b[^\n]*", "__CFN_SELECT__", text)
    return text


def main():
    raw = open(TEMPLATE).read()
    cleaned = strip_cfn_tags(raw)

    try:
        data = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        print("YAML parse FAILED:", exc)
        sys.exit(1)

    print("YAML parse: OK (CFN tags pre-processed)")
    errors = []

    # Top-level structure
    for key in ("AWSTemplateFormatVersion", "Parameters", "Resources", "Outputs"):
        if key not in data:
            errors.append(f"Missing top-level key: {key}")
    print("YAML parse: OK")
    print("Top-level keys:", sorted(data.keys()))

    params = data.get("Parameters", {})
    print("Parameters:", list(params.keys()))
    required_params = [
        "EnvironmentName", "ContainerImageTag", "DesiredCount",
        "TaskCpu", "TaskMemory", "GeminiApiKeyParameterName",
        "GroqApiKeyParameterName",
    ]
    for p in required_params:
        if p not in params:
            errors.append(f"Missing parameter: {p}")

    resources = data.get("Resources", {})
    print("Resources count:", len(resources))
    print("Resource types:", sorted({r.get("Type") for r in resources.values()}))

    # Required resources
    required_resources = {
        "VPC": "AWS::EC2::VPC",
        "PublicSubnet": "AWS::EC2::Subnet",
        "WebSecurityGroup": "AWS::EC2::SecurityGroup",
        "MCPSecurityGroup": "AWS::EC2::SecurityGroup",
        "ECRRepositoryWeb": "AWS::ECR::Repository",
        "ECRRepositoryMCP": "AWS::ECR::Repository",
        "LogGroup": "AWS::Logs::LogGroup",
        "TaskExecutionRole": "AWS::IAM::Role",
        "Cluster": "AWS::ECS::Cluster",
        "TaskDefinition": "AWS::ECS::TaskDefinition",
        "Service": "AWS::ECS::Service",
    }
    for name, rtype in required_resources.items():
        if name not in resources:
            errors.append(f"Missing resource: {name}")
        elif resources[name].get("Type") != rtype:
            errors.append(f"Resource {name} has type {resources[name].get('Type')}, expected {rtype}")

    outputs = data.get("Outputs", {})
    print("Outputs:", list(outputs.keys()))

    # Task definition checks
    td = resources["TaskDefinition"]["Properties"]
    print("CPU arch:", td.get("RuntimePlatform", {}).get("CpuArchitecture"))
    print("Compat:", td.get("RequiresCompatibilities"))
    print("Network mode:", td.get("NetworkMode"))

    cds = td.get("ContainerDefinitions", [])
    print("Containers:", [c["Name"] for c in cds])
    if len(cds) != 2:
        errors.append(f"Expected 2 containers, found {len(cds)}")

    web = next((c for c in cds if c["Name"] == "web-gateway"), None)
    mcp = next((c for c in cds if c["Name"] == "mcp-core"), None)

    if web:
        print("  web-gateway essential=", web.get("Essential"))
        print("  web-gateway ports=", [pm.get("ContainerPort") for pm in web.get("PortMappings", [])])
        print("  web-gateway secrets=", [s["Name"] for s in web.get("Secrets", [])])
        print("  web-gateway env keys=", [e["Name"] for e in web.get("Environment", [])])
        # Port 9000 exposed
        ports = [pm.get("ContainerPort") for pm in web.get("PortMappings", [])]
        if 9000 not in ports:
            errors.append("web-gateway must expose container port 9000")
        # MCP_BASE must be localhost for co-located arch
        env = {e["Name"]: e.get("Value") for e in web.get("Environment", [])}
        if env.get("MCP_BASE") != "http://127.0.0.1:8000":
            errors.append("web-gateway MCP_BASE must point to localhost MCP (co-located arch)")

    if mcp:
        print("  mcp-core essential=", mcp.get("Essential"))
        print("  mcp-core ports=", [pm.get("ContainerPort") for pm in mcp.get("PortMappings", [])])
        ports = [pm.get("ContainerPort") for pm in mcp.get("PortMappings", [])]
        if 8000 not in ports:
            errors.append("mcp-core must expose container port 8000")
        # MCP must NOT have secrets
        if mcp.get("Secrets"):
            errors.append("mcp-core must not reference secrets")
        # MCP must NOT be essential (so web can run if MCP unhealthy is not needed; but MCP is a sidecar)
        # MCP should not be exposed publicly: no HostPort public mapping beyond container port is fine in awsvpc

    # ARM64 check
    arch = td.get("RuntimePlatform", {}).get("CpuArchitecture")
    if arch != "ARM64":
        errors.append(f"Expected ARM64 architecture, got {arch}")

    # Launch type & network
    svc = resources["Service"]["Properties"]
    net = svc.get("NetworkConfiguration", {}).get("AwsvpcConfiguration", {})
    print("Launch type:", svc.get("LaunchType"))
    print("AssignPublicIp:", net.get("AssignPublicIp"))
    if svc.get("LaunchType") != "FARGATE":
        errors.append("Service launch type must be FARGATE")
    if net.get("AssignPublicIp") != "ENABLED":
        errors.append("Web gateway task needs a public IP (no ALB)")

    # Security group: MCP should not have 0.0.0.0/0 ingress on 8000
    mcp_sg = resources["MCPSecurityGroup"]["Properties"]
    for rule in mcp_sg.get("SecurityGroupIngress", []):
        if rule.get("FromPort") == 8000 and rule.get("CidrIp") == "0.0.0.0/0":
            errors.append("MCP security group must not open port 8000 to 0.0.0.0/0")

    # No hardcoded secrets / account IDs / placeholder key values
    template_text = open(TEMPLATE).read()
    for bad in ["AKIA", "your_gemini_api_key_here", "your_groq_api_key_here"]:
        if bad in template_text:
            errors.append(f"Possible hardcoded secret/pattern found: {bad}")

    print("\n--- RESULT ---")
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    else:
        print("ALL OFFLINE VALIDATION CHECKS PASSED")


if __name__ == "__main__":
    main()
