"""Tests for the code_execution safety policy.

Uses lightweight stand-ins for the SDK's content blocks — the guard only ever
reads `.type`, `.name`, and `.input`.
"""
from types import SimpleNamespace

import pytest

from code_execution_guard import has_unsafe_code_execution, is_safe_skill_read


def request(name, **inp):
    """A server_tool_use block, as the model emits when calling a server tool."""
    return SimpleNamespace(type="server_tool_use", name=name, input=inp)


def result(block_type="code_execution_tool_result"):
    """The tool_result block that follows a server_tool_use."""
    return SimpleNamespace(type=block_type, name="", input={})


def text(body="hello"):
    return SimpleNamespace(type="text", text=body, name="", input={})


class TestIsSafeSkillRead:
    def test_text_editor_view_of_skills_path(self):
        assert is_safe_skill_read(
            request("text_editor_code_execution", command="view", path="/skills/rules/SKILL.md")
        )

    def test_bash_cat_of_skills_path(self):
        assert is_safe_skill_read(
            request("bash_code_execution", command="cat /skills/rules/SKILL.md")
        )

    def test_bash_cat_tolerates_leading_whitespace(self):
        assert is_safe_skill_read(
            request("bash_code_execution", command="  cat /skills/x.md")
        )

    @pytest.mark.parametrize("block", [
        request("text_editor_code_execution", command="view", path="/etc/passwd"),
        request("text_editor_code_execution", command="create", path="/skills/x.md"),
        request("bash_code_execution", command="cat /etc/passwd"),
        request("bash_code_execution", command="python compute.py"),
        request("bash_code_execution", command="rm -rf /skills/"),
        request("web_search", query="revenue"),
    ])
    def test_rejects_everything_else(self, block):
        assert not is_safe_skill_read(block)

    def test_missing_input_does_not_crash(self):
        assert not is_safe_skill_read(SimpleNamespace(type="server_tool_use",
                                                     name="bash_code_execution", input=None))


class TestHasUnsafeCodeExecution:
    def test_plain_text_response_is_safe(self):
        assert not has_unsafe_code_execution([text()])

    def test_empty_response_is_safe(self):
        assert not has_unsafe_code_execution([])

    def test_safe_skill_read_with_its_result_is_allowed(self):
        blocks = [
            request("bash_code_execution", command="cat /skills/SKILL.md"),
            result(),
            text(),
        ]
        assert not has_unsafe_code_execution(blocks)

    def test_unsafe_request_is_caught(self):
        assert has_unsafe_code_execution([
            request("bash_code_execution", command="python -c 'print(1)'"),
            result(),
        ])

    def test_orphan_result_without_a_vetted_request_is_caught(self):
        # A result block alone carries no command/path, so it can't be vetted —
        # it must not be assumed safe.
        assert has_unsafe_code_execution([result()])

    def test_one_safe_read_does_not_whitelist_a_later_result(self):
        blocks = [
            request("bash_code_execution", command="cat /skills/SKILL.md"),
            result(),
            result(),  # second result, no matching vetted request
        ]
        assert has_unsafe_code_execution(blocks)

    def test_non_code_execution_server_tool_is_ignored(self):
        assert not has_unsafe_code_execution([request("web_search", query="x"), text()])

    def test_unsafe_after_safe_is_still_caught(self):
        blocks = [
            request("bash_code_execution", command="cat /skills/SKILL.md"),
            result(),
            request("bash_code_execution", command="python plot.py"),
        ]
        assert has_unsafe_code_execution(blocks)
