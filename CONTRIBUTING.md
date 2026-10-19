# Contributing to Skippy

Thanks for helping out! Skippy is the Microsoft EMS Community's Discord bot
(see the [README](README.md) for what it does). Anyone can propose a change;
moderators review and merge, and the bot updates itself.

## How a change reaches the bot

1. Fork the repo, make your change on a branch, and open a pull request
   against `master`.
2. GitHub runs the tests on your PR (the `tests` check). It has to pass.
3. A moderator reviews and merges it.
4. Within about 5 minutes the server picks up `master`, runs the tests again
   in a sandbox, and restarts Skippy. The result is posted in #bot-management,
   with a ping for you if you've opted in (below). If Skippy doesn't
   stay up, it's rolled back automatically.

Changes to `requirements.txt` are never deployed automatically, because they
pull new code from PyPI. A moderator deploys those by hand after reviewing
them. Keep every version pinned with `==`.

## Get a Discord ping when your change goes live

Add yourself to `contributors.json` in your PR, and the deploy notice will
ping you when your change is live:

1. In Discord, open **User Settings → Advanced** and turn on **Developer Mode**.
2. Right-click your own name and choose **Copy User ID**.
3. Add a line with your GitHub username and that ID, both in quotes:
   `"YourGitHubName": "123456789012345678"`

The username has to be the GitHub account opening the PR; moderators check
that, so nobody can sign someone else up. Your Discord ID becomes public in
this repo. It isn't secret (anyone in the server can copy it), but only add
it if you're fine with that. To stop the pings, remove your line in a PR.

## Where things are

| File | What's in it |
|---|---|
| `bot.py` | Starts Skippy; follow-up flags, link cleaning, new-account checks |
| `commands.py` | Slash commands: follow-ups, `/help`, the fun ones, `/rsswatch` |
| `tasks.py` | Background jobs: follow-up reminders, monthly cleanup and report |
| `feeds.py`, `feed_reader.py` | `/feed` and `/blog`, and reading RSS/Atom feeds |
| `link_cleaner.py` | Stripping tracking junk, checking short links |
| `profile_links.py` | Checking authors' LinkedIn, X, YouTube and GitHub links |
| `db.py` | The SQLite database |
| `config.py` | Channel and role IDs, timings |
| `tests/` | Unit tests |
| `deploy/` | Reference copies of the server's service and auto-deploy setup |

New slash commands also go in the moderator guide in `/help`
(`moderator_help()` in `commands.py`); a test checks that every command is
listed there.

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
  change the server; mention it in your PR and a moderator will apply it.
- Found a security problem? Don't open a public issue; contact a moderator
  privately.
