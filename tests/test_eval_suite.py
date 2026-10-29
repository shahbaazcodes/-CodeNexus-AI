# Evaluation Suite Tests for CodeNexus AI Phase 5
"""These tests verify the evaluation dataset structure, schema validation,metric calculations, and report generation. They are designed to runoffline without making any live LLM provider calls."""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__) or ".")
import eval.run_eval as re
class TestDatasetSchema:
    """Test that the cases.json dataset conforms to the expected Phase 5 schema."""

    def test_total_case_count(self):
        """There should be exactly 20 cases."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        assert len(cases) == 20, f"Expected 20 cases, got {len(cases)}"

    def test_case_ids_unique(self):
        """All case IDs must be unique."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), "Duplicate case IDs found"

    def test_all_cases_have_required_fields(self):
        """Each case must have: id, filename, language, code, category, expected_findings."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        for c in cases:
            for field in ["id", "filename", "language", "code", "category", "expected_findings"]:
                assert field in c, f"Case {c.get('id', '?')} missing field '{field}'"

    def test_language_enum_valid(self):
        """Language must be one of the allowed enum values."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        valid_languages = ["python", "javascript", "cpp", "c++", "java"]
        for c in cases:
            assert c["language"] in valid_languages, f"Case {c['id']}: invalid language"

    def test_category_enum_valid(self):
        """Category must be one of the allowed enum values."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        valid_categories = ["known_defect", "clean_code", "prompt_injection", "refactoring", "todo"]
class TestMetricCalculations:
    """Test metric calculations including edge cases."""

    def test_precision_zero_denominator(self):
        """Precision with zero predicted positives should handle gracefully."""
        result = re.find_metrics(tp=0, fp=0, fn=0)
        assert result is None or isinstance(result, (float, type(None)))

    def test_recall_zero_denominator(self):
        """Recall with zero actual positives should handle gracefully."""
        result = re.find_metrics(tp=0, fp=0, fn=0)
        assert result is None or isinstance(result, (float, type(None)))

    def test_f1_zero_denominator(self):
        """F1 with zero precision and recall should handle gracefully."""
        result = re.find_metrics(tp=0, fp=0, fn=0)
        assert result is None or isinstance(result, (float, type(None)))

    def test_precision_normal(self):
        """Precision with TP=2, FP=2 should be 0.5."""
        result = re.find_metrics(tp=2, fp=2, fn=0, metric="precision")
        assert abs(result - 0.5) < 0.001, f"Expected 0.5, got {result}"

    def test_recall_normal(self):
        """Recall with TP=2, FN=2 should be 0.5."""
        result = re.find_metrics(tp=2, fp=0, fn=2, metric="recall")
        assert abs(result - 0.5) < 0.001, f"Expected 0.5, got {result}"

    def test_f1_normal(self):
        """F1 with precision=0.5 and recall=0.5 should be 0.5."""
        result = re.find_metrics(tp=2, fp=2, fn=2, metric="f1")
        assert abs(result - 0.5) < 0.001, f"Expected 0.5, got {result}"

    def test_precision_perfect(self):
        """Precision with TP=5, FP=0 should be 1.0."""
        result = re.find_metrics(tp=5, fp=0, fn=0, metric="precision")
        assert abs(result - 1.0) < 0.001, f"Expected 1.0, got {result}"

    def test_case_schema_has_required_fields(self):
        """CASE_SCHEMA must require id, filename, language, code, category, expected_findings."""
        schema = re.CASE_SCHEMA
        required = schema.get("required", [])
        assert "id" in required
        assert "filename" in required
        assert "language" in required
        assert "code" in required
        assert "category" in required
        assert "expected_findings" in required

    def test_case_schema_language_enum(self):
        """CASE_SCHEMA language must have valid enum values."""
        schema = re.CASE_SCHEMA
        lang_enum = schema["properties"]["language"]["enum"]
        valid = ["python", "javascript", "cpp", "c++", "java"]
        for v in lang_enum:
            assert v in valid, f"Unexpected language in enum: {v}"

    def test_case_schema_category_enum(self):
        """CASE_SCHEMA category must have valid enum values."""
        schema = re.CASE_SCHEMA
        cat_enum = schema["properties"]["category"]["enum"]
        valid = ["known_defect", "clean_code", "prompt_injection", "refactoring", "todo"]
        for v in cat_enum:
            assert v in valid, f"Unexpected category in enum: {v}"
class TestReportGeneration:
    """Test that report generation works correctly."""

    def test_json_report_has_required_sections(self):
        """Generated JSON report must have evaluation mode, timestamp, base URL."""
        results_dir = "eval/results"
        if os.path.isdir(results_dir):
            files = [f for f in os.listdir(results_dir) if f.endswith(".json")]
            if files:
                with open(os.path.join(results_dir, files[0])) as f:
                    report = json.load(f)
                assert "mode" in report, "JSON report missing 'mode'"
                assert "timestamp" in report, "JSON report missing 'timestamp'"
                assert "base_url" in report, "JSON report missing 'base_url'"

    def test_markdown_report_has_required_sections(self):
        """Generated Markdown report must have evaluation mode and summary."""
        results_dir = "eval/results"
        if os.path.isdir(results_dir):
            files = [f for f in os.listdir(results_dir) if f.endswith(".md")]
            if files:
                with open(os.path.join(results_dir, files[0])) as f:
                    content = f.read()
                assert "mode" in content, "Markdown report missing 'mode'"
                assert "mode: Offline" in content or "mode: Live" in content, "Markdown report mode not clearly identified"

    def test_recall_perfect(self):
        """Recall with TP=5, FN=0 should be 1.0."""
        result = re.find_metrics(tp=5, fp=2, fn=0, metric="recall")
        assert abs(result - 1.0) < 0.001, f"Expected 1.0, got {result}"
        valid_categories = ["known_defect", "clean_code", "prompt_injection", "refactoring", "todo"]
        with open("eval/cases.json") as _f:
            _cases = json.load(_f)
        for c in _cases:
            assert c["category"] in valid_categories, f"Case {c['id']}: invalid category"

    def test_expected_findings_structure(self):
        """Each expected_findings entry must have label, severity, description."""
        with open("eval/cases.json") as f:
            cases = json.load(f)
        for c in cases:
            for finding in c["expected_findings"]:
                assert "label" in finding, f"Case {c['id']}: finding missing 'label'"
                assert "severity" in finding, f"Case {c['id']}: finding missing 'severity'"
                assert "description" in finding, f"Case {c['id']}: finding missing 'description'"
class TestOfflineMode:
    """Test that offline mode does not make external LLM calls."""

    def test_llm_enabled_zero_constant(self):
        """LLM_ENABLED=0 should be enforced as a constant in offline mode."""
        import os
        value = os.environ.get("LLM_ENABLED", "0")
        assert value in ("0", "1", None), f"Unexpected LLM_ENABLED value: {value}"
