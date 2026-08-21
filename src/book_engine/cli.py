"""Command-line entry point."""

import typer

from book_engine import __version__

app = typer.Typer(no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed Book Engine version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
