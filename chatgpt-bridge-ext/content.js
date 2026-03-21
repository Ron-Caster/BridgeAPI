chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.type === 'send_prompt') {
        submitPrompt(request.text);
    }
});

function submitPrompt(text) {
    console.log("🔥 BRIDGE ACTIVE: Received this from Python ->", text);

    const promptBox = document.querySelector('#prompt-textarea');
    if (!promptBox) {
        console.error("Could not find prompt box!");
        return;
    }

    const initialCount = document.querySelectorAll('div[data-message-author-role="assistant"]').length;

    // 1. Focus the box
    promptBox.focus();
    
    // 2. Select all existing text and overwrite it (prevents glitchy double-pastes)
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
    
    // 3. THE FIX: Force React to recognize the change so it enables the Send button
    promptBox.dispatchEvent(new Event('input', { bubbles: true }));

    // 4. Wait slightly longer (300ms) for React to update the button state
    setTimeout(() => {
        const sendButton = document.querySelector('button[data-testid="send-button"]');
        
        // Check if the button exists AND isn't disabled
        if (sendButton && !sendButton.disabled) {
            sendButton.click();
        } else {
            // Ultimate fallback: simulate a raw Enter key press
            promptBox.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
            }));
        }
        
        waitForNewResponse(initialCount);
    }, 300);
}

function waitForNewResponse(initialCount) {
    let textStableCount = 0;
    let lastText = "";

    console.log("Waiting for ChatGPT to finish generating...");

    const checkInterval = setInterval(() => {
        const responses = document.querySelectorAll('div[data-message-author-role="assistant"]');
        
        // 3. Has the new message appeared in the DOM yet?
        if (responses.length > initialCount || (initialCount === 0 && responses.length > 0)) {
            
            const latestResponse = responses[responses.length - 1].innerText;
            const stopBtn = document.querySelector('button[data-testid="stop-button"], button[aria-label="Stop generating"]');
            
            if (stopBtn) {
                // The "Stop Generating" button is visible, so it's definitely still typing
                textStableCount = 0; 
            } else {
                // The Stop button is gone. Let's verify the text has stopped changing.
                if (latestResponse === lastText && latestResponse.trim() !== "") {
                    textStableCount++;
                    
                    // If the text hasn't changed for 3 checks (1.5 seconds), it's done!
                    if (textStableCount >= 3) {
                        clearInterval(checkInterval);
                        console.log("Response complete! Sending back to CLI.");
                        chrome.runtime.sendMessage({type: 'chat_response', text: latestResponse});
                    }
                } else {
                    // Text is still streaming in
                    lastText = latestResponse;
                    textStableCount = 0;
                }
            }
        }
    }, 500);
}