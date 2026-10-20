"""
LinkedIn DOM selectors.

LinkedIn changes its markup from time to time. If scanning finds no conversations or
sending fails, this is the file to fix: open https://www.linkedin.com/messaging/ in
Chrome, right-click the element -> Inspect, and update the selector. Comma-separated
alternatives are allowed (first match wins).
"""

MESSAGING_URL = "https://www.linkedin.com/messaging/"

# Conversation list (left pane)
CONVO_ITEM = "li.msg-conversation-listitem"
CONVO_NAME = ".msg-conversation-listitem__participant-names, .msg-conversation-card__participant-names"
CONVO_SNIPPET = ".msg-conversation-card__message-snippet, .msg-conversation-card__message-snippet-body"
CONVO_TIME = ".msg-conversation-listitem__time-stamp, .msg-conversation-card__time-stamp"
CONVO_UNREAD = ".msg-conversation-card__unread-count, .notification-badge--show"
CONVO_LINK = "a.msg-conversation-listitem__link, a[href*='/messaging/thread/']"

# Open thread (right pane)
THREAD_TITLE = ".msg-entity-lockup__entity-title, #thread-detail-jump-target"
MESSAGE_EVENT = "li.msg-s-message-list__event"
MESSAGE_SENDER = ".msg-s-message-group__name, .msg-s-message-group__profile-link"
MESSAGE_TIME = ".msg-s-message-group__timestamp"
DAY_HEADING = ".msg-s-message-list__time-heading"
MESSAGE_BODY = ".msg-s-event-listitem__body"

# Composer
COMPOSE_BOX = "div.msg-form__contenteditable[contenteditable='true']"
SEND_BUTTON = "button.msg-form__send-button"

# The list preview starts with this when YOU sent the last message (adjust for non-English UI)
YOU_PREFIXES = ("You:",)

LOGGED_OUT_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/login", "/signup")