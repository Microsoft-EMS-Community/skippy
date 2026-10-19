<p align="center">
  <img src="skippy-logo.png" alt="Skippy, a skeptical paperclip wearing a headset" width="160">
</p>

<h1 align="center">Skippy</h1>

<p align="center">
  The Microsoft EMS Community's own Discord bot.<br>
  Run by the server's moderators, built in the open by its members.
</p>

<p align="center">
  <a href="https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml"><img src="https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
</p>

---

## What Skippy does

### 🔴 Makes sure questions get followed up

When someone gets help and then goes quiet, a helper reacts 🔴 on their
question. Skippy replies right there to let them know, reminds them after 48
hours, and nudges them if they post elsewhere first. Once it's sorted, a
helper closes it. `/score` and `/stats` show how everyone is doing.

### 📰 Runs the community feed

New posts from members' blogs land in **#community-feed**, each with its own
discussion thread. Microsoft's blogs, service status and CVEs go to
**#ms-blogs**, **#ms-status** and **#cve**.

- Vote for your favourite posts with the :msems: reaction. Every Monday at
  09:00 the roundup in **#general** recaps the week and crowns the top 3.
- Write a blog, or know a member who does? `/blog suggest` it, and a moderator
  will add it. Your posts can link to your LinkedIn, X, YouTube and GitHub
  too (`/blog links`).
- Looking for something? `/blog search` searches everything Skippy has posted.

### 🛡️ Keeps the server safe

- Strips tracking junk from links people post and shows the clean link.
- Checks where short links lead, and blocks ones that point somewhere they
  shouldn't.
- Asks brand-new accounts to confirm they're human.

There are a few fun commands too (`/ghostcheck`, `/verdict`, `/trial`, ...).
Type **`/help`** in the server for everything you can use. Moderators get the
full guide there.

---

## Can I trust it?

- **It only has the access it needs.** Skippy has no admin rights and can't
  see private channels, apart from the two mod channels it reports in. It reads
  messages in the public channels, to clean links and spot follow-ups, but it
  doesn't keep them.
- **It keeps little, and not for long:**
  - **Follow-ups:** who was flagged, a link to the question and its first 200
    characters. Closed follow-ups are deleted after 180 days; only the counts
    behind `/score` stay.
  - **Bloggers on the feed and members on the watchlist:** when and where they
    posted (a link, never the text), so moderators can see who's active.
    Cleared out every month.
  - **Profile links** bloggers add to their posts, until they remove them or
    their blog leaves the feed.
  - **Its log of actions** is kept for 90 days, and its technical log for 7.
- **Every change is checked.** All of Skippy's code is public, right here.
  Each change is reviewed by a moderator and has to pass the automated tests
  before it goes live. If Skippy doesn't come back up properly, the change is
  rolled back on its own.
- **It runs walled off.** On the server, Skippy runs under its own locked-down
  account, kept apart from everything else.

---

## Help build Skippy

Skippy is built by the community, not by one person.

- **Got an idea or found a bug?** Tell a moderator, or
  [open an issue](https://github.com/Microsoft-EMS-Community/skippy/issues).
- **Want to change it yourself?** Open a pull request. A moderator reviews it,
  and once it's merged it's live within about 5 minutes, with a thank-you
  ping in Discord if you want one. [CONTRIBUTING.md](CONTRIBUTING.md) has
  the details.

MIT licensed, see [LICENSE](LICENSE).
