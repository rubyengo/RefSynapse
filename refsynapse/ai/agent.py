"""Agent loop for the RefSynapse assistant.

Glue between providers.py (LLM transport) and board_tools.py (board actions):

    model -> tool_calls -> execute on the GUI thread -> results -> model ...

Design rules:
- Agent.run() BLOCKS and is meant to be called from a worker thread.
  BoardTools already marshals every board call into the GUI thread.
- on_event / confirm callbacks fire from that worker thread; the chat panel
  must marshal them into the GUI thread itself (queued signal).
- Reasoning ("thinking") is streamed to the UI but NEVER stored in history.
- A tool result may carry "_image"; it is stripped from the tool message and
  re-sent as a separate user message (the OpenAI "tool" role has no images).
- Destructive board tools (board_delete) and untrusted MCP calls require
  confirm(name, arguments) -> bool. Without a confirm callback they are
  refused and the model is told to ask the user in chat first.
- cancel() stops the loop and the in-flight HTTP request (Stop button).
- History stays protocol-valid: every assistant tool_call gets a tool answer,
  even when the user cancels mid-batch; old turns are trimmed only at real
  user-message boundaries; old preview images are dropped from the context.

Self-test (offline, scripted provider, real window):
    python -m refsynapse.ai.agent
Live test against the configured backend (LM Studio by default):
    python -m refsynapse.ai.agent --live
"""

import json
import sys
import threading

from .mcp_client import NAME_PREFIX
from .providers import (default_config, make_provider, image_block,
                        text_block, tool_message, user_message)

DEFAULT_MAX_ITERATIONS = 12
DEFAULT_MAX_TOOL_CHARS = 6000
DEFAULT_KEEP_IMAGES = 2
DEFAULT_MAX_HISTORY = 80

SYSTEM_PROMPT = """You are the built-in assistant of RefSynapse, a PureRef-like
reference board. You operate the board exclusively through the provided tools.

Rules:
- Call board_summary or board_list_items first; use the returned uids everywhere.
- Coordinates are scene units on an infinite canvas; sizes are pixels at scale 1.
- Locked items are skipped by move/transform; mention skipped_locked when present.
- Batch independent tool calls in one turn; prefer few, well-chosen calls.
- After visual changes, verify with board_preview when it helps.
- board_delete is destructive: never call it unless the user explicitly asked
  for that deletion in the chat. If unsure, ask instead of deleting.
- External MCP tools (mcp__*) act outside the board; a refusal means the user
  declined the confirmation dialog - ask in chat before trying again.
- Errors come back as data ({"ok": false, "error": ...}); read them and adapt.
- Reply in the user's language, briefly: what you changed and what to look at.
"""


class Agent:
    """One conversation: history + the model/tool loop. Not thread-safe itself;
    run one Agent.run() at a time (the panel disables Send while busy)."""

    def __init__(self, tools, provider, confirm=None, on_event=None,
                 max_iterations=DEFAULT_MAX_ITERATIONS,
                 max_tool_chars=DEFAULT_MAX_TOOL_CHARS,
                 keep_images=DEFAULT_KEEP_IMAGES,
                 max_history=DEFAULT_MAX_HISTORY,
                 system_prompt=None):
        self.tools = tools
        self.provider = provider
        self.confirm = confirm
        self.on_event = on_event
        self.max_iterations = int(max_iterations)
        self.max_tool_chars = int(max_tool_chars)
        self.keep_images = int(keep_images)
        self.max_history = int(max_history)
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.history = []                  # everything except the system message
        self.last_stop = "stop"
        self._cancel = threading.Event()

    # ---- public API ----------------------------------------------------
    def run(self, text, images=()):
        """Blocking agent turn. Returns the final assistant message dict."""
        self._cancel.clear()
        self.last_stop = "stop"
        user = user_message(text, images)
        user["_user_turn"] = True
        self.history.append(user)
        self._trim_history()
        messages = [{"role": "system", "content": self.system_prompt}] + self.history
        final = {"role": "assistant", "content": ""}
        try:
            for iteration in range(1, self.max_iterations + 1):
                if self._cancel.is_set():
                    return self._stopped(final, "cancelled")
                self._emit({"type": "status",
                            "text": "iteration %d/%d" % (iteration, self.max_iterations)})
                assistant, error = self._turn(messages)
                if error is not None:
                    if error.get("cancelled"):
                        return self._stopped(final, "cancelled")
                    self._emit({"type": "error", "message": error.get("message", "unknown")})
                    return self._stopped(final, "error")
                final = assistant
                messages.append(assistant)
                calls = assistant.get("tool_calls") or []
                if not calls:
                    self._emit({"type": "done", "message": assistant})
                    return assistant
                self._run_calls(messages, calls)
                self._prune_images(messages)
            self._emit({"type": "error",
                        "message": "Iteration limit (%d) reached; stopping." % self.max_iterations})
            return self._stopped(final, "limit")
        except Exception as ex:
            self._emit({"type": "error", "message": "%s: %s" % (type(ex).__name__, ex)})
            return self._stopped(final, "error")
        finally:
            self.history = messages[1:]
            self._trim_history()
            self._prune_images(self.history)

    def cancel(self):
        """Stop button: abort the loop and close the streaming socket."""
        self._cancel.set()
        try:
            self.provider.cancel()
        except Exception:
            pass

    def reset(self):
        self.history = []

    # ---- one model turn ------------------------------------------------
    def _turn(self, messages):
        text_parts, message, error = [], None, None
        for event in self.provider.stream(messages, tools=self.tools.schemas()):
            if self._cancel.is_set():
                self.provider.cancel()
                return None, {"cancelled": True, "message": "Cancelled"}
            kind = event.get("type")
            if kind == "text":
                text_parts.append(event.get("text", ""))
                self._emit(event)
            elif kind == "reasoning":
                self._emit(event)                      # UI only, never stored
            elif kind == "error":
                error = event
            elif kind == "done":
                message = event.get("message") or {}
        if error is not None:
            return None, error
        if message is None:
            return None, {"message": "Provider stream ended without a result"}
        message.setdefault("role", "assistant")
        message.setdefault("content", "".join(text_parts))
        return message, None

    # ---- tool batch ----------------------------------------------------
    def _run_calls(self, messages, calls):
        images, answered = [], set()
        for call in calls:
            if self._cancel.is_set():
                break
            name = call.get("name", "")
            args = call.get("arguments") or {}
            self._emit({"type": "tool_call", "name": name, "arguments": args})
            if self.tools.is_dangerous(name) and not self._confirm(name, args):
                if name.startswith(NAME_PREFIX):
                    result = {"ok": False,
                              "error": "Refused: the user declined this external MCP call "
                                       "in the confirmation dialog. Do not retry without "
                                       "asking the user in chat first."}
                else:
                    result = {"ok": False,
                              "error": "Refused: destructive board action needs explicit "
                                       "user confirmation. Ask the user in chat first."}
            elif isinstance(args, dict) and args.get("error") and "raw" in args:
                result = self.tools.call(name, {"__error__": args["error"],
                                                "__raw__": args["raw"]})
            else:
                result = self.tools.call(name, args)
            answered.add(call.get("id"))
            image = result.pop("_image", None) if isinstance(result, dict) else None
            self._emit({"type": "tool_result", "name": name,
                        "ok": bool(result.get("ok")), "result": result})
            messages.append(tool_message(call.get("id", ""), name, self._compact(result)))
            if image:
                images.append((name, image))
                self._emit({"type": "image", "name": name,
                            "data": image.get("data", ""),
                            "mime": image.get("mime", "image/png")})
        # protocol validity: every tool_call id must have an answer
        for call in calls:
            if call.get("id") not in answered:
                messages.append(tool_message(call.get("id", ""), call.get("name", ""),
                                             {"ok": False, "error": "Cancelled by user"}))
        # images ride in separate user messages, after the whole tool batch
        for name, image in images:
            messages.append(user_message(
                "Image returned by %s (clean board preview):" % name,
                [image_block(image.get("data", ""), image.get("mime", "image/png"))]))

    def _confirm(self, name, args):
        if self.confirm is None:
            return False
        try:
            return bool(self.confirm(name, args))
        except Exception:
            return False

    # ---- context hygiene -----------------------------------------------
    def _compact(self, result):
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > self.max_tool_chars:
            text = text[:self.max_tool_chars] + " ...[truncated]"
        return text

    def _prune_images(self, messages):
        holders = [m for m in messages
                   if isinstance(m.get("content"), list)
                   and any(b.get("type") == "image" for b in m["content"])]
        stale = holders if self.keep_images <= 0 else holders[:-self.keep_images]
        for message in stale:
            blocks = [b for b in message["content"] if b.get("type") != "image"]
            message["content"] = blocks or [text_block("(older preview dropped to save context)")]

    def _trim_history(self):
        while len(self.history) > self.max_history:
            cut = next((i for i in range(1, len(self.history))
                        if self.history[i].get("_user_turn")), None)
            if cut is None:
                break
            del self.history[:cut]

    # ---- plumbing -------------------------------------------------------
    def _stopped(self, final, reason):
        self.last_stop = reason
        if reason == "cancelled":
            self._emit({"type": "cancelled"})
        return final

    def _emit(self, event):
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass                       # a broken UI callback must not kill the loop


# ------------------------------------------------------------------ self-test
class _ScriptedProvider:
    """Offline stand-in: replays a fixed tool-call script, no network needed."""

    supports_images = True

    def __init__(self):
        self.turn = 0
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def stream(self, messages, tools=None, stream=True):
        self._cancel.clear()
        self.turn += 1
        yield {"type": "reasoning", "text": "(scripted thinking - must not reach history)"}
        if self.turn == 1:
            calls = [{"id": "c1", "name": "board_add_frame",
                      "arguments": {"x": 0, "y": 0, "width": 400, "height": 300,
                                    "color": "#ff8800"}},
                     {"id": "c2", "name": "board_add_text",
                      "arguments": {"text": "Agent loop works", "x": 20, "y": 340}}]
        elif self.turn == 2:
            calls = [{"id": "c3", "name": "board_preview",
                      "arguments": {"scope": "board", "max_size": 640}}]
        elif self.turn == 3:
            calls = [{"id": "c4", "name": "board_delete", "arguments": {}}]
        else:
            yield {"type": "text", "text": "Scripted run complete."}
            yield {"type": "done",
                   "message": {"role": "assistant", "content": "Scripted run complete.",
                               "tool_calls": []},
                   "usage": {}, "finish_reason": "stop"}
            return
        yield {"type": "done",
               "message": {"role": "assistant", "content": "", "tool_calls": calls},
               "usage": {}, "finish_reason": "tool_calls"}


def _selftest():
    """python -m refsynapse.ai.agent [--live]  (opens the real window)"""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .board_tools import BoardTools
    from ..main_window import MainWindow

    live = "--live" in sys.argv
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    tools = BoardTools(win)
    provider = make_provider(default_config()) if live else _ScriptedProvider()

    def on_event(event):
        kind = event.get("type")
        if kind == "image":
            print("[event] image from %s: %d base64 chars"
                  % (event.get("name"), len(event.get("data", ""))))
        elif kind == "tool_result":
            print("[event] tool_result %s ok=%s" % (event.get("name"), event.get("ok")))
        elif kind in ("text", "reasoning"):
            print("[event] %s: %s" % (kind, event.get("text", "")[:80]))
        else:
            print("[event]", kind, {k: v for k, v in event.items() if k not in ("type", "data")})

    def confirm(name, args):
        print("[confirm] allowing dangerous tool:", name)
        return True

    agent = Agent(tools, provider, confirm=confirm, on_event=on_event)

    def start():
        print("Agent runs in thread:", threading.current_thread().name)
        prompt = ("Create one orange frame and a text 'live test', arrange them in a "
                  "row and show a preview." if live else
                  "Scripted self-test: build, preview, delete, report.")
        final = agent.run(prompt)
        blob = json.dumps(agent.history, ensure_ascii=False)
        print("FINAL:", final.get("content"), "| stop:", agent.last_stop)
        print("history messages:", len(agent.history),
              "| thinking leaked:", "scripted thinking" in blob,
              "| preview images kept:", blob.count('"type": "image"'))
        print("DONE - close the window to exit")

    QTimer.singleShot(800, lambda: threading.Thread(
        target=start, name="agent-selftest", daemon=True).start())
    sys.exit(app.exec())


if __name__ == "__main__":
    _selftest()