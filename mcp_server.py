import ast
import re
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sklearn.ensemble import RandomForestClassifier

app = FastAPI(title="CodeNexus AI — MCP Core Engine", version="2.0.0")

# ─── Machine Learning Risk Classifier Model Initialization ────────
#
# EXPERIMENTAL NOTICE:
# This Random Forest classifier is trained on a small synthetic dataset (6 rows)
# with manually-assigned labels. It has NOT been evaluated on independently
# labelled real-world code. Accuracy, precision, recall, and F1 cannot be
# meaningfully computed from 6 synthetic samples. Risk levels produced by
# this model should be treated as rough heuristics, not verified predictions.
# A deterministic static-analysis baseline (health_score formula below) is
# the primary, trusted signal.
#
def train_ml_risk_classifier():
    # Synthetic AST Training dataset: [complexity, line_count, bare_excepts, todo_count, nesting_depth]
    X_train = np.array([
        [1, 10, 0, 0, 1],   # Low risk (0)
        [2, 25, 0, 1, 2],   # Low risk (0)
        [5, 50, 1, 0, 3],   # Medium risk (1)
        [8, 80, 2, 2, 4],   # Medium risk (1)
        [14, 150, 3, 4, 6], # High risk (2)
        [22, 300, 5, 8, 8], # Critical risk (3)
    ])
    y_train = np.array([0, 0, 1, 1, 2, 3]) # 0: Low, 1: Medium, 2: High, 3: Critical
    model = RandomForestClassifier(n_estimators=10, random_state=42)
    model.fit(X_train, y_train)
    return model

ml_model = train_ml_risk_classifier()
RISK_MAP = {0: "Low", 1: "Medium", 2: "High", 3: "Critical"}

class CodePayload(BaseModel):
    filename: str
    content: str
    language: str = "python"

def extract_python_ast_features(code: str):
    if not code or not code.strip():
        return 1, 0, 0, 0, 1, "Empty or whitespace-only source code", 0
    try:
        tree = ast.parse(code)
        lines = len(code.splitlines())
        bare_excepts = sum(1 for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and node.type is None)
        todos = len(re.findall(r"#\s*TODO", code, re.IGNORECASE))
        ifs = sum(1 for node in ast.walk(tree) if isinstance(node, ast.If))
        loops = sum(1 for node in ast.walk(tree) if isinstance(node, (ast.For, ast.While)))
        complexity = 1 + ifs + loops
        
        # Count None comparisons using == (AST-based, ignores comments/strings)
        none_eq = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op in node.ops:
                    if isinstance(op, ast.Eq):
                        for cmp_node in node.comparators:
                            if isinstance(cmp_node, ast.Constant) and cmp_node.value is None:
                                none_eq += 1
        
        # Max nesting depth
        def get_depth(node, depth=0):
            if isinstance(node, (ast.For, ast.While, ast.If, ast.With, ast.Try)):
                depth += 1
            children = list(ast.iter_child_nodes(node))
            if not children:
                return depth
            return max(get_depth(child, depth) for child in children)
            
        nesting_depth = get_depth(tree)
        return complexity, lines, bare_excepts, todos, nesting_depth, None, none_eq
    except Exception as e:
        lines = len(code.splitlines()) if code else 0
        todos = len(re.findall(r"#\s*TODO", code, re.IGNORECASE))
        return 5, lines, 0, todos, 2, str(e), 0

def extract_multi_language_features(code: str, lang: str):
    lines = len(code.splitlines()) if code else 0
    todos = len(re.findall(r"(//|#|\*)\s*TODO", code, re.IGNORECASE))
    
    if lang in ["java", "cpp", "c++"]:
        mallocs = len(re.findall(r"(malloc|new)", code))
        frees = len(re.findall(r"(free|delete)", code))
        unfreed_pointers = max(0, mallocs - frees)
        loose_equals = len(re.findall(r"(?<!=)==(?!=)", code))
        complexity = 1 + len(re.findall(r"(if|for|while|switch|case)", code))
        nesting_depth = max(1, len(re.findall(r"\{", code)) - len(re.findall(r"\}", code)) + 2)
        return complexity, lines, unfreed_pointers, todos, nesting_depth, None, loose_equals

    elif lang in ["javascript", "typescript", "js", "ts"]:
        loose_equals = len(re.findall(r"(?<!=)==(?!=)", code))
        eval_usage = len(re.findall(r"eval\(", code))
        complexity = 1 + len(re.findall(r"(if|for|while|switch|catch)", code))
        return complexity, lines, eval_usage + loose_equals, todos, 2, None, loose_equals

    return extract_python_ast_features(code)

@app.get("/health")
def health():
    return {"status": "online", "engine": "CodeNexus MCP 2.0 (ML + AST Enabled)"}

@app.post("/code/metrics")
def get_metrics(payload: CodePayload):
    comp, lines, bare, todos, depth, err, extra = extract_multi_language_features(payload.content, payload.language.lower())
    features = np.array([[comp, lines, bare, todos, depth]])
    risk_idx = ml_model.predict(features)[0]
    risk_level = RISK_MAP.get(risk_idx, "Medium")
    
    # Calculate Health Score (0–100)
    # Formula: deductions scaled by severity. Parse errors cap score at 50.
    deductions = (comp * 2) + (bare * 10) + (depth * 5) + (todos * 2)
    if err:
        health_score = max(5, min(50, int(100 - deductions)))
    else:
        health_score = max(5, min(100, int(100 - deductions)))

    return {
        "cyclomatic_complexity": comp,
        "line_count": lines,
        "bare_excepts_or_unfreed": bare,
        "todo_count": todos,
        "nesting_depth": depth,
        "health_score": health_score,
        "risk_level": risk_level,
        "parse_error": err,
        "none_eq_count": extra,
        "ml_classifier": "experimental"
    }

@app.post("/code/analyze")
def analyze(payload: CodePayload):
    metrics = get_metrics(payload)
    issues = []
    if metrics["cyclomatic_complexity"] > 10:
        issues.append("High Cyclomatic Complexity (>10). Modularize function logic.")
    if metrics["bare_excepts_or_unfreed"] > 0:
        issues.append(f"Detected {metrics['bare_excepts_or_unfreed']} unhandled exceptions/memory leaks.")
    if metrics["todo_count"] > 0:
        issues.append(f"Found {metrics['todo_count']} uncompleted TODO items.")
    return {"issues": issues, "metrics": metrics}

@app.post("/code/find_bugs")
def find_bugs(payload: CodePayload):
    metrics = get_metrics(payload)
    bugs = []
    if payload.language.lower() == "python":
        if metrics.get("none_eq_count", 0) > 0:
            bugs.append("Anti-pattern: Comparison to None using '==' (Use 'is None').")
        if metrics.get("bare_excepts_or_unfreed", 0) > 0:
            bugs.append("Critical Bug: Bare except clause catches SystemExit & KeyboardInterrupt.")
    elif payload.language.lower() in ["cpp", "c++"]:
        if "malloc" in payload.content and "free" not in payload.content:
            bugs.append("Memory Leak: 'malloc' called without corresponding 'free'.")
    elif payload.language.lower() in ["javascript", "js"]:
        if "==" in payload.content and "===" not in payload.content:
            bugs.append("Type Safety: Loose equality operator '==' used instead of strict '==='.")
            
    return {"bugs": bugs, "risk_level": metrics["risk_level"]}

@app.post("/code/refactor")
def refactor(payload: CodePayload):
    refactored = payload.content
    if payload.language.lower() == "python":
        refactored = refactored.replace("== None", "is None").replace("except:", "except Exception:")
    elif payload.language.lower() in ["javascript", "js"]:
        refactored = re.sub(r"(?<!=)==(?!=)", "===", refactored)
    return {"refactored_code": refactored}

@app.post("/code/document")
def document(payload: CodePayload):
    doc_header = f"/**\n * File: {payload.filename}\n * Language: {payload.language}\n * Auto-documented by CodeNexus AI Engine\n */\n\n"
    return {"documented_code": doc_header + payload.content}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
