import asyncio
import json
import logging
import re
import sys
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import websockets

# Silence noisy websocket logs.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


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

    async def wait_for_websocket(self, timeout_seconds=12.0):
        if self.websocket is not None:
            return

        deadline = time.monotonic() + timeout_seconds
        while self.websocket is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    async def ask_chatgpt(self, prompt):
        async with self.websocket_lock:
            await self.wait_for_websocket(timeout_seconds=12.0)
            if self.websocket is None:
                raise RuntimeError(
                    "Browser extension is not connected. Open chatgpt.com and ensure the bridge extension is enabled."
                )

            print(f"[bridge] forwarding prompt ({len(str(prompt or ''))} chars)")
            await self.websocket.send(json.dumps({"type": "prompt", "text": prompt}))
            try:
                response = await asyncio.wait_for(self.websocket.recv(), timeout=45.0)
            except asyncio.TimeoutError as exc:
                print("[bridge] browser response timeout")
                raise RuntimeError(
                    "Timed out waiting for browser response. Check the ChatGPT tab and extension state."
                ) from exc
            data = json.loads(response)

            if data.get("type") == "response":
                text = data.get("text", "")
                print(f"[bridge] received response ({len(str(text))} chars)")
                return text
            if data.get("type") == "error":
                message = data.get("text", "Unknown extension error")
                print(f"[bridge] extension error: {message}")
                raise RuntimeError(message)
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


def extract_balanced_object(text, start_index):
    depth = 0
    in_string = False
    escaped = False

    for index in range(start_index, len(text)):
        ch = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]

    return None


def parse_loose_arguments(arguments_text):
    arguments = {}

    # Parse simple key/value pairs first.
    for key, value in re.findall(r'"([A-Za-z0-9_]+)"\s*:\s*"([^\"]*)"', arguments_text):
        arguments[key] = value

    # Recover command strings that may contain unescaped embedded quotes.
    command_match = re.search(
        r'"command"\s*:\s*"([\s\S]*?)"\s*,\s*"[A-Za-z0-9_]+"\s*:',
        arguments_text,
    )
    if not command_match:
        command_match = re.search(r'"command"\s*:\s*"([\s\S]*?)"\s*}', arguments_text)
    if command_match:
        arguments["command"] = command_match.group(1)

    return arguments


def extract_tool_call_from_text(text):
    text = str(text or "")
    if not text.strip():
        return None

    parsed = extract_json_object(text)
    if isinstance(parsed, dict):
        candidate = None
        if isinstance(parsed.get("tool_call"), dict):
            candidate = parsed.get("tool_call")
        elif parsed.get("name") and parsed.get("arguments") is not None:
            candidate = parsed

        if isinstance(candidate, dict):
            tool_name = str(candidate.get("name", "")).strip()
            tool_args = candidate.get("arguments", {})
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except Exception:
                    tool_args = {}

            if tool_name:
                return {
                    "name": tool_name,
                    "arguments": tool_args if isinstance(tool_args, dict) else {},
                }

    name_match = re.search(r'"name"\s*:\s*"([^\"]+)"', text)
    if not name_match:
        return None

    tool_name = name_match.group(1).strip()
    arguments = {}

    arguments_key = re.search(r'"arguments"\s*:', text)
    if arguments_key:
        brace_start = text.find("{", arguments_key.end())
        if brace_start != -1:
            raw_arguments = extract_balanced_object(text, brace_start)
            if raw_arguments:
                try:
                    parsed_arguments = json.loads(raw_arguments)
                    if isinstance(parsed_arguments, dict):
                        arguments = parsed_arguments
                except Exception:
                    arguments = parse_loose_arguments(raw_arguments)

    return {"name": tool_name, "arguments": arguments}


def looks_like_tool_call_payload(text):
    return bool(re.search(r"[\"']tool_call[\"']\s*:", str(text or "")))


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


def iter_text_chunks(text, chunk_size=48):
    text = str(text or "")
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def sse_start(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()


def sse_emit(handler, payload):
    data = json.dumps(payload, ensure_ascii=False)
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def sse_done(handler):
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def sse_heartbeat_chunk(handler, completion_id, model, created):
    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
        },
    )


def stream_text_response_from_future(handler, model, response_future, timeout_seconds=240, keepalive_seconds=1.5):
    now = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    sse_start(handler)
    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
    )

    deadline = time.monotonic() + timeout_seconds
    last_keepalive = time.monotonic()

    while not response_future.done():
        now_monotonic = time.monotonic()

        if now_monotonic >= deadline:
            response_text = "Bridge timed out waiting for ChatGPT response."
            break

        if now_monotonic - last_keepalive >= keepalive_seconds:
            sse_heartbeat_chunk(handler, completion_id, model, now)
            last_keepalive = now_monotonic

        time.sleep(0.1)
    else:
        try:
            response_text = response_future.result()
        except Exception as exc:
            response_text = f"Bridge error: {exc}"

    for chunk in iter_text_chunks(response_text):
        sse_emit(
            handler,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            },
        )

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )
    sse_done(handler)


def stream_plan_response_from_future(handler, model, plan_future, timeout_seconds=240, keepalive_seconds=1.5):
    now = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    sse_start(handler)
    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
    )

    deadline = time.monotonic() + timeout_seconds
    last_keepalive = time.monotonic()

    while not plan_future.done():
        now_monotonic = time.monotonic()

        if now_monotonic >= deadline:
            timeout_text = "Bridge timed out while planning tool usage."
            for chunk in iter_text_chunks(timeout_text):
                sse_emit(
                    handler,
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                    },
                )
            sse_emit(
                handler,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            )
            sse_done(handler)
            return

        if now_monotonic - last_keepalive >= keepalive_seconds:
            sse_heartbeat_chunk(handler, completion_id, model, now)
            last_keepalive = now_monotonic

        time.sleep(0.1)

    try:
        plan_text = plan_future.result()
    except Exception as exc:
        error_text = f"Bridge error: {exc}"
        for chunk in iter_text_chunks(error_text):
            sse_emit(
                handler,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                },
            )
        sse_emit(
            handler,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )
        sse_done(handler)
        return

    plan = extract_json_object(plan_text) or {}
    extracted_tool_call = extract_tool_call_from_text(plan_text)

    if isinstance(extracted_tool_call, dict):
        tool_name = str(extracted_tool_call.get("name", "")).strip()
        tool_args = extracted_tool_call.get("arguments", {})

        if tool_name:
            call_id = f"call_{uuid.uuid4().hex[:24]}"
            sse_emit(
                handler,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": json.dumps(tool_args if isinstance(tool_args, dict) else {}),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            )
            sse_emit(
                handler,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                },
            )
            sse_done(handler)
            return

    final_text = ""
    if isinstance(plan.get("final"), str):
        final_text = plan.get("final", "").strip()
    if not final_text:
        if looks_like_tool_call_payload(plan_text):
            final_text = "Bridge planner returned malformed tool-call JSON. Please retry this request."
        else:
            final_text = str(plan_text).strip() or "No response generated."

    for chunk in iter_text_chunks(final_text):
        sse_emit(
            handler,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            },
        )

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )
    sse_done(handler)


def stream_text_response(handler, model, text):
    now = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    sse_start(handler)

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
    )

    for chunk in iter_text_chunks(text):
        sse_emit(
            handler,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            },
        )

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )
    sse_done(handler)


def stream_tool_call_response(handler, model, tool_name, arguments):
    now = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    call_id = f"call_{uuid.uuid4().hex[:24]}"

    sse_start(handler)

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
    )

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
    )

    sse_emit(
        handler,
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
    )
    sse_done(handler)


class OpenAICompatibleHandler(BaseHTTPRequestHandler):
    server_version = "ChatGPTBridge/1.1"

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
                    "streaming": True,
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

        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return json_response(self, {"error": "messages is required"}, status=400)

        stream_mode = bool(payload.get("stream"))
        model = payload.get("model") or "chatgpt-browser-bridge"
        tools = payload.get("tools") or []
        last_role = str((messages[-1] or {}).get("role", "")).strip().lower()
        is_tool_followup = last_role == "tool"

        prompt = extract_user_prompt(messages)
        if not prompt and not is_tool_followup:
            return json_response(self, {"error": "No user text found in messages"}, status=400)

        if tools and not is_tool_followup:
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
                "whether to call exactly one function now. Return exactly one valid JSON object and nothing else.\n"
                "If a tool call is needed, output: {\"tool_call\": {\"name\": \"...\", \"arguments\": {...}}}\n"
                "If no tool call is needed, output: {\"final\": \"...\"}.\n\n"
                "Do not include markdown fences, commentary, or trailing text. Escape all JSON strings correctly.\n\n"
                f"TOOLS:\n{json.dumps(tool_specs)}\n\n"
                f"CONVERSATION:\n{conversation_text(messages)}"
            )

            try:
                future = asyncio.run_coroutine_threadsafe(state.ask_chatgpt(planner_prompt), event_loop)
                if stream_mode:
                    try:
                        stream_plan_response_from_future(self, model, future)
                        return
                    except (BrokenPipeError, ConnectionResetError):
                        return
                plan_text = future.result(timeout=240)
                plan = extract_json_object(plan_text) or {}
                extracted_tool_call = extract_tool_call_from_text(plan_text)

                if isinstance(extracted_tool_call, dict):
                    tool_name = str(extracted_tool_call.get("name", "")).strip()
                    tool_args = extracted_tool_call.get("arguments", {})
                    if tool_name:
                        if stream_mode:
                            try:
                                stream_tool_call_response(
                                    self,
                                    model,
                                    tool_name,
                                    tool_args if isinstance(tool_args, dict) else {},
                                )
                                return
                            except (BrokenPipeError, ConnectionResetError):
                                return

                        return json_response(
                            self,
                            openai_tool_call_response(
                                model,
                                tool_name,
                                tool_args if isinstance(tool_args, dict) else {},
                            ),
                        )

                if isinstance(plan.get("final"), str) and plan.get("final").strip():
                    final_text = plan.get("final").strip()
                    if stream_mode:
                        try:
                            stream_text_response(self, model, final_text)
                            return
                        except (BrokenPipeError, ConnectionResetError):
                            return
                    return json_response(self, openai_chat_response(model, final_text))

                if looks_like_tool_call_payload(plan_text):
                    return json_response(
                        self,
                        openai_chat_response(
                            model,
                            "Bridge planner returned malformed tool-call JSON. Please retry this request.",
                        ),
                    )
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=503)

        if is_tool_followup:
            answer_prompt = (
                "You are a helpful assistant. Use the provided tool result(s) to answer the user. "
                "Do not output JSON, and do not request additional tool calls in this step.\n\n"
                f"CONVERSATION:\n{conversation_text(messages)}"
            )
        else:
            answer_prompt = prompt

        if stream_mode:
            try:
                future = asyncio.run_coroutine_threadsafe(state.ask_chatgpt(answer_prompt), event_loop)
                stream_text_response_from_future(self, model, future)
                return
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, status=503)

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
    server = QuietThreadingHTTPServer((host, port), OpenAICompatibleHandler)
    print(f"OpenAI-compatible API with streaming: http://{host}:{port}/v1")
    print("No API key validation on this local backend.")
    server.serve_forever()


async def main():
    global event_loop
    event_loop = asyncio.get_running_loop()

    print("Starting WebSocket server on ws://127.0.0.1:8765 for the extension...")
    print("Starting HTTP API on http://127.0.0.1:8000 ...")
    print("Opening ChatGPT in your browser...")
    webbrowser.open("https://chatgpt.com")

    Thread(target=start_http_server, daemon=True).start()

    async with websockets.serve(websocket_handler, "127.0.0.1", 8765):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shut down.")