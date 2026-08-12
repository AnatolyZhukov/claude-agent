"""Tests for the agent's request assembly and tool-call execution.

No API calls: `ask()` itself needs the network, but the pieces it is built from
are pure and tested directly here.
"""
from types import SimpleNamespace

import agent
from agent import ToolCallTrace, build_skills, build_tools, execute_tool_calls
from contracts import ToolResult


def tool_use(name, tool_input, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def response(*blocks):
    return SimpleNamespace(content=list(blocks))


class TestBuildSkills:
    def test_no_skill_id_means_no_skills(self, monkeypatch):
        monkeypatch.delenv("SKILL_ID", raising=False)
        assert build_skills() == []

    def test_skill_id_produces_a_container_skill(self, monkeypatch):
        monkeypatch.setenv("SKILL_ID", "skill_123")
        assert build_skills() == [
            {"type": "custom", "skill_id": "skill_123", "version": "latest"}
        ]


class TestBuildTools:
    def test_without_skills_only_the_db_tools_are_sent(self):
        names = {t["name"] for t in build_tools([])}
        assert "code_execution" not in names
        assert "get_revenue" in names

    def test_with_skills_code_execution_is_added(self):
        # Required by the API whenever container.skills is used at all.
        tools = build_tools([{"type": "custom", "skill_id": "x", "version": "latest"}])
        assert any(t.get("name") == "code_execution" for t in tools)

    def test_does_not_mutate_the_cached_schemas(self):
        before = len(build_tools([]))
        build_tools([{"type": "custom", "skill_id": "x", "version": "latest"}])
        assert len(build_tools([])) == before


class TestExecuteToolCalls:
    def test_non_tool_blocks_are_ignored(self):
        batch = execute_tool_calls(response(SimpleNamespace(type="text", text="hi")))
        assert batch.tool_results == []
        assert batch.trace == []

    def test_successful_call_is_traced_and_summarized(self):
        batch = execute_tool_calls(response(tool_use(
            "get_revenue", {"start_date": "2025-01-01", "end_date": "2025-12-31"}
        )))
        assert len(batch.trace) == 1
        assert batch.trace[0].tool == "get_revenue"
        assert not batch.trace[0].is_error
        assert batch.summaries == ["Total revenue: 613933.58"]
        assert batch.tool_results[0]["tool_use_id"] == "tu_1"
        assert batch.tool_results[0]["type"] == "tool_result"

    def test_chart_is_collected_separately_from_text(self):
        batch = execute_tool_calls(response(tool_use("get_chart_data", {
            "metric": "revenue", "group_by": "category",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
        })))
        assert len(batch.charts) == 1
        # The model still gets the text, not the chart dict.
        assert isinstance(batch.tool_results[0]["content"], str)

    def test_failed_call_is_traced_but_not_summarized(self):
        batch = execute_tool_calls(response(tool_use("nope", {})))
        assert batch.trace[0].is_error
        # Nothing to salvage from a failed call.
        assert batch.summaries == []
        # The model is still told what went wrong.
        assert "Unknown tool" in batch.tool_results[0]["content"]

    def test_multiple_blocks_are_all_executed_in_order(self):
        batch = execute_tool_calls(response(
            tool_use("get_revenue", {"start_date": "2025-01-01", "end_date": "2025-12-31"}, "a"),
            tool_use("get_active_users", {"start_date": "2025-01-01", "end_date": "2025-12-31"}, "b"),
        ))
        assert [t.tool for t in batch.trace] == ["get_revenue", "get_active_users"]
        assert [r["tool_use_id"] for r in batch.tool_results] == ["a", "b"]


class TestToolCallTrace:
    def test_of_copies_the_result_contract(self):
        trace = ToolCallTrace.of("t", {"a": 1}, ToolResult("text", is_error=True))
        assert (trace.tool, trace.input, trace.result, trace.is_error) == (
            "t", {"a": 1}, "text", True,
        )


class TestPrompt:
    def test_schema_is_embedded_in_the_system_prompt(self):
        assert "orders(" in agent.SYSTEM_PROMPT
        assert "region values:" in agent.SYSTEM_PROMPT

    def test_answer_grounding_rules_are_stated(self):
        # These three rules exist because of specific observed failures (see
        # the project checklist): naming a customer that no tool call had
        # returned, computing shares in prose and getting them wrong, and
        # inventing a definition for "cost". Nothing else in the test suite
        # would notice if one of them were dropped from the prompt.
        rules = agent.SYSTEM_PROMPT
        assert "must appear verbatim in a tool result" in rules
        assert "in your head" in rules
        assert "which definition you used" in rules

    def test_max_turns_message_states_the_actual_limit(self):
        assert str(agent.MAX_TURNS) in agent.MAX_TURNS_MESSAGE
