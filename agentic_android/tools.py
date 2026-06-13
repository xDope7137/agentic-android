"""Tool schemas exposed to Claude.

The agent runs a manual tool-use loop: after any action that changes the
screen, it returns a fresh screenshot in the tool_result so Claude can see the
effect of what it just did (the computer-use pattern, adapted for Android).
"""

TOOLS = [
    {
        "name": "screenshot",
        "description": (
            "Capture the current screen and return it as an image. Use this "
            "when you need to re-check the screen state without acting."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "tap",
        "description": (
            "Tap a point on the screen. Coordinates are in pixels of the "
            "screenshot image you were shown (origin top-left)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X pixel (0 = left edge)"},
                "y": {"type": "integer", "description": "Y pixel (0 = top edge)"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tap_element",
        "description": (
            "PREFERRED way to tap: tap an element from the on-screen element list "
            "by its #index (e.g. 5 for '#5') or by its text/label. More reliable "
            "than raw tap coordinates — no guessing. Give either index or text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "The element's #number from the list."},
                "text": {"type": "string", "description": "Case-insensitive substring of the element's label/id."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "swipe",
        "description": (
            "Swipe/drag from one point to another, in screenshot pixels. Use "
            "for scrolling (swipe up to scroll down the page) and dragging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "duration_ms": {
                    "type": "integer",
                    "description": "Gesture duration in ms (default 300; use 600+ for a slow drag).",
                },
            },
            "required": ["x1", "y1", "x2", "y2"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text into the currently focused input field. Tap the field "
            "first so it has focus. ASCII works best."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press_key",
        "description": (
            "Send a hardware/navigation key event. Common keys: BACK, HOME, "
            "ENTER, TAB, DEL, APP_SWITCH, VOLUME_UP, VOLUME_DOWN, POWER, "
            "MENU, SEARCH."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name, e.g. BACK or KEYCODE_BACK."}
            },
            "required": ["key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dump_ui",
        "description": (
            "Return the uiautomator XML view hierarchy as text. Use this when "
            "you need exact bounds, resource-ids, text, or content-desc of "
            "elements that are hard to target visually."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "launch_app",
        "description": "Launch an app by its package name, e.g. com.android.settings.",
        "input_schema": {
            "type": "object",
            "properties": {"package": {"type": "string"}},
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_apps",
        "description": (
            "List installed app package names to use with launch_app — call this "
            "instead of guessing a package. Optionally filter by a case-insensitive "
            "substring of the package name (e.g. 'clock', 'whats', 'chrome'). If "
            "nothing matches, that app isn't installed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Substring to match, e.g. 'clock'."},
                "launchable_only": {
                    "type": "boolean",
                    "description": "Only apps with a launcher icon (default true).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the operator a question when you're stuck or need a decision you "
            "can't make yourself. Provide 2-4 concrete options; the user may also "
            "reply freeform. Returns the user's answer so you can continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "One short, specific question."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 concrete choices to offer.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "done",
        "description": (
            "Call this when the task is complete or cannot proceed. Provide a "
            "short summary of the outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success": {"type": "boolean", "description": "Whether the task was accomplished."},
            },
            "required": ["summary", "success"],
            "additionalProperties": False,
        },
    },
]

# Tools whose result should include a fresh screenshot of the resulting screen.
SCREEN_CHANGING = {"tap", "tap_element", "swipe", "type_text", "press_key", "launch_app", "screenshot"}
