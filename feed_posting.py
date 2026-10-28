# ============================================================
# feed_posting.py - How a feed's new items appear in Discord: one card
# per post, or one summary for a burst, each with its vote emoji and
# discussion thread, and archived for /blog search and the roundup.
# The cards and summaries themselves are built in feed_reader.py.
# ============================================================

import logging

import discord

import db
import feed_reader
import profile_links
from config import FEED_COMMUNITY_CHANNEL_ID, FEED_LINK_EMOJIS, FEED_THREAD_CHANNEL_IDS, FEED_VOTE_EMOJI

logger = logging.getLogger("followup-bot")

THREAD_ARCHIVE_MINUTES = 4320  # discussion threads on posts go quiet after 3 days


async def post_items(channel, feed: dict, name: str, items: list) -> set[str]:
    """Post items one by one as cards, or as one summary when there are
    more than the feed's summary_after. Archives what was posted and
    returns its keys. Posts credit the author but ping nobody."""
    if not items:
        return set()
    author_id = feed["author_id"]
    posted, archive = set(), []
    try:
        if len(items) > feed["summary_after"]:
            highlight = feed_reader.word_list(feed["highlight_words"])
            for part in feed_reader.format_summary(name, items, highlight, author_id=author_id):
                message = await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
                if not posted:  # once the first part is out, don't repeat the summary on a retry
                    posted = {i.key for i in items}
                    archive = [(i, message.jump_url) for i in items]
                    extras = await finish_post(message, f"{name} - {len(items)} new posts", summary=True)
                    logger.info("Feed #%s: posted a summary of %d items in #%s%s",
                                feed["id"], len(items), channel, extras)
        else:
            links_line = await author_links_line(author_id)
            async with feed_reader.session() as http:
                for item in items:
                    preview = await feed_reader.fetch_preview(http, item.link)
                    message = await send_card(channel, feed_reader.post_card(name, item, preview, author_id, links_line))
                    posted.add(item.key)
                    archive.append((item, message.jump_url))
                    extras = await finish_post(message, item.title)
                    logger.info("Feed #%s: posted %r in #%s%s", feed["id"], item.title[:80], channel, extras)
    except discord.HTTPException as e:
        logger.warning("Feed #%s: posting failed, retrying next check: %s", feed["id"], e)
    if archive:
        await db.archive_feed_posts([(feed["id"], name, i.title, feed_reader.clean_url(i.link), url)
                                     for i, url in archive])
    return posted


async def send_card(channel, card: discord.Embed) -> discord.Message:
    """Send a post's card. If Discord turns down the picture's address,
    the card goes without the picture rather than not at all."""
    try:
        return await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        if e.status != 400 or not card.image.url:
            raise
        logger.info("Discord refused the picture %s, posting without it: %s", card.image.url, e)
        card.set_image(url=None)
        return await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())


async def author_links_line(author_id: str | None) -> str | None:
    """The author's profile links for under their posts, if they have any."""
    if not author_id:
        return None
    return profile_links.line(await db.get_author_links(author_id), FEED_LINK_EMOJIS) or None


async def finish_post(message: discord.Message, title: str, summary: bool = False) -> str:
    """What every post gets, however it was posted: the vote emoji in the
    community channel (not on summaries: a vote there can't tell posts
    apart) and a discussion thread in thread channels. Neither failing
    counts the post as unposted. Returns a note for the log."""
    notes = []
    if message.channel.id == FEED_COMMUNITY_CHANNEL_ID and not summary:
        notes.append("vote " + ("ok" if await _add_vote(message) else "FAILED"))
    if message.channel.id in FEED_THREAD_CHANNEL_IDS:
        notes.append("thread " + ("ok" if await _open_thread(message, title) else "FAILED"))
    return f" ({', '.join(notes)})" if notes else ""


async def _add_vote(message: discord.Message) -> bool:
    """Skippy's own vote emoji on the post, so voting is a single click."""
    try:
        await message.add_reaction(FEED_VOTE_EMOJI)
        return True
    except discord.HTTPException as e:
        logger.warning("Couldn't add the vote emoji to %s: %s", message.jump_url, e)
        return False


async def _open_thread(message: discord.Message, title: str) -> bool:
    """A discussion thread on the post itself."""
    try:
        await message.create_thread(name=" ".join(title.split())[:100] or "Discussion",
                                    auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        return True
    except discord.HTTPException as e:
        logger.warning("Couldn't open a thread on %s: %s", message.jump_url, e)
        return False
