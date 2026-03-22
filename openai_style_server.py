import asyncio
import json
import logging
import re
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import websockets

# Silence noisy websocket logs.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


class BridgeState:
    def __init__(self):
        self.websocket = None
        self.websocket_lock = asyncio.Lock()

    async def register_websocket(self, websocket):
        if self.websocket is not None:
            await self.websocket.close()
        self.websocket = websocket
        print("\n--- Browser extension connected ---")

    async def clear_websocket(self, websocket):
        if self.websocket is websocket:
            self.websocket = None
            print("\n[Extension disconnected. Waiting to reconnect...]")

    async def ask_chatgpt(self, prompt):
        async with self.websocket_lock:
            if self.websocket is None:
                raise RuntimeError("Browser extension is not connected")

            await self.websocket.send(json.dumps({"type": "prompt", "text": prompt}))
            response = await self.websocket.recv()
            data = json.loads(response)

            if data.get("type") == "response":
                return data.get("text", "")
            if data.get("type") == "error":
                raise RuntimeError(data.get("text", "Unknown extension error"))
            raise RuntimeError("Unexpected message received from extension")


state = BridgeState()
event_loop = None


def extract_user_prompt(messages):
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return ""

    last = user_messages[-1].get("content", "")
    if isinstance(last, list):
        parts = []
        for item in last:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts).strip()

    return str(last).strip()


def flatten_content(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def conversation_text(messages):
    lines = []
    for message in messages:
        role = message.get("role", "unknown")
        text = flatten_content(message.get("content", ""))
        if role == "tool":
            name = message.get("name", "tool")
            lines.append(f"tool:{name}: {text}")
        elif text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines).strip()


def extract_json_object(text):
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            return None

    return None


def openai_chat_response(model, text):
    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def openai_tool_call_response(model, tool_name, arguments):
    now = int(time.time())
    call_id = f"call_{uuid.uuid4().hex[:24]}"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def json_response(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class OpenAICompatibleHandler(BaseHTTPRequestHandler):
    server_version = "ChatGPTBridge/1.0"

    def log_message(self, format_str, *args):
        return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return json_response(
                self,
                {
                    "ok": True,
                    "extension_connected": state.websocket is not None,
                    "service": "chatgpt-browser-bridge",
                },
            )

        if self.path == "/v1/models":
            return json_response(
                self,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "chatgpt-browser-bridge",
                            "object": "model",
                            "owned_by": "local",
                        }
                    ],
                },
            )

        return json_response(self, {"error": "Not found"}, status=404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return json_response(self, {"error": "Not found"}, status=404)

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return json_response(self, {"error": "Invalid JSON payload"}, status=400)

        if payload.get("stream"):
            return json_response(
                self,
                {"error": "stream=true is not supported by this backend"},
                status=400,
            )

        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return json_response(self, {"error": "messages is required"}, status=400)

        model = payload.get("model") or "chatgpt-browser-bridge"
        tools = payload.get("tools") or []

        prompt = extract_user_prompt(messages)
        if not prompt:
            return json_response(self, {"error": "No user text found in messages"}, status=400)

        has_tool_result = any(m.get("role") == "tool" for m in messages)

        if tools and not has_tool_result:
            tool_specs = []
            for tool in tools:
                fn = (tool or {}).get("function", {})
                if fn.get("name"):
                    tool_specs.append(
                        {
                            "name": fn.get("name"),
                            "description": fn.get("description", ""),
                            "parameters": fn.get("parameters", {}),
                        }
                    )

            planner_prompt = (
                "You are an API tool planner. Based on the conversation and available tools, decide "
                "whether to call exactly one function now. Return JSON only.\n"
                "If a tool call is needed, output: {\"tool_call\": {\"name\": \"...\", \"arguments\": {...}}}\n"
                "If no tool call is needed, output: {\"final\": \"...\"}.\n\n"
                f"TOOLS:\n{json.dumps(tool_specs)}\n\n"
                f"CONVERSATION:\n{conversation_text(messages)}"
            )

            try:
                future = asyncio.run_coroutine_threadsafe(state.ask_chatgpt(planner_prompt), event_loop)
                plan_text = future.result(timeout=240)
                plan = extract_json_object(plan_text) or {}

                if isinstance(plan.get("tool_call"), dict):
                    tool_call = plan["tool_call"]
                    tool_name = str(tool_call.get("name", "")).strip()
                    tool_args = tool_call.get("arguments", {})
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except Exception:
                            tool_args = {}
                    if tool_name:
                        return json_response(
                            self,
                            openai_tool_call_response(model, tool_name, tool_args if isinstance(tool_args, dict) else {}),
                        )

                if isinstance(plan.get("final"), str) and plan.get("final").strip():
                    return json_response(self, openai_chat_response(model, plan.get("final").strip()))
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=503)

        if has_tool_result:
            answer_prompt = (
                "You are a helpful assistant. Use the provided tool result(s) to answer the user.\n\n"
                f"CONVERSATION:\n{conversation_text(messages)}"
            )
        else:
            answer_prompt = prompt

        try:
            future = asyncio.run_coroutine_threadsafe(state.ask_chatgpt(answer_prompt), event_loop)
            answer = future.result(timeout=240)
        except Exception as exc:
            return json_response(self, {"error": str(exc)}, status=503)

        return json_response(self, openai_chat_response(model, answer))


async def websocket_handler(websocket):
    await state.register_websocket(websocket)
    try:
        await websocket.wait_closed()
    finally:
        await state.clear_websocket(websocket)


def start_http_server(host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), OpenAICompatibleHandler)
    print(f"OpenAI-compatible API: http://{host}:{port}/v1")
    print("No API key validation on this local backend.")
    server.serve_forever()


async def main():
    global event_loop
    event_loop = asyncio.get_running_loop()

    print("Starting WebSocket server on ws://localhost:8765 for the extension...")
    print("Starting HTTP API on http://127.0.0.1:8000 ...")
    print("Opening ChatGPT in your browser...")
    webbrowser.open("https://chatgpt.com")

    Thread(target=start_http_server, daemon=True).start()

    async with websockets.serve(websocket_handler, "localhost", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shut down.")