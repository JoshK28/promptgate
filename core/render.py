"""Prompt templates. Pure: no I/O, no AWS."""

PLACEHOLDER = "{{input}}"


def validate_template(t: str) -> None:
    if PLACEHOLDER not in t:
        raise ValueError(f"template must contain {PLACEHOLDER}")


def render_prompt(template: str, input: str) -> str:
    # Replace once; inserted input stays literal.
    return template.replace(PLACEHOLDER, input)
