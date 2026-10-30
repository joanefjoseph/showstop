"""
Gemini web app (gemini.google.com) selectors.

If Gemini drafting stops working, this is the file to fix: run `python -m dm_assistant gemini-login`,
right-click the element in that window -> Inspect, and update the selector.
Comma-separated alternatives are allowed.
"""
# Chat input
INPUT = (
    "rich-textarea div.ql-editor[contenteditable='true'], "
    "div.ql-editor[contenteditable='true'], div[contenteditable='true'][role='textbox']"
)
SEND_BUTTON = "button.send-button:not([disabled]), button[aria-label*='Send' i]:not([disabled])"
STOP_BUTTON = "button[aria-label*='Stop' i]"

# Answers
RESPONSE = "model-response-content, model-response"
RESPONSE_TEXT = "message-content, .message-content"
CODE_BLOCK = "code-block, [data-test-id='code-content'], pre code"

# Present only when signed out
SIGNED_OUT = ("a[href*='accounts.google.com/ServiceLogin'], a[href*='accounts.google.com/signin'], "
              "a[aria-label='Sign in']")

# Optional model picker (used only when GEMINI_WEB_MODEL is set)
MODEL_MENU_BUTTON = ("[data-test-id='bard-mode-menu-button'], bard-mode-switcher button, "
                     "button[aria-label*='mode' i]")
MODEL_MENU_ITEM = "[role='menuitem'], [role='menuitemradio'], mat-option"