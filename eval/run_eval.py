import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import ask  # noqa: E402

DATASET_PATH = Path(__file__).parent / "dataset.json"
RESULTS_DIR = Path(__file__).parent / "results"


def find_number_near(text: str, expected, tolerance: float) -> float | None:
    # `expected` may be a single number or a list of acceptable numbers (used
    # for ratio-style answers like "15.56%" vs "0.1556", where we don't know
    # in advance which unit the model will report in). Scans every number in
    # the tool's raw result text rather than assuming position, since the
    # model's own SQL may return columns in a different order than ours.
    candidates = expected if isinstance(expected, list) else [expected]
    for m in re.findall(r"-?\d[\d,]*\.?\d*", text):
        try:
            val = float(m.replace(",", ""))
        except ValueError:
            continue
        for target in candidates:
            if abs(val - target) <= tolerance:
                return val
    return None


def find_tool_call(trace: list, tool_names) -> dict | None:
    # `tool_names` may be a single tool name or a list of acceptable names —
    # some questions can be correctly answered via more than one tool (e.g.
    # "which category has the highest sales" can go through query_database
    # or get_chart_data(group_by=category), and the model isn't fully
    # deterministic about which it picks at temperature=0.2).
    #
    # Prefers the last successful call over the first match: the model
    # sometimes retries a tool after an earlier attempt errors (e.g. invalid
    # SQL), and the retry's result is what actually answered the question.
    # Falls back to the last call if every attempt errored, so failures still
    # surface instead of silently reporting "tool never called".
    wanted = {tool_names} if isinstance(tool_names, str) else set(tool_names)
    matches = [entry for entry in trace if entry["tool"] in wanted]
    if not matches:
        return None
    for entry in reversed(matches):
        if not entry["is_error"]:
            return entry
    return matches[-1]


def grade_numeric(case: dict, trace: list, charts: list) -> tuple[bool, str]:
    call = find_tool_call(trace, case["expected_tool"])
    if call is None:
        called = ", ".join(sorted({t["tool"] for t in trace})) or "none"
        return False, f"expected tool {case['expected_tool']!r}, but called: {called}"
    if call["is_error"]:
        return False, f"{call['tool']} errored: {call['result']}"
    expected = case["expected"]["value"]
    tolerance = case["expected"].get("tolerance", 0)
    found = find_number_near(str(call["result"]), expected, tolerance)
    if found is None:
        return False, f"expected {expected} (±{tolerance}) not found in: {call['result']!r}"
    return True, f"{found} (expected {expected})"


def grade_top_entity(case: dict, trace: list, charts: list) -> tuple[bool, str]:
    call = find_tool_call(trace, case["expected_tool"])
    if call is None:
        called = ", ".join(sorted({t["tool"] for t in trace})) or "none"
        return False, f"expected tool {case['expected_tool']!r}, but called: {called}"
    if call["is_error"]:
        return False, f"{call['tool']} errored: {call['result']}"
    text = str(call["result"])
    label = case["expected"]["label"]
    if label.lower() not in text.lower():
        return False, f"expected {label!r} in tool result, got: {text[:300]!r}"
    if "value" in case["expected"]:
        expected_value = case["expected"]["value"]
        tolerance = case["expected"].get("tolerance", 0)
        if find_number_near(text, expected_value, tolerance) is None:
            return False, f"found {label!r} but no number near {expected_value} in: {text[:300]!r}"
    return True, f"{label!r} found in tool result"


def grade_list_contains(case: dict, trace: list, charts: list) -> tuple[bool, str]:
    call = find_tool_call(trace, case["expected_tool"])
    if call is None:
        called = ", ".join(sorted({t["tool"] for t in trace})) or "none"
        return False, f"expected tool {case['expected_tool']!r}, but called: {called}"
    if call["is_error"]:
        return False, f"{call['tool']} errored: {call['result']}"
    text = str(call["result"]).lower()
    missing = [item for item in case["expected"]["items"] if item.lower() not in text]
    if missing:
        return False, f"missing from tool result: {missing}"
    forbidden_present = [
        item for item in case["expected"].get("not_items", []) if item.lower() in text
    ]
    if forbidden_present:
        return False, f"unexpectedly present in tool result: {forbidden_present}"
    return True, f"all {len(case['expected']['items'])} required items found"


def grade_chart_total(case: dict, trace: list, charts: list) -> tuple[bool, str]:
    call = find_tool_call(trace, case["expected_tool"])
    if call is None:
        called = ", ".join(sorted({t["tool"] for t in trace})) or "none"
        return False, f"expected tool {case['expected_tool']!r}, but called: {called}"
    if call["is_error"]:
        return False, f"{call['tool']} errored: {call['result']}"
    if not charts:
        return False, "expected a chart, but none was returned"
    actual = sum(charts[0]["data"].values())
    expected = case["expected"]["value"]
    tolerance = case["expected"].get("tolerance", 0)
    if abs(actual - expected) > tolerance:
        return False, f"expected chart total {expected}, got {actual}"
    return True, f"{actual} (expected {expected})"


GRADERS = {
    "numeric": grade_numeric,
    "chart_total": grade_chart_total,
    "top_entity": grade_top_entity,
    "list_contains": grade_list_contains,
}


def run_case(case: dict) -> tuple[bool, str]:
    grader = GRADERS.get(case["expected"]["type"])
    if grader is None:
        return False, f"unknown expected.type {case['expected']['type']!r}"
    _answer, charts, _interaction_id, trace = ask(
        case["question"], log_history=False, return_trace=True
    )
    return grader(case, trace, charts)


def main():
    dataset = json.loads(DATASET_PATH.read_text())
    results = []
    passed = 0
    # Cases tagged "known-issue" are real, already-documented model
    # limitations (see eval/README.md) — they're expected to fail, and one of
    # them is even flaky (passes some runs). Failing on those isn't a
    # regression, so they're tracked and reported separately from cases that
    # broke unexpectedly.
    regressions = []
    known_issue_still_failing = []
    known_issue_now_passing = []

    for case in dataset:
        is_known_issue = "known-issue" in case.get("tags", [])
        print(f"running {case['id']}...", end=" ", flush=True)
        try:
            ok, detail = run_case(case)
        except Exception as e:
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        status = "PASS" if ok else "FAIL"
        tag = " (known issue)" if is_known_issue else ""
        print(f"[{status}]{tag} {detail}")
        results.append({
            "id": case["id"], "status": status, "detail": detail,
            "known_issue": is_known_issue,
        })
        if ok:
            passed += 1
            if is_known_issue:
                known_issue_now_passing.append(case["id"])
        elif is_known_issue:
            known_issue_still_failing.append(case["id"])
        else:
            regressions.append(case["id"])

    total = len(dataset)
    scored_total = total - sum(1 for c in dataset if "known-issue" in c.get("tags", []))
    scored_passed = scored_total - len(regressions)

    print(f"\n{passed}/{total} passed")
    print(f"{scored_passed}/{scored_total} passed, excluding known issues "
          "(this is the number that matters for \"did I break something\")")
    if known_issue_still_failing:
        print(f"known issues still failing (expected): {known_issue_still_failing}")
    if known_issue_now_passing:
        print(f"known issues passing this run (intermittent, not a fix): {known_issue_now_passing}")
    if regressions:
        print(f"REGRESSIONS (unexpected failures): {regressions}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"results saved to {out_path}")

    if regressions:
        sys.exit(1)


if __name__ == "__main__":
    main()
