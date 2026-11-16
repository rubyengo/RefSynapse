"""Agent loop for the RefSynapse assistant.

Glue between providers.py (LLM transport) and board_tools.py (board actions):
model -> tool_calls -> execute on the GUI thread -> results -> model ...

Design rules:
  Agent.run() BLOCKS and is meant to be called from a worker thread.
  BoardTools already marshals every board call into the GUI thread.
  on_event / confirm callbacks fire from that worker thread; the chat panel
  must marshal them into the GUI thread itself (queued signal).
  Reasoning ("thinking") is streamed to the UI but NEVER stored in history.
  A tool result may carry "_image"; it is stripped from the tool message and
  re-sent as a separate user message (the OpenAI "tool" role has no images).
  Destructive board tools (board_delete) and untrusted MCP calls require
  confirm(name, arguments) -> bool. Without a confirm callback they are
  refused and the model is told to ask the user in chat first.
  cancel() stops the loop and the in-flight HTTP request (Stop button).
  History stays protocol-valid: every assistant tool_call gets a tool answer,
  even when the user cancels mid-batch; old turns are trimmed only at real
  user-message boundaries; old preview images are dropped from the context.

GUARDS keep a turn from spinning or damaging the board:
  LOOK FIRST: an edit whose result depends on the picture content (crop,
  crop_aspect, crop_subject, fill, draw_stroke) is refused until the model
  has actually SEEN the target in this conversation. board_crop_subject
  needs the FULL ORIGINAL with the grid (board_crop_view) - a preview of
  the current crop would give wrong fractions. A look goes stale as soon
  as that item is changed. Numeric and organisational actions are NOT
  gated - they need no eyes.
  ARGUMENTS are checked against the tool's own schema before it runs, so a
  wrong name ('uid' instead of 'uids') is refused instead of silently
  ignored - that mistake used to rescale EVERY item on the board.
  Enough preview images stay in context (KEEP_IMAGES) for a whole batch of
  per-item work (grid view + verification preview for each item).
  REPEATS: an identical SUCCESSFUL read repeated more than MAX_REPEATS
  times without a board change is refused with "use what you have".
  An identical call that keeps FAILING gets a different answer: the last
  error is quoted back (it contains a ready-to-send corrected call) -
  telling a model to "act now" on invalid arguments only traps it.
  CROP LIMIT: after MAX_CROP_ATTEMPTS adjustments of one item without
  looking at the full original again, further crops are refused with a
  pointer to board_crop_view + board_crop_subject (one computed call
  instead of endless pixel nudging).
  An empty model answer is reported as an error, never as silence.

Self-test (offline, scripted provider, real window):
    python -m refsynapse.ai.agent
Live test against the configured backend:
    python -m refsynapse.ai.agent --live
"""

import json
import sys
import threading

from .mcp_client import NAME_PREFIX
from .providers import (default_config, make_provider, image_block,
                        text_block, tool_message, user_message)

DEFAULT_MAX_ITERATIONS = 30
DEFAULT_MAX_TOOL_CHARS = 6000
DEFAULT_KEEP_IMAGES = 12
DEFAULT_MAX_HISTORY = 80
DEFAULT_MAX_REPEATS = 3
DEFAULT_MAX_CROP_ATTEMPTS = 3

# Edits that depend on WHAT IS INSIDE the picture.
#   "item" - needs board_preview(scope="item", uid=...) OR board_crop_view
#            of that very item;
#   "grid" - needs board_crop_view of that item: the FULL ORIGINAL with the
#            0..1 grid. A preview of the current crop is NOT enough - its
#            fractions would be relative to the crop, not the source.
#   "any"  - needs any preview of the board/selection/item.
# Everything else (numbers, order, locks, files) is not gated.
VISUAL_GATES = {
    "board_crop": "item",
    "board_crop_aspect": "item",
    "board_crop_subject": "grid",
    "board_fill": "any",
    "board_draw_stroke": "any",
}

CROP_TOOLS = ("board_crop", "board_crop_aspect", "board_crop_subject")

SYSTEM_PROMPT = """You are the built-in assistant of RefSynapse, a PureRef-like
reference board. You operate the board exclusively through the provided tools.

Rules:
- Call board_summary or board_list_items first; use the returned uids everywhere.
- Coordinates are scene units on an infinite canvas; sizes are pixels at scale 1.
- LOOK BEFORE YOU EDIT. Never crop, fill or draw on something you have not seen
  in this conversation. After you change an item, your old view of it is
  outdated - look again before the next edit of that item. Numeric actions
  (move, transform, arrange, lock, group) need no preview.
- CROPPING, THE EASY WAY - use it whenever the user says "centre the subject",
  "keep the animal whole", "make them the same size":
  1. board_crop_view(uid) shows the FULL ORIGINAL image with a labelled 0..1
     grid; the current crop is drawn in red. It always shows the original,
     even when the item is already cropped.
  2. Read the subject's bounding box off the grid as fractions
     [x0, y0, x1, y1] (cover the WHOLE subject: ears, paws, tail).
  3. ONE call: board_crop_subject(uid, box, fill, aspect). The tool computes
     the window: the subject stays fully inside, centred, with air around it.
     Use the SAME fill and aspect for EVERY image - that is what "same size"
     means. The answer reports the fill actually achieved.
  Do NOT compute pixel windows yourself and do NOT nudge a crop by small
  offsets. If a crop looks wrong, call board_crop_view again and re-read the
  box; after 3 adjustments without a fresh look the tools will force this.
- Manual crop (board_crop) is for exact rectangles: x/y/w/h in source pixels
  (x,y = TOP-LEFT), or cx/cy fractions 0..1 of the ORIGINAL with optional w/h
  (>1 = pixels, <=1 = fraction of the source). Without w/h the current window
  size is kept and only its centre moves. Fractions are always relative to
  the ORIGINAL, never to the current crop.
- Errors come back as data ({"ok": false, "error": ...}). Read them: they
  contain the exact limits and often a READY-TO-SEND corrected call - copy it
  instead of resending the same arguments. Repeating a failed call unchanged
  will fail again.
- Locked items are skipped by move/transform; mention skipped_locked when present.
- Batch independent tool calls in one turn; prefer few, well-chosen calls.
- Use the EXACT parameter names from each tool schema. 'uid' is one item
  (string), 'uids' is a list of items - they are NOT interchangeable. Never
  invent a parameter name: a wrong name is rejected, and guessing wastes turns.
- For per-item numeric work (scale, rotation) send one call per item with
  uids: ["<uid>"].
- Preview images stay in your context and are labelled with their item uid.
  Never request the same view twice without changing the item in between.
  When several items need visual work, request their views in ONE batch, then
  act on all of them.
- board_delete is destructive: never call it unless the user explicitly asked
  for that deletion in the chat. If unsure, ask instead of deleting.
- External MCP tools (mcp__) act outside the board; a refusal means the user
  declined the confirmation dialog - ask in chat before trying again.
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
                 max_repeats=DEFAULT_MAX_REPEATS,
                 max_crop_attempts=DEFAULT_MAX_CROP_ATTEMPTS,
                 system_prompt=None):
        self.tools = tools
        self.provider = provider
        self.confirm = confirm
        self.on_event = on_event
        self.max_iterations = int(max_iterations)
        self.max_tool_chars = int(max_tool_chars)
        self.keep_images = int(keep_images)
        self.max_history = int(max_history)
        self.max_repeats = int(max_repeats)
        self.max_crop_attempts = int(max_crop_attempts)
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.history = []                  # everything except the system message
        self.last_stop = "stop"
        self._cancel = threading.Event()
        self._repeats = {}                 # call signature -> times seen this turn
        self._failed = {}                  # call signature -> last error text
        self._crop_attempts = {}           # uid -> crop edits since the last full look
        self._schema_cache = None          # tool name -> parameters schema
        self._seen_items = set()           # uids seen with scope="item" or crop_view
        self._grid_seen = set()            # uids seen with board_crop_view (original+grid)
        self._seen_any = False             # any board/selection/item preview

    # ---- public API ----------------------------------------------------
    def run(self, text, images=()):
        """Blocking agent turn. Returns the final assistant message dict."""
        self._cancel.clear()
        self.last_stop = "stop"
        self._repeats = {}
        self._failed = {}
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
                    if not str(assistant.get("content") or "").strip():
                        self._emit({"type": "error",
                                    "message": "The model returned an empty answer. "
                                               "Raise 'Max tokens' in AI Assistant "
                                               "settings or set 'Reasoning effort' "
                                               "to off."})
                        return self._stopped(final, "empty")
                    self._emit({"type": "done", "message": assistant})
                    return assistant
                self._run_calls(messages, calls)
                self._prune_images(messages)
            self._emit({"type": "error",
                        "message": "Iteration limit (%d) reached; stopping. "
                                   "Ask me to continue, or split the task into "
                                   "smaller steps." % self.max_iterations})
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
        self._repeats = {}
        self._failed = {}
        self._crop_attempts = {}
        self._schema_cache = None
        self._seen_items.clear()
        self._grid_seen.clear()
        self._seen_any = False

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
            result = self._arg_error(name, args)
            if result is None:
                result = self._vision_error(name, args)
            if result is None:
                result = self._crop_limit_error(name, args)
            invalid_refusal = result is not None   # args / vision / crop limit
            if result is None:
                result = self._repeat_error(name, args)
            repeat_refusal = (not invalid_refusal) and result is not None
            if result is not None:
                pass                                   # refused before execution
            elif self.tools.is_dangerous(name) and not self._confirm(name, args):
                if name.startswith(NAME_PREFIX):
                    result = {"ok": False,
                              "error": "Refused: the user declined this external MCP call "
                                       "in the confirmation dialog. Do not retry without "
                                       "asking the user in chat first."}
                else:
                    result = {"ok": False,
                              "error": "Refused: destructive board action needs explicit "
                                       "user confirmation. Ask the user in chat first."}
            elif isinstance(args, dict) and args.get("__error__") and "__raw__" in args:
                result = self.tools.call(name, {"__error__": args["__error__"],
                                                "__raw__": args["__raw__"]})
            else:
                result = self.tools.call(name, args)
            answered.add(call.get("id"))
            image = result.pop("_image", None) if isinstance(result, dict) else None
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            self._emit({"type": "tool_result", "name": name, "ok": ok, "result": result})
            messages.append(tool_message(call.get("id", ""), name, self._compact(result)))
            signature = self._signature(name, args)
            if ok:
                self._failed.pop(signature, None)
                self._note_preview(name, args)
                if self._is_mutating(name):
                    self._repeats.clear()   # board changed: looking again is legitimate
                    self._failed.clear()
                    self._note_change(name, args)
                    self._note_crop(name, args)
            elif not repeat_refusal:
                # a genuinely failed call: remember WHY, so the repeat guard
                # can quote the error instead of saying "act now"
                self._failed[signature] = (str(result.get("error", ""))
                                           if isinstance(result, dict) else "")
            if image:
                images.append((name, image, args))
                self._emit({"type": "image", "name": name,
                            "data": image.get("data", ""),
                            "mime": image.get("mime", "image/png")})
        # protocol validity: every tool_call id must have an answer
        for call in calls:
            if call.get("id") not in answered:
                messages.append(tool_message(call.get("id", ""), call.get("name", ""),
                                             {"ok": False, "error": "Cancelled by user"}))
        # images ride in separate user messages, after the whole tool batch
        total = len(images)
        for index, (name, image, args) in enumerate(images, 1):
            label = name
            uid = (args or {}).get("uid") if isinstance(args, dict) else None
            if uid:
                label += " for item uid=%s" % uid
            if name == "board_crop_view":
                hint = ("This is the FULL ORIGINAL with a 0..1 grid; the red "
                        "rectangle is the current crop. Read the subject's "
                        "bounding box as fractions [x0, y0, x1, y1] off the "
                        "grid and call board_crop_subject with box/fill/aspect "
                        "- do not compute pixels yourself:")
            else:
                hint = ("Note subject positions as fractions of width/height "
                        "before computing pixels:")
            messages.append(user_message(
                "Image %d/%d returned by %s (clean render, still in your context - "
                "do not request it again unless you change the item). %s"
                % (index, total, label, hint),
                [image_block(image.get("data", ""), image.get("mime", "image/png"))]))

    def _confirm(self, name, args):
        if self.confirm is None:
            return False
        try:
            return bool(self.confirm(name, args))
        except Exception:
            return False

    # ---- look-first gate -----------------------------------------------
    def _note_preview(self, name, args):
        """Remember what the model has actually seen."""
        if not isinstance(args, dict):
            return
        if name == "board_preview":
            self._seen_any = True
            if str(args.get("scope", "board")) == "item" and args.get("uid"):
                self._seen_items.add(str(args["uid"]))
        elif name == "board_crop_view":
            uid = str(args.get("uid") or "")
            if uid:
                self._seen_any = True
                self._seen_items.add(uid)
                self._grid_seen.add(uid)
                self._crop_attempts.pop(uid, None)   # fresh look at the original

    def _note_change(self, name, args):
        """A changed item must be looked at again before the next visual edit."""
        args = args if isinstance(args, dict) else {}
        touched = []
        if args.get("uid"):
            touched.append(str(args["uid"]))
        for uid in args.get("uids") or []:
            touched.append(str(uid))
        if touched:
            for uid in touched:
                self._seen_items.discard(uid)
                self._grid_seen.discard(uid)
        else:
            # board-wide change (undo, redo, arrange/delete without uids):
            # nothing can be trusted any more
            self._seen_items.clear()
            self._grid_seen.clear()
            self._seen_any = False

    def _note_crop(self, name, args):
        """Count crop adjustments per item; a reset starts over."""
        if not isinstance(args, dict):
            return
        uid = str(args.get("uid") or "")
        if not uid:
            return
        if name in CROP_TOOLS:
            self._crop_attempts[uid] = self._crop_attempts.get(uid, 0) + 1
        elif name == "board_crop_reset":
            self._crop_attempts.pop(uid, None)

    def _vision_error(self, name, args):
        """Refuse a picture-dependent edit made without looking at the target."""
        gate = VISUAL_GATES.get(name)
        if gate is None or not isinstance(args, dict):
            return None
        if gate == "grid":
            uid = str(args.get("uid") or "")
            if not uid or uid in self._grid_seen:
                return None
            return {"ok": False,
                    "error": "Look first: board_crop_subject needs the FULL "
                             "ORIGINAL with the coordinate grid - a preview of "
                             "the current crop is not enough (its fractions "
                             "would be wrong). Call board_crop_view with "
                             "{\"uid\": \"%s\", \"max_size\": 900}, read the "
                             "subject's bounding box off the 0..1 grid (cover "
                             "the whole subject), then call board_crop_subject "
                             "with that box, fill and aspect. Nothing was "
                             "changed." % uid}
        if gate == "item":
            uid = str(args.get("uid") or "")
            if not uid or uid in self._seen_items:
                return None
            return {"ok": False,
                    "error": "Look first: you have not seen item %s since it last "
                             "changed, so you cannot know where the subject is. "
                             "Best for cropping: board_crop_view with {\"uid\": "
                             "\"%s\"} (full original + 0..1 grid), then one "
                             "board_crop_subject call. Or call board_preview with "
                             "{\"scope\": \"item\", \"uid\": \"%s\", \"max_size\": "
                             "800} and only then call %s again. Nothing was "
                             "changed." % (uid, uid, uid, name)}
        if self._seen_any:
            return None
        return {"ok": False,
                "error": "Look first: call board_preview (scope=\"board\" or "
                         "\"selection\") to see the canvas before %s - coordinates "
                         "chosen blind will land in the wrong place. "
                         "Nothing was changed." % name}

    def _crop_limit_error(self, name, args):
        """Stop endless pixel nudging: force a fresh look at the original."""
        if name not in CROP_TOOLS or not isinstance(args, dict):
            return None
        uid = str(args.get("uid") or "")
        if not uid or self._crop_attempts.get(uid, 0) < self.max_crop_attempts:
            return None
        return {"ok": False,
                "error": "Crop limit: you have adjusted the crop of item %s %d "
                         "times without looking at the full original again. "
                         "Stop nudging pixels. Call board_crop_view with "
                         "{\"uid\": \"%s\"} - it shows the FULL original with "
                         "the 0..1 grid and the current crop in red - re-read "
                         "the subject's bounding box, then make ONE "
                         "board_crop_subject call with that box, fill and "
                         "aspect. Nothing was changed."
                         % (uid, self._crop_attempts.get(uid, 0), uid)}

    # ---- argument check ------------------------------------------------
    def _schemas(self):
        """tool name -> parameters schema (built once, tools never change)."""
        if self._schema_cache is None:
            cache = {}
            try:
                for tool in self.tools.schemas() or []:
                    if isinstance(tool, dict) and tool.get("name"):
                        cache[tool["name"]] = tool.get("parameters") or {}
            except Exception:
                cache = {}
            self._schema_cache = cache
        return self._schema_cache

    def _arg_error(self, name, args):
        """Refuse unknown / missing parameters BEFORE the tool touches the board.

        A silently ignored parameter is dangerous: board_transform with 'uid'
        instead of 'uids' used to rescale every item on the board.
        """
        schema = self._schemas().get(name)
        if not isinstance(schema, dict) or not isinstance(args, dict):
            return None
        if args.get("__error__"):
            return None                      # broken-JSON path, handled elsewhere
        props = schema.get("properties")
        if not isinstance(props, dict):
            return None
        problems = []
        if schema.get("additionalProperties") is not True:
            unknown = [k for k in args if k not in props]
            if unknown:
                hints = []
                for key in unknown:
                    if key + "s" in props:
                        hints.append("Use '%s': [\"%s\"] instead of '%s'."
                                     % (key + "s", args.get(key), key))
                    elif key.endswith("s") and key[:-1] in props:
                        hints.append("Use '%s' (a single value) instead of '%s'."
                                     % (key[:-1], key))
                problems.append("unknown parameter(s): %s. Valid names: %s.%s"
                                % (", ".join(sorted(unknown)),
                                   ", ".join(sorted(props)) or "(none)",
                                   (" " + " ".join(hints)) if hints else ""))
        missing = [k for k in (schema.get("required") or []) if k not in args]
        if missing:
            problems.append("missing required parameter(s): %s."
                            % ", ".join(sorted(missing)))
        if not problems:
            return None
        return {"ok": False,
                "error": "Invalid arguments for %s: %s Nothing was changed - fix the "
                         "argument names and call it again."
                         % (name, " ".join(problems))}

    # ---- repeat guard --------------------------------------------------
    def _signature(self, name, args):
        try:
            return name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return name + "|" + str(args)

    def _repeat_error(self, name, args):
        """Refuse an identical call repeated too often without a board change.

        Two different situations, two different answers:
          * the call keeps SUCCEEDING (a read/preview): the data is already
            there - tell the model to use it and act;
          * the call keeps FAILING: "act now" is a trap, the arguments are
            invalid. Quote the last error (it carries a ready-to-send fix).
        """
        signature = self._signature(name, args)
        seen = self._repeats.get(signature, 0) + 1
        self._repeats[signature] = seen
        if seen <= self.max_repeats:
            return None
        last_error = self._failed.get(signature)
        if last_error:
            snippet = last_error[:400]
            return {"ok": False,
                    "error": "Invalid-call guard: this exact call already failed "
                             "%d times with the same arguments - sending it again "
                             "will fail again. Last error: %s | Follow the "
                             "ready-to-send corrected call inside that error, or "
                             "for cropping use board_crop_view + "
                             "board_crop_subject. Do not resend these arguments. "
                             "Nothing was changed." % (seen - 1, snippet)}
        return {"ok": False,
                "error": "Repeat guard: you already called %s with these exact "
                         "arguments %d times and the board has not changed since. "
                         "The images and numbers you received are still valid - use "
                         "them and act now (e.g. board_crop / board_transform), or "
                         "tell the user what is unclear. Do not request it again."
                         % (name, seen - 1)}

    def _is_mutating(self, name):
        try:
            return bool(self.tools.mutates(name))
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


class _LoopProvider:
    """Offline stand-in: asks for the SAME preview over and over (the old bug)."""
    supports_images = True

    def __init__(self, turns=5):
        self.turn = 0
        self.turns = turns

    def cancel(self):
        pass

    def stream(self, messages, tools=None, stream=True):
        self.turn += 1
        if self.turn > self.turns:
            yield {"type": "done",
                   "message": {"role": "assistant", "content": "Stopped looking.",
                               "tool_calls": []},
                   "usage": {}, "finish_reason": "stop"}
            return
        yield {"type": "done",
               "message": {"role": "assistant", "content": "",
                           "tool_calls": [{"id": "p%d" % self.turn, "name": "board_preview",
                                           "arguments": {"scope": "board", "max_size": 700}}]},
               "usage": {}, "finish_reason": "tool_calls"}


class _BadArgsProvider:
    """Offline stand-in: the real 'uid instead of uids' mistake, then a fix."""
    supports_images = True

    def __init__(self):
        self.turn = 0

    def cancel(self):
        pass

    def stream(self, messages, tools=None, stream=True):
        self.turn += 1
        if self.turn == 1:
            calls = [{"id": "b1", "name": "board_transform",
                      "arguments": {"uid": "deadbeef", "scale": 2.0}}]
        elif self.turn == 2:
            calls = [{"id": "b2", "name": "board_transform",
                      "arguments": {"uids": ["deadbeef"], "scale": 2.0}}]
        else:
            yield {"type": "done",
                   "message": {"role": "assistant", "content": "Fixed the arguments.",
                               "tool_calls": []},
                   "usage": {}, "finish_reason": "stop"}
            return
        yield {"type": "done",
               "message": {"role": "assistant", "content": "", "tool_calls": calls},
               "usage": {}, "finish_reason": "tool_calls"}


class _BlindCropProvider:
    """Offline stand-in: the real failure - crop_info then crop, without looking."""
    supports_images = True

    def __init__(self, uid="deadbeef"):
        self.turn = 0
        self.uid = uid

    def cancel(self):
        pass

    def stream(self, messages, tools=None, stream=True):
        self.turn += 1
        crop = {"id": "k%d" % self.turn, "name": "board_crop",
                "arguments": {"uid": self.uid, "x": 10, "y": 10, "w": 100, "h": 100}}
        if self.turn == 1:
            calls = [{"id": "i1", "name": "board_crop_info",
                      "arguments": {"uid": self.uid}}, crop]
        elif self.turn == 2:
            calls = [{"id": "v1", "name": "board_preview",
                      "arguments": {"scope": "item", "uid": self.uid, "max_size": 800}}]
        elif self.turn == 3:
            calls = [crop]
        else:
            yield {"type": "done",
                   "message": {"role": "assistant", "content": "Looked, then cropped.",
                               "tool_calls": []},
                   "usage": {}, "finish_reason": "stop"}
            return
        yield {"type": "done",
               "message": {"role": "assistant", "content": "", "tool_calls": calls},
               "usage": {}, "finish_reason": "tool_calls"}


class _SubjectProvider:
    """Offline stand-in: the NEW workflow - subject blind, then view, then subject."""
    supports_images = True

    def __init__(self, uid="deadbeef"):
        self.turn = 0
        self.uid = uid

    def cancel(self):
        pass

    def stream(self, messages, tools=None, stream=True):
        self.turn += 1
        subject = {"id": "s%d" % self.turn, "name": "board_crop_subject",
                   "arguments": {"uid": self.uid, "box": [0.2, 0.2, 0.8, 0.8],
                                 "fill": 0.6, "aspect": "3:2"}}
        if self.turn == 1:
            calls = [subject]                       # blind: must be refused
        elif self.turn == 2:
            calls = [{"id": "g1", "name": "board_crop_view",
                      "arguments": {"uid": self.uid, "max_size": 700}}]
        elif self.turn == 3:
            calls = [subject]                       # after the grid: must run
        else:
            yield {"type": "done",
                   "message": {"role": "assistant",
                               "content": "Viewed the grid, cropped by subject.",
                               "tool_calls": []},
                   "usage": {}, "finish_reason": "stop"}
            return
        yield {"type": "done",
               "message": {"role": "assistant", "content": "", "tool_calls": calls},
               "usage": {}, "finish_reason": "tool_calls"}


class _EmptyProvider:
    """Offline stand-in: the 'thinking ate the budget' answer (must not be silent)."""
    supports_images = True

    def cancel(self):
        pass

    def stream(self, messages, tools=None, stream=True):
        yield {"type": "done",
               "message": {"role": "assistant", "content": "", "tool_calls": []},
               "usage": {}, "finish_reason": "stop"}


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
        print("defaults -> iterations:", DEFAULT_MAX_ITERATIONS,
              "| keep_images:", DEFAULT_KEEP_IMAGES,
              "| max_repeats:", DEFAULT_MAX_REPEATS,
              "| max_crop_attempts:", DEFAULT_MAX_CROP_ATTEMPTS)
        # --- offline: LOOK FIRST gate ------------------------------------
        eye = Agent(tools, _ScriptedProvider())
        crop_args = {"uid": "abc123", "x": 0, "y": 0, "w": 100, "h": 100}
        blind = eye._vision_error("board_crop", crop_args)
        print("blind crop refused ->", blind is not None, "(expected True)")
        print("refusal says preview ->", "board_preview" in (blind or {}).get("error", ""),
              "(expected True)")
        eye._note_preview("board_preview", {"scope": "board", "max_size": 1200})
        print("board view not enough ->",
              eye._vision_error("board_crop", crop_args) is not None, "(expected True)")
        print("but enough for fill ->",
              eye._vision_error("board_fill", {"x": 1, "y": 2}) is None, "(expected True)")
        eye._note_preview("board_preview", {"scope": "item", "uid": "abc123"})
        print("after item preview ->",
              eye._vision_error("board_crop", crop_args) is None, "(expected True)")
        eye._note_change("board_crop", crop_args)
        print("stale after change ->",
              eye._vision_error("board_crop", crop_args) is not None, "(expected True)")
        eye._note_preview("board_preview", {"scope": "item", "uid": "abc123"})
        eye._note_preview("board_preview", {"scope": "item", "uid": "other456"})
        eye._note_change("board_crop", crop_args)
        print("neighbour still seen ->",
              eye._vision_error("board_crop", {"uid": "other456", "x": 0, "y": 0,
                                               "w": 10, "h": 10}) is None,
              "(expected True)")
        eye._note_change("board_undo", {})
        print("undo forgets all ->",
              eye._vision_error("board_crop", {"uid": "other456", "x": 0, "y": 0,
                                               "w": 10, "h": 10}) is not None,
              "(expected True)")
        print("move not gated ->",
              eye._vision_error("board_move", {"uids": ["abc123"], "dx": 5}) is None,
              "(expected True)")
        print("transform not gated ->",
              eye._vision_error("board_transform", {"uids": ["abc123"], "scale": 2}) is None,
              "(expected True)")
        # --- offline: the GRID gate for board_crop_subject ----------------
        grid = Agent(tools, _ScriptedProvider())
        subj_args = {"uid": "abc123", "box": [0.2, 0.2, 0.8, 0.8], "fill": 0.6}
        blind_subj = grid._vision_error("board_crop_subject", subj_args)
        print("subject blind refused ->", blind_subj is not None, "(expected True)")
        print("refusal says crop_view ->",
              "board_crop_view" in (blind_subj or {}).get("error", ""), "(expected True)")
        grid._note_preview("board_preview", {"scope": "item", "uid": "abc123"})
        print("preview not enough for subject ->",
              grid._vision_error("board_crop_subject", subj_args) is not None,
              "(expected True)")
        print("but enough for manual crop ->",
              grid._vision_error("board_crop", crop_args) is None, "(expected True)")
        grid._note_preview("board_crop_view", {"uid": "abc123"})
        print("after crop_view subject allowed ->",
              grid._vision_error("board_crop_subject", subj_args) is None,
              "(expected True)")
        print("crop_view counts as look ->",
              grid._vision_error("board_crop", crop_args) is None, "(expected True)")
        grid._note_change("board_crop_subject", subj_args)
        print("subject stale after change ->",
              grid._vision_error("board_crop_subject", subj_args) is not None,
              "(expected True)")
        # --- offline: the crop attempt limit -------------------------------
        limiter = Agent(tools, _ScriptedProvider())
        limiter._crop_attempts["abc123"] = DEFAULT_MAX_CROP_ATTEMPTS
        blocked_crop = limiter._crop_limit_error("board_crop", {"uid": "abc123", "cx": 0.5})
        print("crop limit refused ->", blocked_crop is not None, "(expected True)")
        print("limit points to view ->",
              "board_crop_view" in (blocked_crop or {}).get("error", ""), "(expected True)")
        limiter._note_preview("board_crop_view", {"uid": "abc123"})
        print("crop_view resets limit ->",
              limiter._crop_limit_error("board_crop", {"uid": "abc123", "cx": 0.5}) is None,
              "(expected True)")
        limiter._crop_attempts["abc123"] = DEFAULT_MAX_CROP_ATTEMPTS
        limiter._note_crop("board_crop_reset", {"uid": "abc123"})
        print("crop_reset resets limit ->",
              limiter._crop_limit_error("board_crop", {"uid": "abc123", "cx": 0.5}) is None,
              "(expected True)")
        print("move not crop-limited ->",
              limiter._crop_limit_error("board_move", {"uid": "abc123"}) is None,
              "(expected True)")
        # --- offline: the blind crop must never reach the board ----------
        # a real image item, so crop_info / preview / crop can all succeed
        import base64
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QColor, QImage
        probe_image = QImage(400, 300, QImage.Format.Format_RGB32)
        probe_image.fill(QColor("#3a6ea5"))
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        probe_image.save(buf, "PNG")
        buf.close()
        added = tools.call("board_add_image",
                           {"image_base64": base64.b64encode(bytes(ba)).decode(),
                            "x": 2000, "y": 2000})
        probe_uid = (added.get("item") or {}).get("uid", "deadbeef")
        print("probe image added ->", added.get("ok"), "(expected True)")
        touched = []
        real_call = tools.call

        def watched(name, args):
            touched.append(name)
            return real_call(name, args)

        tools.call = watched
        try:
            blind_run = Agent(tools, _BlindCropProvider(probe_uid))
            blind_run.run("Crop the hedgehog, please.")
        finally:
            tools.call = real_call
        print("blind crop never executed ->", touched.count("board_crop"),
              "(expected 1)")
        # --- offline: the SUBJECT workflow end to end ---------------------
        touched = []
        tools.call = watched
        try:
            subj_run = Agent(tools, _SubjectProvider(probe_uid))
            subj_run.run("Centre the hedgehog, keep it whole.")
        finally:
            tools.call = real_call
        print("subject executed once ->", touched.count("board_crop_subject"),
              "(expected 1)")
        print("grid view executed once ->", touched.count("board_crop_view"),
              "(expected 1)")
        # --- offline: argument check against the real schemas ------------
        guard = Agent(tools, _ScriptedProvider())
        wrong = guard._arg_error("board_transform", {"uid": "abc123", "scale": 2})
        print("uid rejected ->", wrong is not None, "(expected True)")
        print("uid hint ->", "uids" in (wrong or {}).get("error", ""), "(expected True)")
        print("uids accepted ->",
              guard._arg_error("board_transform", {"uids": ["abc123"], "scale": 2}) is None,
              "(expected True)")
        print("preview accepted ->",
              guard._arg_error("board_preview", {"scope": "item", "uid": "abc123",
                                                 "max_size": 500}) is None,
              "(expected True)")
        print("invented param ->",
              guard._arg_error("board_preview", {"scope": "board", "zoom": 3}) is not None,
              "(expected True)")
        print("unknown tool skipped ->",
              guard._arg_error("no_such_tool", {"whatever": 1}) is None, "(expected True)")
        print("subject args accepted ->",
              guard._arg_error("board_crop_subject",
                               {"uid": "abc123", "box": [0.1, 0.1, 0.9, 0.9],
                                "fill": 0.6, "aspect": "3:2"}) is None,
              "(expected True)")
        print("crop_view args accepted ->",
              guard._arg_error("board_crop_view", {"uid": "abc123", "max_size": 900}) is None,
              "(expected True)")
        # --- offline: the wrong name must never reach the board ----------
        touched = []
        tools.call = watched
        try:
            fixer = Agent(tools, _BadArgsProvider())
            fixer.run("Scale one item, please.")
        finally:
            tools.call = real_call
        print("bad args never executed ->", touched.count("board_transform"),
              "(expected 1)")
        # --- offline: repeat guard (successful reads) ---------------------
        args = {"scope": "item", "uid": "abc123", "max_size": 900}
        allowed = [guard._repeat_error("board_preview", args) is None
                   for _ in range(DEFAULT_MAX_REPEATS)]
        blocked = guard._repeat_error("board_preview", args)
        print("repeat allowed ->", all(allowed), "(expected True)")
        print("repeat blocked ->", blocked is not None, "(expected True)")
        print("guard mentions crop ->", "board_crop" in (blocked or {}).get("error", ""),
              "(expected True)")
        guard._repeats.clear()          # what a successful mutation does
        guard._failed.clear()
        print("reset after change ->", guard._repeat_error("board_preview", args) is None,
              "(expected True)")
        print("mutating detected ->", guard._is_mutating("board_fill"),
              "(expected True)")
        print("read-only detected ->", not guard._is_mutating("board_preview"),
              "(expected True)")
        # --- offline: repeat guard (FAILING calls get a different answer) --
        bad_args = {"uid": "abc123", "cx": 0.5, "cy": 0.5, "w": 900, "h": 900}
        sig = guard._signature("board_crop", bad_args)
        guard._failed[sig] = ("w=900 is wider than the source (800). Ready-to-send "
                              "fix: board_crop {\"uid\": \"abc123\", \"cx\": 0.5}")
        for _ in range(DEFAULT_MAX_REPEATS):
            guard._repeat_error("board_crop", bad_args)
        blocked_bad = guard._repeat_error("board_crop", bad_args)
        print("invalid-call guard ->",
              blocked_bad is not None and "Invalid-call guard" in blocked_bad["error"],
              "(expected True)")
        print("guard quotes the fix ->",
              "Ready-to-send" in (blocked_bad or {}).get("error", ""), "(expected True)")
        print("no act-now trap ->",
              "still valid" not in (blocked_bad or {}).get("error", ""), "(expected True)")
        guard._repeats.clear()
        guard._failed.clear()
        # --- offline: twelve previews must all survive in context ----------
        keep = Agent(tools, _ScriptedProvider())
        fake = [{"role": "user",
                 "content": [text_block("preview %d" % i), image_block("AAAA")]}
                for i in range(10)]
        keep._prune_images(fake)
        alive = sum(1 for m in fake
                    if any(b.get("type") == "image" for b in m["content"]))
        print("ten previews kept ->", alive, "(expected 10)")
        # --- offline: the loop scenario now ends with a refusal ----------
        refused = []

        def loop_event(event):
            if event.get("type") != "tool_result":
                return
            error = str((event.get("result") or {}).get("error", ""))
            if "Repeat guard" in error:
                refused.append(error)

        looper = Agent(tools, _LoopProvider(), on_event=loop_event, max_iterations=8)
        looper.run("Look at the board again and again.")
        print("loop refusals ->", len(refused), "(expected 2)")
        print("loop stop reason ->", looper.last_stop, "(expected stop)")
        # --- offline: an empty answer is an error, not silence -----------
        errors = []
        silent = Agent(tools, _EmptyProvider(),
                       on_event=lambda e: errors.append(e) if e.get("type") == "error" else None)
        silent.run("Say something.")
        print("empty reported ->", bool(errors), "(expected True)")
        print("empty stop reason ->", silent.last_stop, "(expected empty)")
        # --- the original scripted run ----------------------------------
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