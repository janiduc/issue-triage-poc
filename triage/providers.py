"""Model providers behind one call shape.

Both the classifier (llm.py) and the agent (agent.py) speak the Anthropic Messages format:
a system prompt, messages whose content is text or a list of text / tool_use / tool_result
blocks, and an optional tool list. `send` returns a body in that same format, so the rest of
the code does not care which provider answered.

Providers:
- anthropic: the Anthropic Messages API (needs ANTHROPIC_API_KEY; paid).
- ollama:    a local open-weights model served by Ollama (free, offline). Requests and
             responses are translated to and from Ollama's /api/chat format.
"""
import json

import requests

from . import config

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class ProviderError(Exception):
    """The model could not be called. The message is safe to show (no secrets)."""


def send(system, messages, tools=None, max_tokens=600, json_mode=False):
    if config.llm_provider() == "ollama":
        return _ollama(system, messages, tools, max_tokens, json_mode)
    return _anthropic(system, messages, tools, max_tokens)


# ---------------- Anthropic ----------------

def _anthropic(system, messages, tools, max_tokens):
    payload = {"model": config.llm_model(), "max_tokens": max_tokens, "temperature": 0,
               "system": system, "messages": messages}
    if tools:
        payload["tools"] = tools
    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={"x-api-key": config.llm_api_key(), "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=config.llm_timeout())
    except requests.Timeout:
        raise ProviderError("LLM request timed out")
    except requests.RequestException as exc:
        raise ProviderError(f"LLM service unreachable ({type(exc).__name__})")
    if resp.status_code == 429:
        raise ProviderError("LLM rate limit reached")
    if resp.status_code in (401, 403):
        raise ProviderError("LLM authentication failed (check API key)")
    if resp.status_code >= 400:
        raise ProviderError(f"LLM service error (HTTP {resp.status_code})")
    return resp.json()


# ---------------- Ollama ----------------

def to_ollama_messages(system, messages):
    """Anthropic-format messages -> Ollama /api/chat messages."""
    out = [{"role": "system", "content": system}]
    tool_names = {}  # tool_use id -> tool name, so results can name their tool
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = []
            for b in content:
                if b.get("type") == "tool_use":
                    tool_names[b.get("id")] = b.get("name")
                    calls.append({"function": {"name": b.get("name"), "arguments": b.get("input") or {}}})
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        for b in content:  # user turn: tool results and/or text
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "content": b.get("content", ""),
                            "tool_name": tool_names.get(b.get("tool_use_id"), "")})
            elif b.get("type") == "text":
                out.append({"role": "user", "content": b.get("text", "")})
    return out


def to_ollama_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def from_ollama_response(body):
    """Ollama /api/chat response -> Anthropic-format body (content blocks + usage)."""
    message = body.get("message") or {}
    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # some models return the arguments as a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        content.append({"type": "tool_use", "id": f"call_{i}", "name": fn.get("name"), "input": args})
    return {"content": content,
            "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
            "usage": {"input_tokens": body.get("prompt_eval_count", 0),
                      "output_tokens": body.get("eval_count", 0)}}


def _ollama(system, messages, tools, max_tokens, json_mode):
    payload = {"model": config.llm_model(), "stream": False,
               "messages": to_ollama_messages(system, messages),
               "options": {"temperature": 0, "num_predict": max_tokens}}
    if tools:
        payload["tools"] = to_ollama_tools(tools)
    if json_mode:
        payload["format"] = "json"
    try:
        resp = requests.post(f"{config.ollama_url()}/api/chat", json=payload,
                             timeout=config.llm_timeout())
    except requests.Timeout:
        raise ProviderError("Local model timed out (try a smaller model or a longer LLM_TIMEOUT)")
    except requests.RequestException as exc:
        raise ProviderError(f"Ollama is not reachable at {config.ollama_url()} ({type(exc).__name__})")
    if resp.status_code == 404:
        raise ProviderError(f"Ollama has no model '{config.llm_model()}' (run: ollama pull {config.llm_model()})")
    if resp.status_code >= 400:
        raise ProviderError(f"Ollama error (HTTP {resp.status_code})")
    return from_ollama_response(resp.json())
