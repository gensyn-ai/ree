"""Bare-bones REE SDK tool-calling demo."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.request import Request, urlopen


from gensyn_sdk import InferenceSession, OperationSet
from gensyn_sdk.location import resolve_task_dir
from gensyn_sdk.prepare_task import run as prepare_task


MODEL_NAME = "Qwen/Qwen3-0.6B"
MODEL_REVISION = "main"
URL = "https://example.com"
MAX_NEW_TOKENS = 128
TASKS_ROOT = Path.home() / ".cache" / "gensyn"

FETCH_WEB_PAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_web_page",
        "description": "Fetch a web page and return a compact summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
    },
}


def fetch_web_page(url: str, max_chars: int = 600) -> dict:
    request = Request(url, headers={"User-Agent": "ree-sdk-tool-demo/1.0"})
    with urlopen(request, timeout=10) as response:
        html = response.read(200_000).decode("utf-8", errors="replace")
        status = response.status
        content_type = response.headers.get("content-type", "")

    return {
        "ok": 200 <= status < 400,
        "status": status,
        "url": url,
        "content_type": content_type,
        "content": html[:max_chars],
    }


def parse_tool_call(text: str) -> dict:
    match = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S)
    return json.loads(match.group(1) if match else text)


def call_tool(tool_call: dict) -> dict:
    if tool_call["name"] != "fetch_web_page":
        raise ValueError(f"unknown tool: {tool_call['name']}")
    return fetch_web_page(**tool_call.get("arguments", {}))


def print_json(title: str, value: dict) -> None:
    print(f"--- {title} ---")
    print(json.dumps(value, indent=2, sort_keys=True))


def main() -> int:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)
    prepare_task(
        tasks_root=TASKS_ROOT,
        task_dir=None,
        model_name=MODEL_NAME,
        model_revision=MODEL_REVISION,
        prompt_text="hello",
        prompt_file=None,
        max_new_tokens=MAX_NEW_TOKENS,
        short_circuit_length=None,
        short_circuit_token=None,
        disable_kv_cache=False,
    )

    task_dir = resolve_task_dir(
        task_dir=None,
        tasks_root=TASKS_ROOT,
        model_name=MODEL_NAME,
        model_revision=MODEL_REVISION,
    )
    session = InferenceSession(
        task_dir=task_dir,
        operation_set=OperationSet.REPRODUCIBLE,
    )

    messages = [
        {"role": "system", "content": "Call fetch_web_page before answering."},
        {"role": "user", "content": f"Fetch {URL} and return the content."},
    ]
    first = session.complete(
        messages=messages,
        tools=[FETCH_WEB_PAGE_TOOL],
        max_new_tokens=MAX_NEW_TOKENS,
        enable_thinking=False,
    )

    print("--- assistant tool request ---")
    print(first.text)

    tool_call = parse_tool_call(first.text)
    tool_result = call_tool(tool_call)
    print_json("executed tool result", tool_result)

    messages += [
        {
            "role": "assistant",
            "tool_calls": [{"type": "function", "function": tool_call}],
        },
        {"role": "tool", "content": json.dumps(tool_result)},
    ]
    final = session.complete(
        messages=messages,
        tools=[FETCH_WEB_PAGE_TOOL],
        max_new_tokens=MAX_NEW_TOKENS,
        enable_thinking=False,
    )

    print("--- final assistant answer ---")
    print(final.text)
    print("--- receipt hash ---")
    print(final.receipt.receipt_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
