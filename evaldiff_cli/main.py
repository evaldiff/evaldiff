"""evaldiff — CI for LLM prompts.

Stubs a small, self-serve evaluation gate: datasets, runs, and
regression diffs exposed as a pass/fail gate for CI.
"""

from __future__ import annotations

import typer
from rich.console import Console

__version__ = "0.0.4"

app = typer.Typer(
    name="evaldiff",
    help="CI for LLM prompts: datasets, eval runs, regression diffs.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """Print the evaldiff version."""
    console.print(f"evaldiff {__version__}")


@app.command()
def init(
    name: str = typer.Option("my-dataset", help="Dataset name"),
) -> None:
    """Scaffold a dataset JSON file in the current directory."""
    import json
    import pathlib

    path = pathlib.Path(f"{name}.json")
    if path.exists():
        console.print(f"[yellow]{path} already exists, leaving untouched.[/yellow]")
        raise typer.Exit(0)
    sample = [
        {
            "input": "Refund policy for a returned item",
            "expected": "You can return any item within 30 days for a full refund.",
            "rubric": ["mentions the 30-day window", "mentions full refund"],
            "tags": ["support"],
        },
        {
            "input": "How do I reset my password?",
            "expected": "Go to Settings → Security → Reset password and follow the email link.",
            "rubric": ["mentions Settings", "mentions Security"],
            "tags": ["support"],
        },
    ]
    path.write_text(json.dumps(sample, indent=2) + "\n")
    console.print(f"[green]Wrote[/green] {path} with {len(sample)} example cases.")
    console.print("Edit it, then run [bold]evaldiff run[/bold].")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
