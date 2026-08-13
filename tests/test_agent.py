"""Tests for the agent's request assembly and tool-call execution.

No API calls: `ask()` itself needs the network, but the pieces it is built from
are pure and tested directly here.
"""
from types import SimpleNamespace

import agent
from agent import ToolCallTrace, build_skills, build_tools, execute_tool_calls
from contracts import ChartType, ToolResult


def tool_use(name, tool_input, block_id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id,
                           model_dump=lambda mode="json": {"type": "tool_use", "name": name})


def text(body):
    return SimpleNamespace(type="text", text=body,
                           model_dump=lambda mode="json": {"type": "text", "text": body})


def response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


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


class TestGroundingCorrection:
    """ask()'s one corrective round-trip when a draft answer says something no
    tool returned. The API is replaced by a scripted sequence of responses, so
    these run offline like everything else here.
    """

    @staticmethod
    def _scripted(monkeypatch, *responses):
        """Points ask() at a fake client replaying `responses`, and returns the
        list of messages each call was made with.
        """
        monkeypatch.delenv("SKILL_ID", raising=False)
        sent = []
        remaining = list(responses)

        def create(**kwargs):
            sent.append(kwargs["messages"][-1])
            return remaining.pop(0)

        monkeypatch.setattr(agent, "get_client", lambda: SimpleNamespace(
            messages=SimpleNamespace(create=create)))
        return sent

    # A real tool call, so there is genuine tool output to check the answer
    # against — the check is skipped entirely when nothing was called.
    _REVENUE_CALL = ("get_revenue", {"start_date": "2025-01-01", "end_date": "2025-12-31"})

    def test_an_ungrounded_answer_is_sent_back_once(self, monkeypatch):
        sent = self._scripted(
            monkeypatch,
            response(tool_use(*self._REVENUE_CALL), stop_reason="tool_use"),
            response(text("Revenue was $613,933.58, which is $50,000 per month.")),
            response(text("Revenue was $613,933.58.")),
        )
        result = agent.ask("revenue in 2025?", log_history=False)

        assert result.answer == "Revenue was $613,933.58."
        # The last thing the model was asked was the correction, naming the
        # figure it made up rather than a generic complaint.
        assert "50,000" in sent[-1]["content"]

    def test_a_grounded_answer_is_returned_without_a_second_pass(self, monkeypatch):
        sent = self._scripted(
            monkeypatch,
            response(tool_use(*self._REVENUE_CALL), stop_reason="tool_use"),
            response(text("Revenue was $613,933.58.")),
        )
        result = agent.ask("revenue in 2025?", log_history=False)

        assert result.answer == "Revenue was $613,933.58."
        assert len(sent) == 2

    def test_an_answer_with_no_tool_calls_is_not_checked(self, monkeypatch):
        # "What can you do?" has nothing to be grounded in; checking it would
        # flag the whole reply.
        self._scripted(monkeypatch, response(text("I can answer questions about sales.")))
        result = agent.ask("what can you do?", log_history=False)
        assert result.answer == "I can answer questions about sales."

    def test_a_still_ungrounded_rewrite_is_returned_rather_than_looped(self, monkeypatch):
        sent = self._scripted(
            monkeypatch,
            response(tool_use(*self._REVENUE_CALL), stop_reason="tool_use"),
            response(text("Revenue was $613,933.58, or $50,000 per month.")),
            response(text("Revenue was $613,933.58, roughly $51,161 per month.")),
        )
        result = agent.ask("revenue in 2025?", log_history=False)

        assert "51,161" in result.answer
        assert len(sent) == 3


def server_tool_use(name, tool_input):
    return SimpleNamespace(type="server_tool_use", name=name, input=tool_input,
                           model_dump=lambda mode="json": {"type": "server_tool_use"})


class TestCodeExecutionRecovery:
    """What happens when a turn detours into the blocked code_execution tool
    after real tools have already returned data.
    """

    _PLOT_ATTEMPT = server_tool_use("bash_code_execution", {"command": "python plot.py"})
    _REVENUE_CALL = ("get_revenue", {"start_date": "2025-01-01", "end_date": "2025-12-31"})

    def test_the_model_is_asked_to_answer_from_the_data_it_has(self, monkeypatch):
        sent = TestGroundingCorrection._scripted(
            monkeypatch,
            response(tool_use(*self._REVENUE_CALL), stop_reason="tool_use"),
            response(text("Let me plot that."), self._PLOT_ATTEMPT),
            response(text("Revenue was $613,933.58.")),
        )
        result = agent.ask("chart revenue for 2025", log_history=False)

        assert result.answer == "Revenue was $613,933.58."
        assert "code_execution tool is not available" in sent[-1]["content"]

    def test_raw_tool_output_is_never_handed_over_as_the_answer(self, monkeypatch):
        # The regression this exists for: the answer used to be the tool
        # results joined together, which reached the user as a wall of numbers
        # carrying the tools' own instructions to the model ("summarize the
        # trend rather than restating values").
        TestGroundingCorrection._scripted(
            monkeypatch,
            response(tool_use(*self._REVENUE_CALL), stop_reason="tool_use"),
            response(self._PLOT_ATTEMPT),
            response(self._PLOT_ATTEMPT),
        )
        result = agent.ask("chart revenue for 2025", log_history=False)

        assert result.answer == agent.UNSAFE_CODE_EXECUTION_MESSAGE
        assert "Total revenue" not in result.answer

    def test_a_detour_before_any_tool_ran_is_refused_outright(self, monkeypatch):
        # Nothing has been fetched, so there is nothing to write an answer from.
        TestGroundingCorrection._scripted(monkeypatch, response(self._PLOT_ATTEMPT))
        result = agent.ask("chart revenue for 2025", log_history=False)
        assert result.answer == agent.UNSAFE_CODE_EXECUTION_MESSAGE

    def test_charts_already_produced_survive_the_detour(self, monkeypatch):
        # The blocked turn doesn't invalidate what the real tools returned.
        TestGroundingCorrection._scripted(
            monkeypatch,
            response(tool_use("get_chart_data", {
                "metric": "revenue", "group_by": "category",
                "start_date": "2025-01-01", "end_date": "2025-12-31",
            }), stop_reason="tool_use"),
            response(self._PLOT_ATTEMPT),
            response(text("Technology leads.")),
        )
        result = agent.ask("chart revenue by category", log_history=False)
        assert [c["chart_type"] for c in result.charts] == [ChartType.BAR]


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
        # The counterweight to those three: they constrain what may be
        # claimed, and on their own pushed the model into one-line answers
        # that dropped context it used to fetch (a top-by-revenue customer
        # being loss-making). This one asks for the context back — as more
        # tool calls, not as more prose.
        assert "SEPARATE query for each candidate measure" in rules
        # ...and the guard that rule needed: fetching the competing measures in
        # one ranked, LIMITed query made the model rank by those columns too,
        # naming an 11-order customer the top by order count when eight
        # customers outside the top-10-by-revenue had more.
        assert "wasn't ranked by" in rules

    def test_max_turns_message_states_the_actual_limit(self):
        assert str(agent.MAX_TURNS) in agent.MAX_TURNS_MESSAGE
