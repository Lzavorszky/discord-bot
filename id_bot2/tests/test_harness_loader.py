"""Unit tests for the regression harness loader/validator (Phase 0.2).
These run fully offline — no model calls."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ID_BOT2 = os.path.dirname(HERE)
REPO = os.path.dirname(ID_BOT2)
sys.path.insert(0, ID_BOT2)
import run_harness as H  # noqa: E402

CASES_YAML = os.path.join(REPO, "regression_cases.yaml")


def test_real_case_file_loads_and_validates():
    cases = H.load_cases(CASES_YAML)
    assert len(cases) >= 20
    assert H.validate_cases(cases) == []  # the shipped file must be valid


def test_validate_catches_bad_route():
    bad = [{"id": "x", "input": "hi", "status": "new",
            "expect": {"route": "not_a_route"}}]
    problems = H.validate_cases(bad)
    assert any("route" in p for p in problems)


def test_validate_catches_unknown_expect_key():
    bad = [{"id": "x", "input": "hi", "status": "new", "expect": {"frobnicate": 1}}]
    assert any("frobnicate" in p for p in H.validate_cases(bad))


def test_validate_catches_duplicate_id_and_missing_input():
    bad = [
        {"id": "dup", "input": "a", "status": "new", "expect": {}},
        {"id": "dup", "input": "", "status": "new", "expect": {}},
    ]
    problems = H.validate_cases(bad)
    assert any("duplicate id" in p for p in problems)
    assert any("input" in p for p in problems)


def test_text_expectation_matching_is_accent_insensitive():
    verdict, _ = H.check_text_expectations("A dózisa 3 g/day", {"output_has": ["DOZISA", "3 g/day"]})
    assert verdict == H.PASS
    verdict, reasons = H.check_text_expectations("PROPHYLAXIS tier", {"output_not": ["PROPHYLAXIS"]})
    assert verdict == H.FAIL and reasons


def test_offline_run_returns_zero_and_skips():
    rc = H.run(CASES_YAML, target="none")
    assert rc == 0


def test_evaluate_case_old_target_skips_structured_only():
    case = {"id": "s", "input": "x", "status": "known_fail",
            "expect": {"route": "drug_dose", "tool": "get_dose"}}
    out = H.evaluate_case(case, "old", answer_fn=lambda t, chat_id=0: "irrelevant")
    assert out["result"] == H.SKIP


def test_evaluate_case_old_target_checks_text():
    case = {"id": "t", "input": "x", "status": "baseline",
            "expect": {"output_has": ["HIGH_DOSE"]}}
    ok = H.evaluate_case(case, "old", answer_fn=lambda t, chat_id=0: "use HIGH_DOSE path")
    bad = H.evaluate_case(case, "old", answer_fn=lambda t, chat_id=0: "nope")
    assert ok["result"] == H.PASS and bad["result"] == H.FAIL
