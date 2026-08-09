"""Safety policy for the server-side code_execution tool.

The code_execution tool has no access to the sample_superstore database — its
only legitimate use in this app is reading the metric-aggregation-rules Skill
file the container mounts read-only under /skills/. These helpers vet a
response's content blocks so ask() can reject any other use of code_execution
before its (unverifiable) output reaches the user.
"""


def _is_safe_skill_read(block) -> bool:
    """True only for the one allowed code_execution use: reading the Skill file
    under /skills/, via the text_editor "view" command or a plain `cat`.

    Anything else touching code_execution (writing/running scripts, reading
    elsewhere) is out of scope — the model has no real access to
    sample_superstore through this tool, so using it for anything but reading
    the skill risks a fabricated answer.
    """
    name = getattr(block, "name", "") or ""
    if "code_execution" not in name:
        return False
    inp = getattr(block, "input", None) or {}
    cmd = inp.get("command", "")
    path = inp.get("path", "")
    if name == "text_editor_code_execution" and cmd == "view" and str(path).startswith("/skills/"):
        return True
    if name == "bash_code_execution" and isinstance(cmd, str) and cmd.strip().startswith("cat /skills/"):
        return True
    return False


def has_unsafe_code_execution(content_blocks) -> bool:
    """True if any code_execution use in `content_blocks` is not a safe Skill
    read.

    Walks the block list in order so a *_tool_result block can be matched
    against the server_tool_use request that produced it — the result block
    alone doesn't carry the command/path needed to vet it.
    """
    safe_result_pending = False
    for block in content_blocks:
        if block.type == "server_tool_use":
            if "code_execution" not in (getattr(block, "name", "") or ""):
                safe_result_pending = False
                continue
            if _is_safe_skill_read(block):
                safe_result_pending = True
            else:
                return True
        elif "code_execution" in block.type:
            if safe_result_pending:
                safe_result_pending = False
            else:
                return True
    return False
