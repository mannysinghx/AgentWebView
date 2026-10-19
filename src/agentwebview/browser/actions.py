"""Typed browser actions. There is deliberately no action that evaluates caller-supplied JavaScript, runs
shell commands, Python or SQL, or touches the filesystem.

Selectors are structured (``css``, ``role`` + ``name``, ``text``, ``label``, ``placeholder``, ``test_id``) and
turned into Playwright locators by the host; raw selector-engine prefixes are rejected so a caller cannot
reach selector engines beyond plain CSS.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

WRITE_CATEGORIES = ("submit", "send", "purchase", "delete", "publish")
_ENGINE_PREFIX = re.compile(r"^\s*[a-z_-]+\s*=", re.I)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Selector(_Strict):
    css: str | None = Field(default=None, max_length=500)
    role: str | None = Field(default=None, max_length=40, pattern=r"^[a-z]+$")
    name: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    placeholder: str | None = Field(default=None, max_length=200)
    test_id: str | None = Field(default=None, max_length=200)

    @field_validator("css")
    @classmethod
    def _plain_css(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip() or "\n" in v or _ENGINE_PREFIX.match(v) or ">>" in v:
            raise ValueError("css must be a plain CSS selector (no selector-engine prefixes or chaining)")
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> Selector:
        kinds = [k for k in ("css", "role", "text", "label", "placeholder", "test_id") if getattr(self, k)]
        if len(kinds) != 1:
            raise ValueError("a selector needs exactly one of css, role, text, label, placeholder, test_id")
        if self.name is not None and self.role is None:
            raise ValueError("name is only valid together with role")
        return self


class Navigate(_Strict):
    type: Literal["navigate"]
    url: str = Field(max_length=2048)


class Click(_Strict):
    type: Literal["click"]
    selector: Selector


class Fill(_Strict):
    type: Literal["fill"]
    selector: Selector
    value: str = Field(max_length=4_000)


class Select(_Strict):
    type: Literal["select"]
    selector: Selector
    values: list[Annotated[str, Field(max_length=200)]] = Field(min_length=1, max_length=20)


class Scroll(_Strict):
    type: Literal["scroll"]
    direction: Literal["up", "down"] = "down"
    pixels: int = Field(default=800, ge=1, le=20_000)


class Extract(_Strict):
    type: Literal["extract"]
    selector: Selector | None = None
    mode: Literal["text", "links", "main_text"] = "main_text"
    max_chars: int = Field(default=20_000, ge=100, le=100_000)


class Screenshot(_Strict):
    type: Literal["screenshot"]
    full_page: bool = False
    # Return the PNG to the caller as base64 (otherwise it is only stored and viewable on the dashboard).
    inline: bool = False


class Close(_Strict):
    type: Literal["close"]


Action = Annotated[
    Navigate | Click | Fill | Select | Scroll | Extract | Screenshot | Close, Field(discriminator="type")
]
ACTION_ADAPTER: TypeAdapter[Any] = TypeAdapter(Action)


def parse_action(data: Any) -> Any:
    return ACTION_ADAPTER.validate_python(data)


def redacted_params(params: dict[str, Any]) -> dict[str, Any]:
    """What may be stored and shown: fill values are never kept after the action ran."""
    if params.get("type") == "fill" and "value" in params:
        return {**params, "value": f"[REDACTED {len(str(params['value']))} chars]"}
    return params


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "purchase",
        re.compile(
            r"\b(buy|purchase|checkout|check out|order|orders|pay|payment|add to cart|"
            r"subscribe|upgrade plan|donate)\b",
            re.I,
        ),
    ),
    (
        "delete",
        re.compile(
            r"\b(delete|remove|destroy|erase|discard|unsubscribe|cancel (my )?(account|subscription)|"
            r"close account|deactivate)\b",
            re.I,
        ),
    ),
    ("publish", re.compile(r"\b(publish|post|tweet|share|go live|release|deploy|make public)\b", re.I)),
    ("send", re.compile(r"\b(send|reply|forward|email|message|invite|notify|submit request)\b", re.I)),
    (
        "submit",
        re.compile(
            r"\b(submit|save|confirm|apply|sign up|register|create account|log ?in|sign in|continue|"
            r"accept|agree|finish|done|ok)\b",
            re.I,
        ),
    ),
]


def _button_like(info: dict[str, Any]) -> bool:
    """Controls that act on the page rather than open a link: buttons, button roles, script links."""
    tag = str(info.get("tag") or "").lower()
    etype = str(info.get("type") or "").lower()
    href = str(info.get("href") or "").strip().lower()
    if tag == "button" or str(info.get("role") or "").lower() == "button":
        return True
    if tag == "input" and etype in ("submit", "button", "image"):
        return True
    return tag == "a" and (href in ("", "#") or href.startswith("javascript:"))


def classify_click(info: dict[str, Any], *, typed_on_page: bool = False) -> str | None:
    """Classify a click target (tag/type/form/text/aria/href/role) as write-capable, or None.

    Conservative by design: anything that submits a form, or whose label or link target reads like a
    send/buy/delete/publish/submit control, needs a capability or a decision. After the agent has typed or
    selected something on the current page, any button-like control (including script-driven ones with a
    neutral label such as "Go") is treated as a submit, because it may send what was typed.
    """
    category = _classify_label(info)
    if category is None and typed_on_page and _button_like(info):
        return "submit"
    return category


def _classify_label(info: dict[str, Any]) -> str | None:
    text = " ".join(str(info.get(k) or "") for k in ("text", "aria", "value", "title", "name"))[:500]
    href = str(info.get("href") or "")
    for category, pattern in _PATTERNS[:-1]:
        if pattern.search(text) or pattern.search(href.replace("-", " ").replace("_", " ").replace("/", " ")):
            return category
    tag = str(info.get("tag") or "").lower()
    etype = str(info.get("type") or "").lower()
    in_form = bool(info.get("form"))
    if (tag == "input" and etype in ("submit", "image")) or (tag == "button" and in_form and etype in ("", "submit")):
        return "submit"
    if _PATTERNS[-1][1].search(text) and (in_form or tag in ("button", "input")):
        return "submit"
    return None
