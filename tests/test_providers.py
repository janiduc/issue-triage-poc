"""Tests for the model provider layer: the free local Ollama provider and its translation
to and from the Anthropic message format the rest of the app uses.

Ollama itself is mocked; nothing is downloaded or run.
"""
import json
import os
from unittest import mock

from tests.test_triage import Base
from triage import config, providers


def ollama_reply(content="", tool_calls=None, prompt=500, out=60):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return mock.Mock(status_code=200, json=lambda: {"message": message, "prompt_eval_count": prompt,
                                                     "eval_count": out})


def call(name, args):
    return {"function": {"name": name, "arguments": args}}


class OllamaProviderTests(Base):
    def setUp(self):
        os.environ["LLM_PROVIDER"] = "ollama"
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)  # the local provider needs no key
        super().setUp()
        os.environ.pop("ANTHROPIC_API_KEY", None)  # Base.setUp sets a test key

    def tearDown(self):
        os.environ.pop("LLM_PROVIDER", None)
        super().tearDown()

    # PR1: with the local provider the AI is enabled without any API key, with local defaults
    def test_pr1_enabled_without_key(self):
        self.assertTrue(config.llm_enabled())
        self.assertEqual(config.llm_model(), "qwen2.5:3b")
        self.assertEqual(config.llm_timeout(), 180.0)

    # PR2: classification runs through Ollama in JSON mode and is validated as usual
    def test_pr2_classification_through_ollama(self):
        reply = json.dumps({"type": "bug", "module": "payments", "priority": "critical",
                            "confidence": 0.8, "justification": "Checkout down.", "duplicate_of": None,
                            "recommendation": None, "recommendation_sources": []})
        lead = self.as_user("lead")
        self.agent_patch.stop()  # let the agent call the (mocked) provider too, and fail over to rules
        with mock.patch.object(providers.requests, "post",
                               side_effect=[ollama_reply(reply), ollama_reply("I cannot help.")]) as post:
            data = lead.post("/api/issues", json={"title": "Checkout down",
                                                  "description": "Nobody can pay at checkout since this morning."}).get_json()
        self.assertEqual(data["suggestion"]["source"], "llm")
        self.assertEqual(data["suggestion"]["priority"], "critical")
        first = post.call_args_list[0]
        self.assertTrue(first.args[0].endswith("/api/chat"))
        self.assertEqual(first.kwargs["json"]["format"], "json")
        self.assertEqual(first.kwargs["json"]["messages"][0]["role"], "system")

    # PR3: the agent's tool loop works through Ollama, including tool results sent back
    def test_pr3_agent_tool_loop_through_ollama(self):
        reply = json.dumps({"type": "bug", "module": "payments", "priority": "high", "confidence": 0.8,
                            "justification": "x", "duplicate_of": None, "recommendation": None,
                            "recommendation_sources": []})
        dev = self.as_user("dev.payments").get("/api/me").get_json()["id"]
        responses = [
            ollama_reply(reply),
            ollama_reply(tool_calls=[call("search_similar_issues", {"query": "discount code error"})]),
            ollama_reply(tool_calls=[call("submit_plan", json.dumps({  # arguments as a JSON string
                "type": "bug", "module": "payments", "priority": "critical", "action": "assign",
                "assignee_id": dev, "comment_for_reporter": "Thanks, we're on it.",
                "confidence": 0.7, "summary": "Coupon bug again."}))]),
        ]
        self.agent_patch.stop()
        lead = self.as_user("lead")
        with mock.patch.object(providers.requests, "post", side_effect=responses) as post:
            issue = lead.post("/api/issues", json={"title": "Discount code error",
                                                   "description": "Applying a discount code at checkout shows a 500 error."}).get_json()
        plan = lead.get(f"/api/issues/{issue['id']}").get_json()["plan"]
        self.assertEqual(plan["source"], "agent")
        self.assertEqual(plan["plan"]["assignee_id"], dev)
        self.assertEqual(plan["plan"]["classification"]["priority"], "critical")
        third = post.call_args_list[2].kwargs["json"]
        self.assertIn("tools", third)
        roles = [m["role"] for m in third["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(third["messages"][3]["tool_name"], "search_similar_issues")

    # PR4: Ollama not running -> clear message and the backup classifier takes over
    def test_pr4_ollama_down_falls_back(self):
        lead = self.as_user("lead")
        with mock.patch.object(providers.requests, "post",
                               side_effect=providers.requests.ConnectionError()):
            data = lead.post("/api/issues", json={"title": "Report wrong",
                                                  "description": "The monthly report totals are wrong."}).get_json()
        self.assertEqual(data["suggestion"]["source"], "fallback")
        self.assertIn("Ollama is not reachable", data["suggestion"]["fallback_reason"])

    # PR5: a missing model gives the exact command to fix it
    def test_pr5_missing_model_message(self):
        with mock.patch.object(providers.requests, "post", return_value=mock.Mock(status_code=404)):
            with self.assertRaises(providers.ProviderError) as ctx:
                providers.send("s", [{"role": "user", "content": "hi"}])
        self.assertIn("ollama pull qwen2.5:3b", str(ctx.exception))
