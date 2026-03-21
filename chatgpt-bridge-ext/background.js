let socket = null;

function connectToPython() {
    socket = new WebSocket('ws://127.0.0.1:8765');

    socket.onopen = () => console.log("Connected to Python CLI");
    
    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'prompt') {
            // Find the active ChatGPT tab and send it the user's text
            chrome.tabs.query({url: "*://*.chatgpt.com/*"}, (tabs) => {
                if (tabs.length > 0) {
                    chrome.tabs.sendMessage(tabs[0].id, {type: 'send_prompt', text: data.text});
                } else {
                    socket.send(JSON.stringify({type: 'error', text: 'No ChatGPT tab found. Please open chatgpt.com'}));
                }
            });
        }
    };

    socket.onclose = () => {
        // If the Python server restarts, the extension will auto-reconnect
        setTimeout(connectToPython, 3000);
    };
}

connectToPython();

// Listen for the scraped response from the content script and forward it to Python
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === 'chat_response' && socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({type: 'response', text: message.text}));
    }
});