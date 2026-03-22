import json
import re
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent
MOCK_DB_PATH = ROOT / "mock_records.json"


def get_mock_record(record_id: str) -> dict:
    data = json.loads(MOCK_DB_PATH.read_text(encoding="utf-8"))
    for row in data.get("records", []):
        if row.get("id") == record_id:
            return row
    return {"error": f"record '{record_id}' not found"}


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            return {}

    return {}


def run_function_call_flow() -> bool:
    client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_mock_record",
                "description": "Fetch one mock user record by id from local JSON.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "record_id": {
                            "type": "string",
                            "description": "Record id like u_1001"
                        }
                    },
                    "required": ["record_id"]
                }
            }
        }
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are a function-calling assistant. "
                "If user asks for a record, call get_mock_record with the record id."
            ),
        },
        {"role": "user", "content": "Get record u_1002 and summarize it in one sentence."},
    ]

    first = client.chat.completions.create(
        model="chatgpt-browser-bridge",
        messages=messages,
        tools=tools,
        tool_choice="auto",
    )

    tool_calls = first.choices[0].message.tool_calls or []
    if not tool_calls:
        print("INFO: Backend did not emit native tool_calls. Trying JSON-planned tool call fallback...")
        fallback_first = client.chat.completions.create(
            model="chatgpt-browser-bridge",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You must plan one function call as strict JSON and nothing else. "
                        "Format exactly: {\"tool_call\": {\"name\": \"get_mock_record\", "
                        "\"arguments\": {\"record_id\": \"u_1002\"}}}. "
                        "Task: fetch record u_1002."
                    ),
                }
            ],
        )
        plan = extract_json(fallback_first.choices[0].message.content or "")
        tool_call = plan.get("tool_call", {}) if isinstance(plan, dict) else {}
        name = tool_call.get("name") if isinstance(tool_call, dict) else None
        args = tool_call.get("arguments", {}) if isinstance(tool_call, dict) else {}

        if name != "get_mock_record" or not isinstance(args, dict):
            print("FAIL: Fallback planner did not produce a valid tool call.")
            print("Raw content:")
            print(fallback_first.choices[0].message.content)
            return False

        result = get_mock_record(args.get("record_id", ""))
        second = client.chat.completions.create(
            model="chatgpt-browser-bridge",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Using this tool result, provide a one-sentence summary for the user:\n"
                        f"{json.dumps(result)}"
                    ),
                }
            ],
        )

        final_text = (second.choices[0].message.content or "").strip()
        if not final_text:
            print("FAIL: Empty final assistant response after fallback tool flow.")
            return False

        print("PASS: Function calling flow completed (JSON-planned fallback mode).")
        print("Final response:")
        print(final_text)
        return True

    messages.append(
        {
            "role": "assistant",
            "content": first.choices[0].message.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.function.name,
                        "arguments": c.function.arguments,
                    },
                }
                for c in tool_calls
            ],
        }
    )

    for call in tool_calls:
        if call.function.name != "get_mock_record":
            print(f"FAIL: Unexpected tool requested: {call.function.name}")
            return False

        args = json.loads(call.function.arguments or "{}")
        result = get_mock_record(args.get("record_id", ""))

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": "get_mock_record",
                "content": json.dumps(result),
            }
        )

    second = client.chat.completions.create(
        model="chatgpt-browser-bridge",
        messages=messages,
    )

    final_text = (second.choices[0].message.content or "").strip()
    if not final_text:
        print("FAIL: Empty final assistant response after tool execution.")
        return False

    print("PASS: Function calling flow completed.")
    print("Final response:")
    print(final_text)
    return True


if __name__ == "__main__":
    ok = run_function_call_flow()
    raise SystemExit(0 if ok else 1)
