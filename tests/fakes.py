"""Test doubles. Nothing here touches the network, the screen or a real API key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import requests
from anthropic.types import Message
from PIL import Image


# ------------------------------------------------------------ pokemontcg.io

class FakeResponse:
    def __init__(self, payload: Any = None, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Stands in for requests.Session: returns queued responses, records calls."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def queries(self) -> list[str]:
        return [call["params"]["q"] for call in self.calls]


def api_card(card_id: str, name: str, set_name: str, number: str, printed_total: int,
             prices: dict[str, dict[str, float]] | None) -> dict[str, Any]:
    """A card object shaped like the documented pokemontcg.io v2 card."""
    card = {
        "id": card_id,
        "name": name,
        "number": number,
        "rarity": "Rare Holo",
        "set": {"id": card_id.split("-")[0], "name": set_name,
                "printedTotal": printed_total, "releaseDate": "2023/09/22"},
    }
    if prices is not None:
        card["tcgplayer"] = {"url": f"https://prices.pokemontcg.io/tcgplayer/{card_id}",
                             "updatedAt": "2026/09/27", "prices": prices}
    return card


def search_result(*cards: dict[str, Any]) -> FakeResponse:
    return FakeResponse({"data": list(cards), "page": 1, "pageSize": 6,
                         "count": len(cards), "totalCount": len(cards)})


# ---------------------------------------------------------------- Anthropic

class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    """Stands in for anthropic.Anthropic: each create() returns or raises the next outcome."""

    def __init__(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes))


def tool_response(tool_input: dict[str, Any], name: str = "report_card",
                  stop_reason: str = "tool_use") -> Message:
    """A real SDK Message containing one tool_use block."""
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "tool_use", "id": "toolu_test", "name": name, "input": tool_input}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 80},
    })


def text_response(text: str) -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1500, "output_tokens": 20},
    })


def api_error(cls: type[anthropic.APIStatusError], status: int, message: str = "error") -> anthropic.APIStatusError:
    """Build an SDK status error without an HTTP stack."""
    response = SimpleNamespace(status_code=status, headers={}, request=None)
    return cls(f"Error code: {status} - {message}", response=response, body=None)


CHARIZARD = {
    "cardVisible": True, "cardName": "Charizard", "setName": "Base", "cardNumber": "4/102",
    "language": "English", "isHolo": True, "isGraded": False, "gradingCompany": None,
    "grade": None, "streamPrice": 300, "condition": "LP", "confidence": "high",
}


# ------------------------------------------------------------------- frames

def solid(color: tuple[int, int, int], size: tuple[int, int] = (640, 480)) -> Image.Image:
    return Image.new("RGB", size, color)
