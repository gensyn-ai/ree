"""Gensyn REE SDK Hello World

This is a minimal example of building on top of REE using the gensyn_sdk.

It compiles a hardcoded model with `prepare_task.run`, then loads it with
`InferenceSession` and runs a hardcoded prompt. Finally, it prints the generated text
and saves the receipt.
"""

import json
from pathlib import Path

from gensyn_sdk import InferenceSession, OperationSet
from gensyn_sdk.location import resolve_task_dir
from gensyn_sdk.prepare_task import run as prepare_task

MODEL_NAME = "Qwen/Qwen3-0.6B"
MODEL_REVISION = "main"
MESSAGES = [{"role": "user", "content": 'Reply with exactly "Hello, world!"'}]
MAX_NEW_TOKENS = 8

TASKS_ROOT = Path.home() / ".cache" / "gensyn"


def main() -> None:
    TASKS_ROOT.mkdir(parents=True, exist_ok=True)

    prepare_task(
        tasks_root=TASKS_ROOT,
        task_dir=None,
        model_name=MODEL_NAME,
        model_revision=MODEL_REVISION,
        prompt_text=json.dumps(MESSAGES, ensure_ascii=False),
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

    result = session.complete(
        messages=MESSAGES,
        max_new_tokens=MAX_NEW_TOKENS,
        enable_thinking=False,
    )
    receipt_path = TASKS_ROOT / "hello_world_receipt.json"
    result.receipt.save(receipt_path)

    print("--- output ---")
    print(result.text)
    print("--- receipt path ---")
    print(receipt_path)
    print("--- receipt hash ---")
    print(result.receipt.receipt_hash)


if __name__ == "__main__":
    main()
