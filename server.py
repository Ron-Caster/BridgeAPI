import asyncio
import websockets
import json
import sys
import logging
import webbrowser  # <-- NEW: Imports the browser automation tool

# Silence the annoying "ghost ping" errors
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

async def chat_handler(websocket):
    print("\n--- Connected to Firefox Extension ---")
    print("Ready! Type your message (or 'exit' to quit).")
    
    try:
        while True:
            user_input = await asyncio.to_thread(input, "\nYou: ")
            
            if user_input.strip().lower() == 'exit':
                print("Closing server...")
                sys.exit(0)
            if not user_input.strip():
                continue
                
            await websocket.send(json.dumps({"type": "prompt", "text": user_input}))
            print("ChatGPT is typing...", end="\r")
            
            response = await websocket.recv()
            data = json.loads(response)
            
            if data.get("type") == "response":
                print(" " * 20, end="\r") 
                print(f"ChatGPT: {data.get('text')}")
            elif data.get("type") == "error":
                print(f"\n[Extension Error: {data.get('text')}]")
                
    except websockets.exceptions.ConnectionClosed:
        print("\n[Connection lost. Waiting for Firefox to reconnect...]")

async def main():
    print("Starting local WebSocket server on ws://localhost:8765...")
    
    # --- NEW: Tell Python to open a new Firefox tab automatically ---
    print("Opening ChatGPT in your browser...")
    webbrowser.open('https://chatgpt.com')
    
    # Start the server
    async with websockets.serve(chat_handler, "localhost", 8765):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer shut down.")