"""Example command actions for non-persistent "peek" prompts and HTML summaries."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError

from fast_agent.command_actions import PluginCommandActionContext, PluginCommandActionResult
from fast_agent.interfaces import AgentProtocol
from fast_agent.types import RequestParams
from fast_agent.ui.display_suppression import suppress_interactive_display
from fast_agent.ui.progress_display import progress_display

if TYPE_CHECKING:
    from fast_agent.types import PromptMessageExtended


async def peek(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Send a prompt, print the response, then restore the prior chat history."""
    prompt = ctx.arguments.strip()
    if not prompt:
        return PluginCommandActionResult(message="Usage: /peek <message>")

    history = list(ctx.message_history)
    try:
        progress_display.resume()
        with suppress_interactive_display():
            response = await ctx.agent.send(prompt)
    finally:
        ctx.load_message_history(history)

    return PluginCommandActionResult(markdown=response.strip() or "_No response._")


async def html_summary(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Write an HTML summary of the current conversation and return a clickable file link.

    Usage:
      /html-summary
      /html-summary frontend
      /html-summary frontend summary.html
      /html-summary --serve
      /html-summary frontend summary.html --serve --port 8765
      /html-summary html --max-chars 90000
      /html-summary html --full
      /html-summary --ask "focus on the HTML plugin design decisions"
    """
    args = _parse_html_summary_args(ctx.arguments)
    summariser = ctx.get_agent(args.agent_name) if args.agent_name else ctx.agent
    if summariser is None:
        return PluginCommandActionResult(message=f"Unknown agent: {args.agent_name}")

    target = _resolve_output_path(args.output_path, ctx.session_cwd)
    target.parent.mkdir(parents=True, exist_ok=True)

    current_history = list(ctx.message_history)
    summariser_history = list(summariser.message_history)
    template = _preferred_template(args.question) or "status_report"
    try:
        progress_display.resume()
        with suppress_interactive_display():
            response = await _send_ephemeral(
                summariser,
                _sample_summary_prompt(
                    current_history,
                    template=template,
                    max_chars=args.max_chars,
                    full=args.full,
                    question=args.question,
                ),
            )
            fragment = _extract_html_fragment(_strip_html_fence(response))
            unknown_classes = _unknown_template_classes(fragment, template)
            if unknown_classes:
                response = await _send_ephemeral(
                    summariser,
                    _sample_rewrite_prompt(
                        fragment,
                        template=template,
                        unknown_classes=unknown_classes,
                    ),
                )
                fragment = _extract_html_fragment(_strip_html_fence(response))
    finally:
        summariser.load_message_history(summariser_history)

    document = _wrap_html_document(
        fragment,
        title=_html_title(fragment, args.question),
        css=_template_css(template),
        wrap=False,
    )
    target.write_text(document, encoding="utf-8")

    uri = _served_html_uri(target, args.port) if args.serve else target.resolve().as_uri()
    return PluginCommandActionResult(markdown=f"HTML summary written: [open {target.name}]({uri})")


class Badge(BaseModel):
    text: str
    tone: Literal["neutral", "ok", "warn", "risk", "info"] = "neutral"


class Card(BaseModel):
    title: str
    desc: str
    file: str | None = None
    tone: Literal["neutral", "ok", "warn", "risk", "info"] = "neutral"


class Section(BaseModel):
    id: str
    title: str
    body: str | None = None
    cards: list[Card] = Field(default_factory=list)


class CommandItem(BaseModel):
    label: str
    command: str
    result: str | None = None


class FileItem(BaseModel):
    path: str
    note: str | None = None
    tone: Literal["neutral", "ok", "warn", "risk", "info"] = "neutral"


class RiskItem(BaseModel):
    title: str
    mitigation: str | None = None
    tone: Literal["neutral", "ok", "warn", "risk", "info"] = "risk"


class ActionItem(BaseModel):
    title: str
    owner: str | None = None
    command: str | None = None


class ToolItem(BaseModel):
    label: str
    detail: str | None = None


class ToolGroup(BaseModel):
    title: str
    open: bool = False
    items: list[ToolItem] = Field(default_factory=list)


class HtmlSummaryPayload(BaseModel):
    template: Literal[
        "session_dossier",
        "exploration_code_approaches",
        "exploration_visual_designs",
        "code_review",
        "code_understanding",
        "prototype_animation",
        "prototype_interaction",
        "slide_deck",
        "status_report",
        "incident_report",
        "research_feature_explainer",
        "research_concept_explainer",
        "implementation_plan",
        "pr_writeup",
        "editor_triage_board",
        "editor_feature_flags",
        "editor_prompt_tuner",
    ] = "session_dossier"
    title: str
    subtitle: str | None = None
    compact_note: str | None = None
    badges: list[Badge] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    commands: list[CommandItem] = Field(default_factory=list)
    files: list[FileItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    next_actions: list[ActionItem] = Field(default_factory=list)
    tool_groups: list[ToolGroup] = Field(default_factory=list)


for _model in (
    Badge,
    Card,
    Section,
    CommandItem,
    FileItem,
    RiskItem,
    ActionItem,
    ToolItem,
    ToolGroup,
    HtmlSummaryPayload,
):
    _model.model_rebuild(
        _types_namespace={
            "Literal": Literal,
            "Badge": Badge,
            "Card": Card,
            "Section": Section,
            "CommandItem": CommandItem,
            "FileItem": FileItem,
            "RiskItem": RiskItem,
            "ActionItem": ActionItem,
            "ToolItem": ToolItem,
            "ToolGroup": ToolGroup,
        }
    )


class HtmlSummaryArgs:
    __slots__ = (
        "agent_name",
        "full",
        "max_chars",
        "output_path",
        "port",
        "question",
        "serve",
    )

    def __init__(
        self,
        agent_name: str | None,
        output_path: str | None,
        serve: bool,
        port: int | None,
        max_chars: int,
        full: bool,
        question: str | None,
    ) -> None:
        self.agent_name = agent_name
        self.output_path = output_path
        self.serve = serve
        self.port = port
        self.max_chars = max_chars
        self.full = full
        self.question = question


def _parse_html_summary_args(arguments: str) -> HtmlSummaryArgs:
    parts = shlex.split(arguments)
    serve = True
    full = False
    port: int | None = None
    max_chars = 60000
    question: str | None = None
    positional: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--serve":
            serve = True
        elif part == "--no-serve":
            serve = False
        elif part == "--port":
            index += 1
            if index >= len(parts):
                raise ValueError(
                    "Usage: /html-summary [agent-name] [output.html] [--serve] [--port N]"
                )
            port = int(parts[index])
            serve = True
        elif part == "--max-chars":
            index += 1
            if index >= len(parts):
                raise ValueError("Usage: /html-summary [agent-name] [output.html] [--max-chars N]")
            max_chars = int(parts[index])
        elif part == "--full":
            full = True
        elif part in {"--ask", "--question", "--focus"}:
            index += 1
            if index >= len(parts):
                raise ValueError("Usage: /html-summary [agent-name] [output.html] [--ask QUESTION]")
            question = parts[index]
        else:
            positional.append(part)
        index += 1

    parts = positional
    if not parts:
        return HtmlSummaryArgs("html", None, serve, port, max_chars, full, question)
    if len(parts) == 1:
        if parts[0].endswith((".html", ".htm")):
            return HtmlSummaryArgs("html", parts[0], serve, port, max_chars, full, question)
        return HtmlSummaryArgs(parts[0], None, serve, port, max_chars, full, question)
    return HtmlSummaryArgs(parts[0], parts[1], serve, port, max_chars, full, question)


async def _send_ephemeral(agent: object, prompt: str) -> str:
    """Send without adding the summarisation prompt to the helper's normal context."""
    if isinstance(agent, AgentProtocol):
        response = await agent.generate(prompt, RequestParams(use_history=False))
        return response.last_text() or ""
    return await agent.send(prompt)


def _resolve_output_path(output_path: str | None, session_cwd: Path | None) -> Path:
    base = session_cwd or Path.cwd()
    if output_path:
        path = Path(output_path).expanduser()
        return path if path.is_absolute() else base / path

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / ".fast-agent" / "summaries" / f"conversation-summary-{stamp}.html"


def _served_html_uri(target: Path, requested_port: int | None) -> str:
    host = "127.0.0.1"
    directory = target.resolve().parent
    port = _ensure_summary_server(directory, host=host, requested_port=requested_port)
    from urllib.parse import quote

    return f"http://{host}:{port}/{quote(target.name)}"


def _ensure_summary_server(directory: Path, *, host: str, requested_port: int | None) -> int:
    state_path = directory / ".fast-agent-summary-server.json"
    state = _read_server_state(state_path)
    if (
        state
        and state.get("directory") == str(directory)
        and isinstance(state.get("port"), int)
        and isinstance(state.get("pid"), int)
        and (requested_port is None or state["port"] == requested_port)
        and _pid_running(state["pid"])
    ):
        return state["port"]

    port = requested_port or _free_port(host)
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", host],
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state_path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "host": host,
                "port": port,
                "directory": str(directory),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return port


def _read_server_state(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _summary_prompt(
    history: list[PromptMessageExtended],
    *,
    max_chars: int = 60000,
    full: bool = False,
    question: str | None = None,
) -> str:
    message_text_limit = 20000 if full else 4000
    messages = [
        _message_payload(index, message, text_limit=message_text_limit)
        for index, message in enumerate(history)
    ]
    original_count = len(messages)
    if not full:
        messages = _compact_messages(messages, max_chars=max_chars)
    payload = {
        "conversation": messages,
        "extracted_context": _extracted_context(messages),
        "source_stats": {
            "original_message_count": original_count,
            "included_message_count": len(messages),
            "compacted": len(messages) < original_count,
            "max_chars": None if full else max_chars,
        },
        "hints": {
            "rendering": "standalone local HTML",
            "interactivity": "vanilla JavaScript is allowed; prefer details/search/filter/tabs/copy buttons over frameworks",
            "assets": "use relative links for any local assets; do not invent files that were not provided",
            "performance": "optimise for a fast model: use extracted_context as an index, then verify against conversation",
            "user_question": question,
        },
    }

    question_instruction = (
        "The user supplied this specific focus/question. Make it the organizing thesis of the JSON payload: "
        f"{question}\n{_template_hint(question)}"
        if question
        else ""
    )

    return (
        "Create structured JSON for the fast-agent deterministic HTML renderer.\n"
        f"{question_instruction}"
        "Return JSON only: no Markdown fences, no commentary, no HTML, no CSS, no scripts.\n"
        "Choose one template: session_dossier, status_report, code_review, "
        "implementation_plan, incident_report.\n"
        "Match this schema exactly enough for validation:\n"
        "{template,title,subtitle,compact_note,badges:[{text,tone}],"
        "sections:[{id,title,body,cards:[{title,desc,file,tone}]}],"
        "commands:[{label,command,result}],files:[{path,note,tone}],"
        "risks:[{title,mitigation,tone}],next_actions:[{title,owner,command}],"
        "tool_groups:[{title,open,items:[{label,detail}]}]}.\n"
        "Allowed tone values: neutral, ok, warn, risk, info.\n"
        "Make the summary useful as a project/session dashboard with:\n"
        "- an executive summary\n"
        "- current goal/status\n"
        "- key decisions and constraints\n"
        "- files, commands, URLs, and tools mentioned or used\n"
        "- notable tool calls/results, grouped and collapsible\n"
        "- risks/open questions\n"
        "- concrete next actions\n"
        "If source_stats.compacted is true, set compact_note to one short sentence. "
        "Do not make compaction a risk or executive-summary item.\n"
        "Use extracted_context as a fast index of likely-important entities. Verify details against conversation_json before presenting them as facts.\n"
        "Preserve important technical details, paths, commands, errors, and links.\n\n"
        "<conversation_json>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "</conversation_json>"
    )


def _sample_summary_prompt(
    history: list[PromptMessageExtended],
    *,
    template: str,
    max_chars: int = 60000,
    full: bool = False,
    question: str | None = None,
) -> str:
    message_text_limit = 20000 if full else 4000
    messages = [
        _message_payload(index, message, text_limit=message_text_limit)
        for index, message in enumerate(history)
    ]
    original_count = len(messages)
    if not full:
        messages = _compact_messages(messages, max_chars=max_chars)

    payload = {
        "conversation": messages,
        "extracted_context": _extracted_context(messages),
        "source_stats": {
            "original_message_count": original_count,
            "included_message_count": len(messages),
            "compacted": len(messages) < original_count,
            "max_chars": None if full else max_chars,
        },
        "user_question": question,
    }
    sample = _template_sample(template)
    allowed_classes = ", ".join(_template_class_names(sample))
    return (
        "Create an HTML body fragment for the fast-agent summary viewer.\n"
        f"Chosen upstream artifact template: {template}.\n"
        f"User focus/question: {question or '(general status summary)'}\n\n"
        "Use the reference artifact below as the visual and structural model. "
        "Reproduce its page shape and class names with new factual content from "
        "conversation_json. Do not combine it with other upstream examples.\n"
        f"Allowed CSS classes for this template: {allowed_classes}.\n"
        "Use only those class names. Do not use generic classes from other templates "
        "such as grid, card, summary-band, stat-card, t-body, t-small, pill, "
        "compact-note, module-page, item, or list unless they appear in the allowed "
        "class list above.\n"
        "Return HTML markup only: no Markdown fences, no commentary, no <!doctype>, "
        "no <html>, no <head>, no <style>, no <script>, and no remote dependencies.\n"
        "Preserve exact paths, commands, errors, URLs, decisions, and next actions. "
        'Do not invent facts. Long commands must be in <pre class="code"><code>...</code></pre>.\n\n'
        "<reference_artifact>\n"
        f"{sample}\n"
        "</reference_artifact>\n\n"
        "<conversation_json>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "</conversation_json>"
    )


def _sample_rewrite_prompt(
    fragment: str,
    *,
    template: str,
    unknown_classes: set[str],
) -> str:
    sample = _template_sample(template)
    allowed_classes = ", ".join(_template_class_names(sample))
    unknown = ", ".join(sorted(unknown_classes))
    return (
        "Rewrite this HTML body fragment to match the selected upstream artifact.\n"
        f"Template: {template}\n"
        f"Unknown/disallowed classes used: {unknown}\n"
        f"Allowed CSS classes: {allowed_classes}\n"
        "Preserve the factual content, but use only the structure and classes in "
        "the reference artifact. Do not use CSS classes from other examples. "
        "Return HTML fragment only: no Markdown fences, no commentary, no style/script.\n\n"
        "<reference_artifact>\n"
        f"{sample}\n"
        "</reference_artifact>\n\n"
        "<fragment_to_rewrite>\n"
        f"{fragment}\n"
        "</fragment_to_rewrite>"
    )


def _template_hint(question: str) -> str:
    normalized = question.lower()
    if any(
        word in normalized for word in ("module", "architecture", "flow", "explainer", "exapliner")
    ):
        return "Template hint: use template code_understanding.\n"
    if any(word in normalized for word in ("pr", "pull request", "writeup")):
        return "Template hint: use template pr_writeup.\n"
    if any(word in normalized for word in ("review", "diff")):
        return "Template hint: use template code_review.\n"
    if any(word in normalized for word in ("incident", "failure", "regression")):
        return "Template hint: use template incident_report.\n"
    if any(word in normalized for word in ("plan", "roadmap", "milestone")):
        return "Template hint: use template implementation_plan.\n"
    if any(word in normalized for word in ("status", "progress", "blocker")):
        return "Template hint: use template status_report.\n"
    if any(word in normalized for word in ("triage", "board", "backlog")):
        return "Template hint: use template editor_triage_board.\n"
    return ""


def _format_message(message: PromptMessageExtended) -> str:
    text = message.all_text().strip()
    if not text:
        return ""
    return f"{message.role}: {text}"


def _message_payload(
    index: int,
    message: PromptMessageExtended,
    *,
    text_limit: int,
) -> dict[str, object]:
    text = message.all_text()
    payload: dict[str, object] = {
        "index": index,
        "role": str(message.role),
        "text": _truncate_text(text, text_limit),
    }
    if len(text) > text_limit:
        payload["text_truncated_chars"] = len(text) - text_limit
    if message.timestamp is not None:
        payload["timestamp"] = message.timestamp.isoformat()
    if message.stop_reason is not None:
        payload["stop_reason"] = str(message.stop_reason)
    if message.tool_calls:
        payload["tool_calls"] = [_safe_model_dump(call) for call in message.tool_calls.values()]
    if message.tool_results:
        payload["tool_results"] = [
            _safe_model_dump(result) for result in message.tool_results.values()
        ]
    return payload


def _compact_messages(
    messages: list[dict[str, object]],
    *,
    max_chars: int,
) -> list[dict[str, object]]:
    if max_chars <= 0:
        return messages
    encoded = json.dumps(messages, ensure_ascii=False)
    if len(encoded) <= max_chars:
        return messages

    head_count = min(4, len(messages))
    head = messages[:head_count]
    tail: list[dict[str, object]] = []
    budget = max_chars - len(json.dumps(head, ensure_ascii=False)) - 800
    for message in reversed(messages[head_count:]):
        candidate = [message, *tail]
        if len(json.dumps(candidate, ensure_ascii=False)) > max(1000, budget):
            break
        tail.insert(0, message)
    omitted = max(0, len(messages) - len(head) - len(tail))
    if omitted:
        marker: dict[str, object] = {
            "index": "compaction-marker",
            "role": "system",
            "text": f"[{omitted} middle messages omitted to fit the HTML agent context window.]",
            "omitted_message_count": omitted,
        }
        return [*head, marker, *tail]
    return [*head, *tail]


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return text[:head] + f"\n\n[... {len(text) - limit} characters omitted ...]\n\n" + text[-tail:]


def _safe_model_dump(value: object) -> object:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else str(dumped)
    return str(value)


def _extracted_context(messages: list[dict[str, object]]) -> dict[str, object]:
    text = "\n".join(str(message.get("text", "")) for message in messages)
    tool_names: list[str] = []
    for message in messages:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            params = call.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                tool_names.append(params["name"])

    return {
        "paths": _unique_matches(
            r"(?<![\w.-])(?:\.?\.?/|/|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./@+%=-]+(?:\.[A-Za-z0-9]+)?",
            text,
        )[:80],
        "commands": _unique_matches(
            r"(?m)^\s*(?:uv|python|pytest|ruff|ty|git|rg|find|cat|sed|awk|npm|pnpm|yarn|node|bash|sh)\b[^\n]*",
            text,
        )[:50],
        "urls": _unique_matches(r"https?://[^\s)>'\"]+", text)[:40],
        "tool_names": sorted(set(tool_names)),
        "error_lines": _unique_matches(
            r"(?im)^.*(?:error|failed|failure|traceback|exception|warning)[: ].*$",
            text,
        )[:40],
    }


def _unique_matches(pattern: str, text: str) -> list[str]:
    seen: set[str] = set()
    matches: list[str] = []
    for match in re.finditer(pattern, text):
        value = match.group(0).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        matches.append(value)
    return matches


def _summary_payload_from_response(response: str, *, question: str | None) -> HtmlSummaryPayload:
    text = _strip_json_fence(response)
    try:
        data = json.loads(text)
        payload = HtmlSummaryPayload.model_validate(data)
        preferred = _preferred_template(question)
        if preferred is not None:
            payload.template = preferred
        return payload
    except (json.JSONDecodeError, ValidationError):
        title = (
            f"fast-agent summary — {question[:80]}" if question else "fast-agent session summary"
        )
        return HtmlSummaryPayload(
            title=title,
            subtitle="The summarizer did not return valid schema JSON; raw output is shown as escaped text.",
            badges=[Badge(text="schema fallback", tone="warn")],
            sections=[
                Section(
                    id="raw-output",
                    title="Raw summarizer output",
                    body=response.strip() or "No summary content was generated.",
                )
            ],
        )


def _preferred_template(question: str | None) -> str | None:
    if not question:
        return None
    match = re.search(r"Template hint: use template ([a-z_]+)", _template_hint(question))
    return match.group(1) if match else None


def _template_sample(template: str) -> str:
    try:
        text = _template_file(template).read_text(encoding="utf-8")
    except OSError:
        text = _fallback_template_sample()
    # Keep the sample bounded but still include the complete body/class vocabulary
    # for the fast summarizer. Most upstream artifacts are comfortably below this.
    return _truncate_text(text, 36000)


def _template_class_names(sample: str) -> list[str]:
    names: set[str] = set()
    for match in re.finditer(r"class=[\"']([^\"']+)[\"']", sample):
        names.update(part for part in match.group(1).split() if part)
    return sorted(names)


def _unknown_template_classes(fragment: str, template: str) -> set[str]:
    allowed = set(_template_class_names(_template_sample(template)))
    used: set[str] = set()
    for match in re.finditer(r"class=[\"']([^\"']+)[\"']", fragment):
        used.update(part for part in match.group(1).split() if part)
    return used - allowed


def _template_css(template: str) -> str:
    try:
        text = _template_file(template).read_text(encoding="utf-8")
    except OSError:
        return _viewer_css()
    match = re.search(r"<style>\s*(.*?)\s*</style>", text, flags=re.DOTALL | re.I)
    if match is None:
        return _viewer_css()
    return match.group(1).rstrip()


def _template_file(template: str) -> Path:
    upstream = Path(__file__).resolve().parents[2] / ".cdx" / "html-effectiveness" / "upstream"
    name = {
        "exploration_code_approaches": "01-exploration-code-approaches.html",
        "exploration_visual_designs": "02-exploration-visual-designs.html",
        "code_review": "03-code-review-pr.html",
        "code_understanding": "04-code-understanding.html",
        "prototype_animation": "07-prototype-animation.html",
        "prototype_interaction": "08-prototype-interaction.html",
        "slide_deck": "09-slide-deck.html",
        "status_report": "11-status-report.html",
        "incident_report": "12-incident-report.html",
        "research_feature_explainer": "14-research-feature-explainer.html",
        "research_concept_explainer": "15-research-concept-explainer.html",
        "implementation_plan": "16-implementation-plan.html",
        "pr_writeup": "17-pr-writeup.html",
        "editor_triage_board": "18-editor-triage-board.html",
        "editor_feature_flags": "19-editor-feature-flags.html",
        "editor_prompt_tuner": "20-editor-prompt-tuner.html",
        "session_dossier": "11-status-report.html",
    }.get(template, "11-status-report.html")
    return upstream / name


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.I)
    if match:
        return match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def render_document(payload: HtmlSummaryPayload) -> str:
    if payload.template == "code_understanding":
        return _wrap_html_document(render_code_understanding(payload), title=payload.title)
    return _wrap_html_document(render_session_dossier(payload), title=payload.title)


def render_session_dossier(payload: HtmlSummaryPayload) -> str:
    parts = [
        render_header(payload),
        render_compact_note(payload),
        *(render_section(section) for section in payload.sections),
        render_files(payload.files),
        render_commands(payload.commands),
        render_risks(payload.risks),
        render_next_actions(payload.next_actions),
        render_tool_groups(payload.tool_groups),
    ]
    return "\n".join(part for part in parts if part)


def render_code_understanding(payload: HtmlSummaryPayload) -> str:
    """Render a module explainer using the 04-code-understanding artifact shape."""
    steps: list[str] = []
    for index, section in enumerate(payload.sections, start=1):
        body = html.escape(section.body or "")
        cards = "".join(
            f"<p><code>{html.escape(card.file or card.title)}</code> — {html.escape(card.desc)}</p>"
            for card in section.cards
        )
        hot = " hot" if index == 1 else ""
        steps.append(
            f'<div class="module-step{hot}">'
            f'<div class="step-badge">{index}</div>'
            '<div class="step-body">'
            f'<div class="step-loc">{html.escape(section.title)}</div>'
            f"<p>{body}</p>{cards}"
            "</div></div>"
        )

    files = "".join(
        f'<li><span class="path">{html.escape(item.path)}</span>'
        f'<span class="desc">{html.escape(item.note or "Referenced file")}</span></li>'
        for item in payload.files
    )
    if not files:
        files = "".join(
            f'<li><span class="path">{html.escape(card.file)}</span>'
            f'<span class="desc">{html.escape(card.desc)}</span></li>'
            for section in payload.sections
            for card in section.cards
            if card.file
        )

    risks = "".join(
        f"<li>{html.escape(risk.title)}"
        + (f" — {html.escape(risk.mitigation)}" if risk.mitigation else "")
        + "</li>"
        for risk in payload.risks
    )
    commands = "".join(render_command(command) for command in payload.commands)
    tool_groups = render_tool_groups(payload.tool_groups)
    subtitle = payload.subtitle or "Module explainer generated from the current conversation."

    return (
        '<div class="module-page">'
        "<header>"
        '<div class="repo-line">fast-agent · module explainer</div>'
        f"<h1>{html.escape(payload.title)}</h1>"
        f'<p class="summary">{html.escape(subtitle)}</p>'
        "</header>"
        "<main>"
        '<h2 style="margin-top:0">Module map</h2>'
        f"{render_module_diagram(payload)}"
        "<h2>Walkthrough</h2>"
        f"{''.join(steps)}"
        f"{('<h2>Commands</h2><div class="list">' + commands + '</div>') if commands else ''}"
        f"{tool_groups}"
        "</main>"
        "<aside>"
        f"{('<div class="panel"><h3>Key files</h3><ul class="key-files">' + files + '</ul></div>') if files else ''}"
        f"{('<div class="gotchas"><h3>Risks / gotchas</h3><ul>' + risks + '</ul></div>') if risks else ''}"
        "</aside>"
        "</div>"
    )


def render_module_diagram(payload: HtmlSummaryPayload) -> str:
    labels = [section.title for section in payload.sections[:5]]
    if not labels:
        labels = ["Input", "Summarizer", "Schema", "Renderer", "HTML"]
    width = max(720, len(labels) * 150)
    boxes: list[str] = []
    arrows: list[str] = []
    for index, label in enumerate(labels):
        x = 28 + index * 138
        boxes.append(
            f'<g><rect class="box{" hot" if index == 0 else ""}" x="{x}" y="46" '
            'width="112" height="58" rx="10"></rect>'
            f'<text x="{x + 56}" y="76" text-anchor="middle">{html.escape(label[:18])}</text></g>'
        )
        if index:
            arrows.append(
                f'<line class="arrow" x1="{x - 26}" y1="75" x2="{x}" y2="75" marker-end="url(#moduleArrow)"></line>'
            )
    return (
        '<div class="diagram-panel"><svg class="flow" '
        f'viewBox="0 0 {width} 150" width="{width}" height="150" role="img" '
        'aria-label="Module flow diagram">'
        '<defs><marker id="moduleArrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#87867F"></path>'
        "</marker></defs>"
        f"{''.join(boxes)}{''.join(arrows)}</svg></div>"
    )


def render_header(payload: HtmlSummaryPayload) -> str:
    subtitle = (
        payload.subtitle or "Structured session dossier generated from the current conversation."
    )
    badges = "".join(
        f'<span class="badge {_tone_class(badge.tone)}">{html.escape(badge.text)}</span>'
        for badge in payload.badges
    )
    badge_block = f'<div class="badges">{badges}</div>' if badges else ""
    return (
        "<header>\n"
        f"  <h1>{html.escape(payload.title)}</h1>\n"
        f'  <p class="sub">{html.escape(subtitle)}</p>\n'
        f"  {badge_block}\n"
        "</header>"
    )


def render_compact_note(payload: HtmlSummaryPayload) -> str:
    if not payload.compact_note:
        return ""
    return f'<p class="compact-note">{html.escape(payload.compact_note)}</p>'


def render_section(section: Section) -> str:
    body = f'<p class="t-body">{html.escape(section.body)}</p>' if section.body else ""
    cards = "".join(render_card(card) for card in section.cards)
    grid = f'<div class="grid">{cards}</div>' if cards else ""
    return (
        f'<section id="{_html_id(section.id)}">\n'
        f"  <h2>{html.escape(section.title)}</h2>\n"
        '  <hr class="rule">\n'
        f"  {body}\n"
        f"  {grid}\n"
        "</section>"
    )


def render_card(card: Card) -> str:
    file_line = (
        f'<div class="file"><span>{html.escape(card.file)}</span><span>→</span></div>'
        if card.file
        else ""
    )
    return (
        f'<article class="card {_tone_class(card.tone)}">'
        '<div class="body">'
        f'<div class="title">{html.escape(card.title)}</div>'
        f'<div class="desc">{html.escape(card.desc)}</div>'
        f"{file_line}</div></article>"
    )


def render_files(files: list[FileItem]) -> str:
    if not files:
        return ""
    cards = "".join(
        render_card(
            Card(
                title=item.path, desc=item.note or "Referenced file", file=item.path, tone=item.tone
            )
        )
        for item in files
    )
    return (
        '<section id="files"><h2>Files</h2><hr class="rule">'
        f'<div class="grid">{cards}</div></section>'
    )


def render_commands(commands: list[CommandItem]) -> str:
    if not commands:
        return ""
    items = "".join(render_command(command) for command in commands)
    return (
        '<section id="commands"><h2>Commands</h2><hr class="rule">'
        f'<div class="list">{items}</div></section>'
    )


def render_command(command: CommandItem) -> str:
    result = f'<p class="t-small">{html.escape(command.result)}</p>' if command.result else ""
    escaped_command = html.escape(command.command)
    return (
        '<div class="item">'
        f"<p><strong>{html.escape(command.label)}</strong></p>"
        f'<pre class="code"><code>{escaped_command}</code></pre>'
        f'<button class="button btn" data-copy="{html.escape(command.command, quote=True)}">Copy</button>'
        f"{result}</div>"
    )


def render_risks(risks: list[RiskItem]) -> str:
    if not risks:
        return ""
    cards = "".join(
        render_card(
            Card(
                title=risk.title,
                desc=risk.mitigation or "Mitigation not yet captured.",
                tone=risk.tone,
            )
        )
        for risk in risks
    )
    return (
        '<section id="risks"><h2>Risks</h2><hr class="rule"><div class="grid">'
        + cards
        + "</div></section>"
    )


def render_next_actions(actions: list[ActionItem]) -> str:
    if not actions:
        return ""
    items: list[str] = []
    for action in actions:
        owner = f'<p class="t-small">Owner: {html.escape(action.owner)}</p>' if action.owner else ""
        command = (
            f'<pre class="code"><code>{html.escape(action.command)}</code></pre>'
            if action.command
            else ""
        )
        items.append(
            f'<div class="item"><p><strong>{html.escape(action.title)}</strong></p>{owner}{command}</div>'
        )
    return (
        '<section id="next-actions"><h2>Next actions</h2><hr class="rule"><div class="list">'
        + "".join(items)
        + "</div></section>"
    )


def render_tool_groups(groups: list[ToolGroup]) -> str:
    if not groups:
        return ""
    details = []
    for group in groups:
        items = "".join(
            f'<div class="item"><p><strong>{html.escape(item.label)}</strong></p>'
            f'<p class="t-small">{html.escape(item.detail or "")}</p></div>'
            for item in group.items
        )
        open_attr = " open" if group.open else ""
        details.append(
            f"<details{open_attr}><summary>{html.escape(group.title)}</summary>"
            f'<div class="list">{items}</div></details>'
        )
    return (
        '<section id="tooling"><h2>Tooling</h2><hr class="rule">' + "".join(details) + "</section>"
    )


def _tone_class(tone: Literal["neutral", "ok", "warn", "risk", "info"]) -> str:
    return "" if tone == "neutral" else tone


def _html_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return html.escape(slug or "section", quote=True)


def _strip_html_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:html)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.I)
    return match.group(1).strip() if match else stripped


def _extract_html_fragment(text: str) -> str:
    stripped = text.strip()
    body_match = re.search(r"<body[^>]*>(.*?)</body>", stripped, flags=re.DOTALL | re.I)
    if body_match:
        stripped = body_match.group(1).strip()
    stripped = re.sub(r"<!doctype[^>]*>", "", stripped, flags=re.I).strip()
    stripped = re.sub(r"</?html[^>]*>", "", stripped, flags=re.I).strip()
    stripped = re.sub(r"<head[^>]*>.*?</head>", "", stripped, flags=re.DOTALL | re.I).strip()
    stripped = re.sub(r"<style[^>]*>.*?</style>", "", stripped, flags=re.DOTALL | re.I).strip()
    stripped = re.sub(r"<script[^>]*>.*?</script>", "", stripped, flags=re.DOTALL | re.I).strip()
    return stripped or "<main><p>No summary content was generated.</p></main>"


def _html_title(fragment: str, question: str | None) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", fragment, flags=re.DOTALL | re.I)
    if match:
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if text:
            return html.unescape(text)
    if question:
        return f"fast-agent summary — {question[:80]}"
    return "fast-agent session summary"


def _wrap_html_document(
    fragment: str,
    *,
    title: str,
    css: str | None = None,
    wrap: bool = True,
) -> str:
    body = f'<div class="wrap">\n{fragment}\n</div>' if wrap else fragment
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<style>\n"
        f"{css if css is not None else _viewer_css()}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "<script>\n"
        f"{_viewer_js()}\n"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


def _viewer_css() -> str:
    for css_path in (
        Path(__file__).with_name("viewer.css"),
        Path(__file__).resolve().parents[2] / ".cdx" / "html-effectiveness" / "viewer.css",
    ):
        try:
            return css_path.read_text(encoding="utf-8")
        except OSError:
            continue
    return _fallback_viewer_css()


def _fallback_template_sample() -> str:
    return """
<main class="page">
  <header>
    <h1>Session summary</h1>
    <p class="sub">Conversation dashboard</p>
  </header>
  <section>
    <h2>Executive summary</h2>
    <hr class="rule">
    <p class="t-body">Summarise the current state, decisions, files, commands, risks, and next actions.</p>
    <div class="list">
      <div class="item">
        <p><strong>Command</strong></p>
        <pre class="code"><code>uv run scripts/lint.py</code></pre>
        <button class="button btn">Copy</button>
      </div>
    </div>
  </section>
</main>
"""


def _fallback_viewer_css() -> str:
    return """:root { --ivory:#FAF9F5; --paper:#FFFFFF; --slate:#141413; --clay:#D97757; --clay-d:#B85C3E; --oat:#E3DACC; --olive:#788C5D; --g100:#F0EEE6; --g200:#E6E3DA; --g300:#D1CFC5; --g500:#87867F; --g700:#3D3D3A; --serif:ui-serif, Georgia, serif; --sans:system-ui, sans-serif; --mono:ui-monospace, Menlo, monospace; } *{box-sizing:border-box} body{margin:0;background:var(--ivory);color:var(--slate);font-family:var(--sans);line-height:1.55}.wrap{max-width:1120px;margin:0 auto;padding:0 32px 140px}.masthead{padding:80px 0 56px;border-bottom:1.5px solid var(--g300);margin-bottom:12px}.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--g500)}h1{font-family:var(--serif);font-weight:500;font-size:clamp(38px,5.4vw,62px);line-height:1.06;letter-spacing:-.018em}.intro{font-size:16.5px;color:var(--g700);max-width:620px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(316px,1fr));gap:20px}.card{background:var(--paper);border:1.5px solid var(--g300);border-radius:14px;padding:18px}.sec-head{display:flex;align-items:baseline;gap:16px;margin:72px 0 10px}.sec-head .idx{font-family:var(--mono);font-size:13px;color:var(--clay);font-weight:600;width:34px}.sec-head h2{font-family:var(--serif);font-weight:500;font-size:27px;margin:0}.sec-intro{font-size:14.5px;color:var(--g700);max-width:700px;margin:0 0 24px 50px}"""


def _viewer_js() -> str:
    return """
function initDashboard() {
  const q = document.querySelector('[data-filter]');
  if (q) q.addEventListener('input', () => {
    const term = q.value.trim().toLowerCase();
    document.querySelectorAll('[data-search]').forEach(el => {
      el.classList.toggle('hidden', Boolean(term) && !el.dataset.search.toLowerCase().includes(term));
    });
  });
  document.querySelectorAll('[data-expand]').forEach(btn => {
    btn.addEventListener('click', () => document.querySelectorAll('details').forEach(d => d.open = true));
  });
  document.querySelectorAll('[data-collapse]').forEach(btn => {
    btn.addEventListener('click', () => document.querySelectorAll('details').forEach(d => d.open = false));
  });
  document.querySelectorAll('[data-copy]').forEach(btn => {
    btn.addEventListener('click', async () => {
      await navigator.clipboard?.writeText(btn.getAttribute('data-copy') || '');
      const old = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = old, 900);
    });
  });
  document.querySelectorAll('[data-tab-group]').forEach(group => {
    const tabs = group.querySelectorAll('[role="tab"]');
    const panels = group.querySelectorAll('[role="tabpanel"]');
    tabs.forEach(tab => tab.addEventListener('click', () => {
      tabs.forEach(t => t.setAttribute('aria-selected', String(t === tab)));
      panels.forEach(panel => panel.classList.toggle('hidden', panel.id !== tab.getAttribute('aria-controls')));
    }));
  });
}
document.addEventListener('DOMContentLoaded', initDashboard);
"""
