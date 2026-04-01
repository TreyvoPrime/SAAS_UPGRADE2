from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from modules.membercount import _count_recent_members
from modules.purge import _message_matches, _scan_limit
from modules.wiki import _trim_summary


class FakeAuthor:
    def __init__(self, author_id: int, *, bot: bool = False) -> None:
        self.id = author_id
        self.bot = bot


class FakeMessage:
    def __init__(
        self,
        *,
        author_id: int = 1,
        bot: bool = False,
        content: str = "",
        pinned: bool = False,
        attachments: list[object] | None = None,
        embeds: list[object] | None = None,
        mentions: list[object] | None = None,
        role_mentions: list[object] | None = None,
        mention_everyone: bool = False,
    ) -> None:
        self.author = FakeAuthor(author_id, bot=bot)
        self.content = content
        self.pinned = pinned
        self.attachments = attachments or []
        self.embeds = embeds or []
        self.mentions = mentions or []
        self.role_mentions = role_mentions or []
        self.mention_everyone = mention_everyone


class FakeMember:
    def __init__(self, joined_at: datetime | None) -> None:
        self.joined_at = joined_at


class FakeGuild:
    def __init__(self, members: list[FakeMember]) -> None:
        self.members = members


class PurgeHelperTests(unittest.TestCase):
    def test_message_matches_modes_and_contains(self) -> None:
        link_message = FakeMessage(content="Check https://example.com right now")
        attachment_message = FakeMessage(attachments=[object()])
        mention_message = FakeMessage(mentions=[object()])

        self.assertTrue(_message_matches(link_message, mode="links", target_user_id=None, contains=None, include_pinned=False))
        self.assertTrue(_message_matches(attachment_message, mode="attachments", target_user_id=None, contains=None, include_pinned=False))
        self.assertTrue(_message_matches(mention_message, mode="mentions", target_user_id=None, contains=None, include_pinned=False))
        self.assertTrue(_message_matches(link_message, mode="all", target_user_id=None, contains="example", include_pinned=False))
        self.assertFalse(_message_matches(link_message, mode="all", target_user_id=None, contains="missing", include_pinned=False))

    def test_message_matches_user_and_pin_rules(self) -> None:
        pinned_message = FakeMessage(author_id=44, pinned=True)

        self.assertFalse(_message_matches(pinned_message, mode="all", target_user_id=None, contains=None, include_pinned=False))
        self.assertTrue(_message_matches(pinned_message, mode="all", target_user_id=44, contains=None, include_pinned=True))
        self.assertFalse(_message_matches(pinned_message, mode="all", target_user_id=55, contains=None, include_pinned=True))

    def test_scan_limit_scales_and_caps(self) -> None:
        self.assertEqual(_scan_limit(5), 50)
        self.assertEqual(_scan_limit(25), 125)
        self.assertEqual(_scan_limit(500), 1000)


class MemberCountHelperTests(unittest.TestCase):
    def test_count_recent_members_uses_requested_window(self) -> None:
        now = datetime.now(UTC)
        guild = FakeGuild(
            [
                FakeMember(now - timedelta(hours=2)),
                FakeMember(now - timedelta(days=3)),
                FakeMember(now - timedelta(days=10)),
                FakeMember(None),
            ]
        )

        self.assertEqual(_count_recent_members(guild, days=1), 1)
        self.assertEqual(_count_recent_members(guild, days=7), 2)


class WikiHelperTests(unittest.TestCase):
    def test_trim_summary_collapses_whitespace_and_truncates(self) -> None:
        text = "This   is  a   very long sentence.\n" * 50
        result = _trim_summary(text, limit=80)

        self.assertLessEqual(len(result), 80)
        self.assertNotIn("  ", result)


if __name__ == "__main__":
    unittest.main()
