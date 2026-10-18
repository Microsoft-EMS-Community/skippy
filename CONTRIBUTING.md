# Contributing to Skippy

Thanks for helping out! Skippy is the community's follow-up and link-cleaning
Discord bot. Anyone can propose a change; maintainers review and merge, and
the bot updates itself.

## How a change reaches the bot

1. Fork the repo, make your change on a branch, and open a pull request
   against `master`.
2. GitHub runs the tests on your PR (the `tests` check). It has to pass.
3. A maintainer reviews and merges it.
4. Within about 5 minutes the server picks up `master`, runs the tests again
   in a sandbox, and restarts Skippy. The result is posted in the helper log
   channel. If Skippy doesn't stay up, it's rolled back automatically.

Changes to `requirements.txt` are never deployed automatically, because they
pull new code from PyPI. A maintainer deploys those by hand after reviewing
them. Keep every version pinned with `==`.

## Running the tests

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m unittest discover -s tests -v
```

Tests must not need the network or a Discord connection (the server runs them
with networking switched off).

## Running the bot yourself

You need your own Discord bot and test server. Set `DISCORD_BOT_TOKEN` in your
environment and point the IDs in `config.py` at your server, but don't commit
those ID changes. The database is created next to `config.py`.

## Ground rules

- Never commit tokens, secrets or databases (`secrets.env` and `*.db` are
  gitignored).
- Keep PRs small and focused, and say what the change does and why.
- `deploy/` holds reference copies of the server setup. Changing them doesn't
  change the server; mention it in your PR and a maintainer will apply it.
- Found a security problem? Don't open a public issue; contact a maintainer
  privately.
