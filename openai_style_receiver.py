from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed"  # ignored by your backend
)

resp = client.chat.completions.create(
    model="chatgpt-browser-bridge",
    messages=[
        {"role": "user", "content": "Write a haiku about local APIs"}
    ]
)

print(resp.choices[0].message.content)