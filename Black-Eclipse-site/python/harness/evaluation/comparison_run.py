"""Run the comparison task set against two harness policies and score the answers.

This is the *measurement* half of the comparison instrument: `comparison.py` scores
transcripts it is given, and this module produces them. Each side is one system prompt
plus the same model, so what is measured is the **harness policy** — the instructions a
project gives its model — not its tool plumbing. That scope is stated here because it
matters: a harness with a live tool registry can answer differently from one whose
prompt merely describes the same discipline.

Nothing is fabricated. A side that fails a call writes the failure into the transcript
where a reader can see it, and a missing transcript means the task was never run rather
than answered with nothing.

Usage::

    python python/harness/evaluation/comparison_run.py \
        --set docs/harness/comparison-task-set.json \
        --left eclipse --left-prompt prompt-eclipse.txt \
        --right vibe --right-prompt prompt-vibe.txt \
        --model deepseek-v4.1-flash:cloud --out C:/tmp/cmp
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .comparison import compare, load_task_set, render_markdown, score_directory

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_SUFFIX = "\n\nAnswer the user's request directly. Do not ask clarifying questions."
MAX_TRANSCRIPT_CHARS = 40_000


def chat(
    *,
    host: str,
    model: str,
    system: str,
    user: str,
    timeout_s: float = 180.0,
    temperature: float = 0.0,
) -> str:
    """One non-streaming chat call. Returns the assistant text or a labelled failure."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    body = json.dumps({
        "model": model,
        "stream": False,
        "options": {"temperature": temperature},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return f"[RUN FAILED: {type(exc).__name__}: {exc}]"
    message = payload.get("message") or {}
    text = str(message.get("content") or "").strip()
    return text[:MAX_TRANSCRIPT_CHARS] if text else "[RUN RETURNED NO TEXT]"


def _is_an_answer(text: str) -> bool:
    """Whether this output is a reply a reader could score.

    A bare ``<tool_call>`` block or a runner failure marker is *not* an answer: the
    model asked for a tool this comparison does not provide, or the call failed. Scoring
    those would measure the runner's plumbing, so they are kept as raw evidence and left
    out of the comparison entirely.
    """
    if not text or text.startswith("[RUN "):
        return False
    stripped = text.strip()
    if stripped.startswith("<tool_call>") and stripped.rstrip().endswith("</tool_call>"):
        return False
    return bool(stripped)


def run_side(
    task_set: Any,
    *,
    label: str,
    system_prompt: str,
    model: str,
    out_dir: Path,
    host: str = DEFAULT_HOST,
    tasks: Optional[list[str]] = None,
    timeout_s: float = 180.0,
    progress: bool = True,
) -> dict[str, str]:
    """Run every task for one side; return ``{task_id: "scored"|"not-an-answer"}``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outcomes: dict[str, str] = {}
    for task in task_set.tasks:
        if tasks and task.id not in tasks:
            continue
        if progress:
            print(f"  [{label}] {task.id} …", flush=True)
        answer = chat(
            host=host, model=model,
            system=f"{system_prompt}{DEFAULT_SUFFIX}",
            user=task.prompt, timeout_s=timeout_s,
        )
        if _is_an_answer(answer):
            (out_dir / f"{task.id}.md").write_text(answer + "\n", encoding="utf-8")
            outcomes[task.id] = "scored"
        else:
            # Keep the evidence, exclude it from the score.
            (out_dir / f"{task.id}.raw.md").write_text(answer + "\n", encoding="utf-8")
            outcomes[task.id] = "not-an-answer"
    return outcomes


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", required=True)
    parser.add_argument("--left", required=True, help="left label")
    parser.add_argument("--left-prompt", required=True, help="file with the left system prompt")
    parser.add_argument("--right", required=True, help="right label")
    parser.add_argument("--right-prompt", required=True, help="file with the right system prompt")
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    parser.add_argument("--score-only", action="store_true", help="skip the calls, score what is on disk")
    args = parser.parse_args(argv)

    task_set = load_task_set(args.set)
    root = Path(args.out)
    left_dir, right_dir = root / args.left, root / args.right
    wanted = [item for item in args.tasks.split(",") if item.strip()] or None

    if not args.score_only:
        left_prompt = Path(args.left_prompt).read_text(encoding="utf-8")
        right_prompt = Path(args.right_prompt).read_text(encoding="utf-8")
        print(f"running {len(task_set) if not wanted else len(wanted)} task(s) × 2 sides on {args.model}")
        outcomes = {}
        outcomes[args.left] = run_side(
            task_set, label=args.left, system_prompt=left_prompt, model=args.model,
            out_dir=left_dir, host=args.host, tasks=wanted, timeout_s=args.timeout)
        outcomes[args.right] = run_side(
            task_set, label=args.right, system_prompt=right_prompt, model=args.model,
            out_dir=right_dir, host=args.host, tasks=wanted, timeout_s=args.timeout)
        for label, result in outcomes.items():
            skipped = [tid for tid, state in result.items() if state != "scored"]
            print(f"  {label}: {len(result) - len(skipped)} scored, "
                  f"{len(skipped)} not-an-answer {skipped}")

    left = score_directory(task_set, left_dir)
    right = score_directory(task_set, right_dir)
    print()
    print(f"transcripts: {args.left}={len(left)} {args.right}={len(right)}")
    if not left or not right:
        print("nothing to compare: one side produced no transcript")
        return 2
    result = compare(left, right)
    print(render_markdown(result, args.left, args.right))
    (root / "scores.json").write_text(
        json.dumps({"result": result, "left": left, "right": right}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
