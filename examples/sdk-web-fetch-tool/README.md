# Gensyn REE SDK Web Fetch Tool Demo

This is a bare-bones reproducible inference demo that adds one tool-calling step on
top of the REE SDK. It loads `Qwen/Qwen3-0.6B`, advertises a `fetch_web_page` tool
with the tokenizer chat template, asks the model to fetch `https://example.com`,
executes the emitted `<tool_call>{...}</tool_call>`, and sends the fetched content
back to the model for a final answer.

## Run the demo

Run it directly in the REE container:

```
docker run --rm \
  -v "$HOME/.cache:/home/gensyn/.cache" \
  -v "$PWD:/app" \
  --entrypoint python3 \
  gensynai/ree:v0.5.0 \
  /app/web_fetch_tool.py
```
