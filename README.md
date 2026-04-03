# GPT CLI Local Bridge

Local OpenAI-compatible API that forwards requests to GPT via browser extension.

<img width="1861" height="984" alt="example" src="https://github.com/user-attachments/assets/6a8469e6-934d-462f-a681-78681cb88d53" />

## Requirements

- Python (3.12 used while making)
- Firefox/Chromium extension in `chatgpt-bridge-ext`
- Logged-in GPT tab at https://chatgpt.com (Didn't test logged out case)

Open FireFox and type about:debugging". Click "This FireFox" and click "Load Temporary Add-on..."
Browse and select "manifest.json" from the folder "chatgpt-bridge-ext" - this will load the temporary extension to youur FireFox.

## Install

```bash
pip install websockets openai
```

## Run Server

```bash
python openai_style_server.py
```

Server endpoints:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/v1/models`
- `http://127.0.0.1:8000/v1/chat/completions`

## Client Example

```bash
python openai_style_receiver.py
```

## Function Calling Test

```bash
cd Example-Agent
python function_call_test.py
```

## Notes

- Use `base_url="http://127.0.0.1:8000/v1"` in OpenAI client.
- API key is not validated by this local backend.
