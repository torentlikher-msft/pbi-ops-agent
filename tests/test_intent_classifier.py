import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def _make_agent():
    with patch("foundry_client.app_credential", return_value=Mock()):
        from foundry_client import FoundryAgent

        agent = FoundryAgent("https://x/api/projects/p", "agent", "scope")
    agent._token = AsyncMock(return_value="tok")
    agent._get_agent_def = AsyncMock(return_value={"model": "gpt-x"})
    return agent


def _responses_payload(text: str) -> dict:
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ]
    }


class ClassifyReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_accept(self):
        agent = _make_agent()
        agent._post = AsyncMock(return_value=(200, _responses_payload("accept"), ""))
        self.assertEqual(await agent.classify_reply("yeah go for it"), "accept")

    async def test_decline(self):
        agent = _make_agent()
        agent._post = AsyncMock(return_value=(200, _responses_payload("decline"), ""))
        self.assertEqual(await agent.classify_reply("not right now"), "decline")

    async def test_other_when_unrelated(self):
        agent = _make_agent()
        agent._post = AsyncMock(return_value=(200, _responses_payload("other"), ""))
        self.assertEqual(await agent.classify_reply("what is a measure?"), "other")

    async def test_unrecognized_label_defaults_to_other(self):
        agent = _make_agent()
        agent._post = AsyncMock(return_value=(200, _responses_payload("maybe"), ""))
        self.assertEqual(await agent.classify_reply("hmm"), "other")

    async def test_http_error_returns_error(self):
        agent = _make_agent()
        agent._post = AsyncMock(return_value=(500, {}, "boom"))
        self.assertEqual(await agent.classify_reply("yes"), "error")

    async def test_exception_returns_error(self):
        agent = _make_agent()
        agent._post = AsyncMock(side_effect=RuntimeError("network"))
        self.assertEqual(await agent.classify_reply("yes"), "error")


if __name__ == "__main__":
    unittest.main()
