"""File-system sandbox for the RefSynapse agent.

One rule, enforced in code (not in a model prompt): every file the agent
reads or writes on its own initiative must live inside a single folder.
Default: <repo>/agent. Override: settings["agent_sandbox"].

Because the check sits in the tool layer, it applies to EVERY provider -
local LM Studio models and any cloud API alike. A model can only request a
tool call; this module decides whether the path is allowed.

The board itself is shared: items the user placed on the canvas stay visible
to the agent (that is its job). The sandbox limits the file system, not the
canvas. MCP servers are separate programs the user configures by hand; point
them at the same folder if you use them.
"""

import os

from .board_tools import ToolError


def sandbox_root(win):
    """The only folder where the agent may read and create files."""
    raw = win.settings.get("agent_sandbox")
    if not raw:
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        raw = os.path.join(repo, "agent")
    root = os.path.realpath(os.path.abspath(os.path.expanduser(str(raw))))
    os.makedirs(root, exist_ok=True)
    return root


def is_inside(win, path):
    """True if the resolved path lies inside the sandbox (no '..' tricks)."""
    root = os.path.normcase(sandbox_root(win))
    target = os.path.normcase(os.path.realpath(
        os.path.abspath(os.path.expanduser(str(path)))))
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False


def sandbox_path(win, raw, key="path", folder=False):
    """Resolve a target path and refuse anything outside the sandbox."""
    root = sandbox_root(win)
    if not raw:
        raise ToolError("'%s' is required - a path inside the agent folder '%s'"
                        % (key, root))
    target = os.path.realpath(os.path.abspath(os.path.expanduser(str(raw))))
    if not is_inside(win, target):
        raise ToolError("Access denied: the agent may only work inside '%s'. "
                        "Use that folder." % root)
    if not folder and target == root:
        raise ToolError("'%s' is the sandbox folder itself - give a full file name"
                        % target)
    return target