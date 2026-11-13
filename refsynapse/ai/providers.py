"""LLM providers for the RefSynapse agent.

Standard library only (urllib + json): the portable embeddable Python must not
need extra packages. Two wire formats are supported:

* OpenAI-compatible  - LM Studio, llama.cpp server, Ollama, OpenAI, OpenRouter
* Anthropic Messages - Claude

Both are normalised to ONE message format and ONE stream-event format, so the
agent loop never has to care which backend is running.

Normalised message
------------------
{"role": "system"|"user"|"assistant"|"tool",
 "content": str | [ {"type": "text",  "text": str},
                    {"type": "image", "data": <base64>, "mime": "image/png"} ],
 "tool_calls":   [{"id": str, "name": str, "arguments": dict}],   # assistant
 "tool_call_id": str,                                            # role="tool"
 "name": str}                                                    # role="tool"

Stream events
-------------
{"type": "text",      "text": str}          visible answer delta
{"type": "reasoning", "text": str}          thinking delta (shown collapsed)
{"type": "done",      "message": dict, "usage": dict, "finish_reason": str}
{"type": "error",     "message": str}

Run this file directly to test the backend without any UI:
    python refsynapse\\ai\\providers.py
"""
import base64
import json
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# Reasoning effort -> Anthropic thinking budget (tokens).
THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


class ProviderError(Exception):
    """Backend refused the request or answered with something unusable."""


class Cancelled(Exception):
    """The user pressed Stop while the answer was streaming."""


# --------------------------------------------------------------------- helpers
def text_block(text):
    return {"type": "text", "text": text}


def image_block(data, mime="image/png"):
    """Normalised image block from raw bytes or an existing base64 string."""
    if isinstance(data, (bytes, bytearray)):
        data = base64.b64encode(bytes(data)).decode("ascii")
    return {"type": "image", "data": data, "mime": mime}


def user_message(text, images=()):
    if not images:
        return {"role": "user", "content": text}
    content = [text_block(text)] if text else []
    content.extend(images)
    return {"role": "user", "content": content}


def tool_message(call_id, name, result):
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": result}


def _load_args(raw):
    """Tool arguments arrive as a JSON string; keep the raw text if it is broken."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except ValueError:
        return {"__error__": "Model produced invalid JSON arguments", "__raw__": raw}


class _ToolBuffer:
    """Accumulates streamed tool-call fragments by their choice index."""

    def __init__(self):
        self.slots = {}

    def add(self, calls):
        for call in calls or []:
            index = call.get("index", 0)
            slot = self.slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name") and not slot["name"]:
                slot["name"] = function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]

    def result(self):
        out = []
        for index in sorted(self.slots):
            slot = self.slots[index]
            if not slot["name"]:
                continue
            out.append({"id": slot["id"] or "call_%d" % index,
                        "name": slot["name"],
                        "arguments": _load_args(slot["arguments"])})
        return out


# ------------------------------------------------------------------------ base
class Provider:
    """Common configuration, cancellation and HTTP plumbing."""

    kind = "base"
    supports_images = True

    def __init__(self, model=DEFAULT_MODEL, base_url=None, api_key="",
                 temperature=0.7, max_tokens=2048, reasoning_effort="low",
                 timeout=300, extra=None):
        self.model = model
        self.base_url = (base_url or self.default_base_url()).rstrip("/")
        self.api_key = api_key or ""
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.reasoning_effort = reasoning_effort or "off"
        self.timeout = float(timeout)
        self.extra = dict(extra or {})
        self._cancel = threading.Event()
        self._resp = None
        self._lock = threading.Lock()

    @staticmethod
    def default_base_url():
        return DEFAULT_BASE_URL

    # ---- lifecycle -----------------------------------------------------
    def cancel(self):
        """Abort the running request from another thread (Stop button)."""
        self._cancel.set()
        with self._lock:
            resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _begin(self):
        self._cancel.clear()
        with self._lock:
            self._resp = None

    def _finish(self):
        with self._lock:
            resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    # ---- errors --------------------------------------------------------
    def _http_error(self, error):
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        detail = body.strip()
        try:
            data = json.loads(body)
            node = data.get("error", data)
            detail = node.get("message", detail) if isinstance(node, dict) else str(node)
        except ValueError:
            pass
        detail = (detail or "")[:600]
        if error.code in (401, 403):
            return "Authorisation failed (HTTP %d). Check the API key. %s" % (error.code, detail)
        if error.code == 404:
            return ("Endpoint or model not found (HTTP 404). Check the base URL and "
                    "the model id. %s" % detail)
        if error.code == 400:
            return ("The server rejected the request (HTTP 400). The model may not "
                    "support tools or images. %s" % detail)
        return "HTTP %d: %s" % (error.code, detail)

    def _connection_error(self, error):
        reason = getattr(error, "reason", error)
        local = "127.0.0.1" in self.base_url or "localhost" in self.base_url
        hint = (" Start LM Studio and switch the server to Running."
                if local else " Check the network and the base URL.")
        return "Cannot reach %s (%s).%s" % (self.base_url, reason, hint)

    # ---- HTTP ----------------------------------------------------------
    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _post(self, path, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers=headers or self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as ex:
            raise ProviderError(self._http_error(ex)) from None
        except urllib.error.URLError as ex:
            raise ProviderError(self._connection_error(ex)) from None
        except (TimeoutError, OSError) as ex:
            raise ProviderError("Connection failed: %s" % ex) from None
        with self._lock:
            self._resp = resp
        return resp

    def _get_json(self, path, headers=None):
        request = urllib.request.Request(self.base_url + path,
                                         headers=headers or self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 20)) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as ex:
            raise ProviderError(self._http_error(ex)) from None
        except urllib.error.URLError as ex:
            raise ProviderError(self._connection_error(ex)) from None
        except (TimeoutError, OSError, ValueError) as ex:
            raise ProviderError("Connection failed: %s" % ex) from None

    def _sse(self, resp):
        """Yield parsed SSE data objects; stop on [DONE] or cancellation."""
        try:
            for raw in resp:
                if self._cancel.is_set():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except ValueError:
                    continue
        except Cancelled:
            raise
        except Exception:
            # cancel() closes the socket from another thread; that surfaces here.
            if self._cancel.is_set():
                raise Cancelled() from None
            raise

    # ---- public API ----------------------------------------------------
    def list_models(self):
        raise NotImplementedError

    def stream(self, messages, tools=None, stream=True):
        raise NotImplementedError

    def complete(self, messages, tools=None):
        """Non-streaming convenience: return the final assistant message."""
        final = None
        for event in self.stream(messages, tools, stream=False):
            if event["type"] == "done":
                final = event["message"]
            elif event["type"] == "error":
                raise ProviderError(event["message"])
        return final or {"role": "assistant", "content": ""}

    def probe(self):
        """Connection test used by the settings dialog."""
        try:
            models = self.list_models()
        except ProviderError as ex:
            return {"ok": False, "message": str(ex), "models": []}
        if not models:
            return {"ok": False, "models": [],
                    "message": "Connected, but the server reports no models."}
        if self.model not in models:
            return {"ok": True, "models": models,
                    "message": "Connected. Warning: '%s' is not in the model list." % self.model}
        return {"ok": True, "models": models,
                "message": "Connected. %d model(s), '%s' available." % (len(models), self.model)}


# ------------------------------------------------------------- OpenAI compatible
class OpenAIProvider(Provider):
    """LM Studio / llama.cpp / Ollama / OpenAI / OpenRouter."""

    kind = "openai"

    def list_models(self):
        data = self._get_json("/models")
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    # ---- normalised -> wire -------------------------------------------
    def _wire_content(self, content):
        if isinstance(content, str):
            return content
        parts = []
        for block in content or []:
            if block.get("type") == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                mime = block.get("mime", "image/png")
                parts.append({"type": "image_url", "image_url": {
                    "url": "data:%s;base64,%s" % (mime, block.get("data", ""))}})
        return parts

    def _wire_messages(self, messages):
        out = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                out.append({"role": "tool",
                            "tool_call_id": message.get("tool_call_id", ""),
                            "content": message.get("content", "")})
                continue
            if role == "assistant" and message.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": [{"id": call["id"], "type": "function",
                                    "function": {"name": call["name"],
                                                 "arguments": json.dumps(
                                                     call.get("arguments", {}),
                                                     ensure_ascii=False)}}
                                   for call in message["tool_calls"]]})
                continue
            out.append({"role": role, "content": self._wire_content(message.get("content", ""))})
        return out

    def _payload(self, messages, tools, stream):
        payload = {"model": self.model,
                   "messages": self._wire_messages(messages),
                   "temperature": self.temperature,
                   "max_tokens": self.max_tokens,
                   "stream": bool(stream)}
        if tools:
            payload["tools"] = [{"type": "function", "function": tool} for tool in tools]
            payload["tool_choice"] = "auto"
        if self.reasoning_effort and self.reasoning_effort != "off":
            payload["reasoning_effort"] = self.reasoning_effort
        payload.update(self.extra)
        return payload

    # ---- streaming -----------------------------------------------------
    def stream(self, messages, tools=None, stream=True):
        self._begin()
        try:
            resp = self._post("/chat/completions", self._payload(messages, tools, stream))
            if not stream:
                try:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                except ValueError:
                    raise ProviderError("The server returned a non-JSON answer.") from None
                if data.get("error"):
                    raise ProviderError(str(data["error"]))
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                text = message.get("content") or ""
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                calls = [{"id": call.get("id", ""),
                          "name": (call.get("function") or {}).get("name", ""),
                          "arguments": _load_args((call.get("function") or {}).get("arguments"))}
                         for call in message.get("tool_calls") or []]
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                if text:
                    yield {"type": "text", "text": text}
                yield {"type": "done",
                       "message": {"role": "assistant", "content": text, "tool_calls": calls},
                       "usage": data.get("usage") or {},
                       "finish_reason": choice.get("finish_reason") or "stop"}
                return

            buffer = _ToolBuffer()
            text_parts, reason_parts = [], []
            finish, usage = "stop", {}
            for chunk in self._sse(resp):
                if chunk.get("error"):
                    raise ProviderError(str(chunk["error"]))
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or choice.get("message") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    reason_parts.append(reasoning)
                    yield {"type": "reasoning", "text": reasoning}
                piece = delta.get("content")
                if piece:
                    text_parts.append(piece)
                    yield {"type": "text", "text": piece}
                if delta.get("tool_calls"):
                    buffer.add(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            yield {"type": "done",
                   "message": {"role": "assistant",
                               "content": "".join(text_parts),
                               "tool_calls": buffer.result()},
                   "usage": usage,
                   "finish_reason": finish}
        except Cancelled:
            yield {"type": "error", "message": "Cancelled", "cancelled": True}
        except ProviderError as ex:
            yield {"type": "error", "message": str(ex)}
        except Exception as ex:
            yield {"type": "error", "message": "Provider failure: %s" % ex}
        finally:
            self._finish()


# ------------------------------------------------------------------- Anthropic
class AnthropicProvider(Provider):
    """Claude Messages API."""

    kind = "anthropic"

    @staticmethod
    def default_base_url():
        return ANTHROPIC_BASE_URL

    def _headers(self):
        return {"Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION}

    def list_models(self):
        data = self._get_json("/models")
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def _wire_content(self, content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        blocks = []
        for block in content or []:
            if block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                blocks.append({"type": "image", "source": {
                    "type": "base64",
                    "media_type": block.get("mime", "image/png"),
                    "data": block.get("data", "")}})
        return blocks

    def _wire_messages(self, messages):
        system, out, pending_results = [], [], []

        def flush_results():
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages:
            role = message.get("role")
            if role == "system":
                content = message.get("content", "")
                system.append(content if isinstance(content, str)
                              else " ".join(b.get("text", "") for b in content))
                continue
            if role == "tool":
                pending_results.append({"type": "tool_result",
                                        "tool_use_id": message.get("tool_call_id", ""),
                                        "content": message.get("content", "")})
                continue
            flush_results()
            if role == "assistant" and message.get("tool_calls"):
                blocks = self._wire_content(message.get("content", ""))
                for call in message["tool_calls"]:
                    blocks.append({"type": "tool_use", "id": call["id"],
                                   "name": call["name"],
                                   "input": call.get("arguments", {})})
                out.append({"role": "assistant", "content": blocks})
                continue
            blocks = self._wire_content(message.get("content", ""))
            if blocks:
                out.append({"role": role, "content": blocks})
        flush_results()
        return "\n\n".join(s for s in system if s), out

    def _payload(self, messages, tools, stream):
        system, wire = self._wire_messages(messages)
        payload = {"model": self.model,
                   "messages": wire,
                   "max_tokens": self.max_tokens,
                   "temperature": self.temperature,
                   "stream": bool(stream)}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": tool["name"],
                                 "description": tool.get("description", ""),
                                 "input_schema": tool.get("parameters",
                                                          {"type": "object", "properties": {}})}
                                for tool in tools]
        budget = THINKING_BUDGET.get(self.reasoning_effort)
        if budget:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(self.max_tokens, budget + 1024)
            payload["temperature"] = 1.0      # required while thinking is enabled
        payload.update(self.extra)
        return payload

    def stream(self, messages, tools=None, stream=True):
        self._begin()
        try:
            resp = self._post("/messages", self._payload(messages, tools, stream))
            if not stream:
                try:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                except ValueError:
                    raise ProviderError("The server returned a non-JSON answer.") from None
                if data.get("type") == "error":
                    raise ProviderError(str(data.get("error")))
                text, reasoning, calls = [], [], []
                for block in data.get("content") or []:
                    if block.get("type") == "text":
                        text.append(block.get("text", ""))
                    elif block.get("type") == "thinking":
                        reasoning.append(block.get("thinking", ""))
                    elif block.get("type") == "tool_use":
                        calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                                      "arguments": block.get("input") or {}})
                if reasoning:
                    yield {"type": "reasoning", "text": "".join(reasoning)}
                if text:
                    yield {"type": "text", "text": "".join(text)}
                yield {"type": "done",
                       "message": {"role": "assistant", "content": "".join(text),
                                   "tool_calls": calls},
                       "usage": data.get("usage") or {},
                       "finish_reason": data.get("stop_reason") or "stop"}
                return

            blocks, text_parts = {}, []
            finish, usage = "stop", {}
            for chunk in self._sse(resp):
                kind = chunk.get("type")
                if kind == "error":
                    raise ProviderError(str(chunk.get("error")))
                if kind == "message_start":
                    usage = (chunk.get("message") or {}).get("usage") or {}
                elif kind == "content_block_start":
                    block = chunk.get("content_block") or {}
                    blocks[chunk.get("index", 0)] = {
                        "type": block.get("type"),
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "text": [], "json": []}
                elif kind == "content_block_delta":
                    slot = blocks.setdefault(chunk.get("index", 0),
                                             {"type": "text", "id": "", "name": "",
                                              "text": [], "json": []})
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        piece = delta.get("text", "")
                        slot["text"].append(piece)
                        text_parts.append(piece)
                        yield {"type": "text", "text": piece}
                    elif delta.get("type") == "thinking_delta":
                        piece = delta.get("thinking", "")
                        slot["text"].append(piece)
                        yield {"type": "reasoning", "text": piece}
                    elif delta.get("type") == "input_json_delta":
                        slot["json"].append(delta.get("partial_json", ""))
                elif kind == "message_delta":
                    finish = (chunk.get("delta") or {}).get("stop_reason") or finish
                    usage.update(chunk.get("usage") or {})
                elif kind == "message_stop":
                    break
            calls = []
            for index in sorted(blocks):
                slot = blocks[index]
                if slot["type"] == "tool_use":
                    calls.append({"id": slot["id"] or "call_%d" % index,
                                  "name": slot["name"],
                                  "arguments": _load_args("".join(slot["json"]))})
            yield {"type": "done",
                   "message": {"role": "assistant", "content": "".join(text_parts),
                               "tool_calls": calls},
                   "usage": usage,
                   "finish_reason": finish}
        except Cancelled:
            yield {"type": "error", "message": "Cancelled", "cancelled": True}
        except ProviderError as ex:
            yield {"type": "error", "message": str(ex)}
        except Exception as ex:
            yield {"type": "error", "message": "Provider failure: %s" % ex}
        finally:
            self._finish()


# --------------------------------------------------------------------- factory
PROVIDERS = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}

PROVIDER_LABELS = {"openai": "OpenAI-compatible (LM Studio / OpenAI / OpenRouter / Ollama)",
                   "anthropic": "Anthropic (Claude)"}


def default_config():
    return {"provider": "openai",
            "base_url": DEFAULT_BASE_URL,
            "api_key": "",
            "model": DEFAULT_MODEL,
            "temperature": 0.7,
            "max_tokens": 2048,
            "reasoning_effort": "low",
            "timeout": 300,
            "extra": {}}


def make_provider(config=None):
    config = dict(default_config(), **(config or {}))
    cls = PROVIDERS.get(config.get("provider", "openai"), OpenAIProvider)
    return cls(model=config.get("model") or DEFAULT_MODEL,
               base_url=config.get("base_url") or cls.default_base_url(),
               api_key=config.get("api_key", ""),
               temperature=config.get("temperature", 0.7),
               max_tokens=config.get("max_tokens", 2048),
               reasoning_effort=config.get("reasoning_effort", "low"),
               timeout=config.get("timeout", 300),
               extra=config.get("extra"))


# -------------------------------------------------------------------- self-test
def _selftest():
    """python refsynapse\\ai\\providers.py [model-id]"""
    config = default_config()
    if len(sys.argv) > 1:
        config["model"] = sys.argv[1]
    provider = make_provider(config)
    print("Backend:", provider.base_url)
    result = provider.probe()
    print(result["message"])
    for name in result["models"]:
        print("  -", name)
    if not result["ok"]:
        return
    print("\nStreaming a short answer from '%s'...\n" % provider.model)
    messages = [{"role": "system", "content": "You are a terse assistant."},
                user_message("Say hello in one short sentence.")]
    for event in provider.stream(messages):
        if event["type"] == "text":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
        elif event["type"] == "reasoning":
            sys.stdout.write("\033[90m" + event["text"] + "\033[0m")
            sys.stdout.flush()
        elif event["type"] == "error":
            print("\nERROR:", event["message"])
        elif event["type"] == "done":
            print("\n\nfinish_reason:", event["finish_reason"], "| usage:", event["usage"])


if __name__ == "__main__":
    _selftest()