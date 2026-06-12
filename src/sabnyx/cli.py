"""sabnyx — CLI entrypoint."""

from __future__ import annotations

import click

from . import __version__
from .commands.health import health_cmd
from .commands.ingest import ingest_cmd
from .commands.lint import lint_cmd
from .commands.new import new_cmd
from .commands.promote import promote_cmd
from .commands.reweave import reweave_cmd
from .commands.triage import triage_cmd


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="sabnyx")
def main() -> None:
    """sabnyx — a personal knowledge vault with a three-tier pipeline."""


main.add_command(new_cmd)
main.add_command(ingest_cmd)
main.add_command(lint_cmd)
main.add_command(triage_cmd)
main.add_command(promote_cmd)
main.add_command(reweave_cmd)
main.add_command(health_cmd)


if __name__ == "__main__":
    main()
