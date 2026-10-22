"""Turn a full browser-agent run into a replayable path for the fast engine.

Steps are matched by the element's visible name, which both engines compute the same way
(accessibility name). Navigation, extraction and other agent-only actions are left out: where
the recorded path no longer fits the page, Jev decides with the next recorded step as a hint.
"""


def _label(element) -> str:
    if element is None:
        return ""
    attrs = getattr(element, "attributes", None) or {}
    return (getattr(element, "ax_name", None) or attrs.get("aria-label") or attrs.get("title")
            or attrs.get("placeholder") or attrs.get("alt") or attrs.get("value") or "").strip()


def from_browser_use(history) -> list[dict]:
    steps: list[dict] = []
    for item in getattr(history, "history", []) or []:
        output = getattr(item, "model_output", None)
        if output is None:
            continue
        elements = getattr(item.state, "interacted_element", None) or [None] * len(output.action)
        for action, element in zip(output.action, elements, strict=False):
            data = action.model_dump(exclude_unset=True, exclude_none=True)
            if not data:
                continue
            name, params = next(iter(data.items()))
            params = params or {}
            label = _label(element)
            if name == "click" and label:
                steps.append({"kind": "click", "label": label, "value": None})
            elif name == "input" and label:
                steps.append({"kind": "fill", "label": label, "value": params.get("text")})
            elif name == "select_dropdown" and label:
                steps.append({"kind": "select", "label": f"{label} → {params.get('text', '')}", "value": None})
            elif name == "go_back":
                steps.append({"kind": "back", "label": "back", "value": None})
            elif name == "scroll":
                steps.append({"kind": "scroll", "label": "Scroll down" if params.get("down", True) else "Scroll up",
                              "value": None})
    return steps
