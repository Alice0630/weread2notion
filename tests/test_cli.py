import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from weread2notion import cli


class CliTestCase(unittest.TestCase):
    def setUp(self):
        cli.title_property_name = "书名"
        cli.skipped_property_names = set()
        cli.data_source_id = "data-source-id"
        cli.data_source_property_types = {
            "书名": "title",
            "BookId": "rich_text",
            "ISBN": "rich_text",
            "链接": "url",
            "Sort": "number",
            "评分": "number",
            "阅读状态": "status",
            "阅读时长": "number",
            "微信读书进度": "number",
            "时间": "date",
            "最后阅读时间": "date",
        }

    def test_build_book_properties_matches_existing_database(self):
        properties = cli.build_book_properties(
            "测试书",
            "book-id",
            123,
            "9780000000000",
            8.6,
            {
                "markedStatus": 4,
                "readingTime": 23819,
                "readingProgress": 0.2,
                "finishedDate": 1754352000,
                "lastReadDate": 1754438400,
            },
        )

        self.assertEqual(properties["阅读状态"], {"status": {"name": "已读"}})
        self.assertEqual(properties["阅读时长"], {"number": 23819})
        self.assertEqual(properties["微信读书进度"], {"number": 0.2})
        self.assertEqual(properties["BookId"]["rich_text"][0]["text"]["content"], "book-id")
        self.assertIn("时间", properties)
        self.assertIn("最后阅读时间", properties)

    def test_unfinished_book_does_not_write_completed_date(self):
        properties = cli.build_book_properties(
            "在读书",
            "book-id",
            123,
            "",
            None,
            {
                "markedStatus": 2,
                "readingTime": 60,
                "readingProgress": 0.1,
                "finishedDate": None,
                "lastReadDate": 1754438400,
            },
        )

        self.assertEqual(properties["阅读状态"], {"status": {"name": "在读"}})
        self.assertNotIn("时间", properties)

    def test_normalize_reading_progress(self):
        self.assertEqual(cli.normalize_reading_progress(20), 0.2)
        self.assertEqual(cli.normalize_reading_progress(0.2), 0.2)
        self.assertEqual(cli.normalize_reading_progress(150), 1)

    def test_content_refresh_uses_sort_but_properties_can_update_daily(self):
        existing_page = {"id": "page-id"}
        self.assertFalse(
            cli.should_refresh_content(100, 100, False, existing_page)
        )
        self.assertTrue(
            cli.should_refresh_content(101, 100, False, existing_page)
        )
        self.assertTrue(cli.should_refresh_content(100, 100, True, existing_page))
        self.assertTrue(cli.should_refresh_content(100, 100, False, None))

    @patch.dict(os.environ, {"WEREAD_BOOK_ID": "target-book"}, clear=False)
    def test_optional_book_filter(self):
        self.assertTrue(cli.matches_book_filter("target-book"))
        self.assertFalse(cli.matches_book_filter("other-book"))

    @patch.object(cli, "query_data_source")
    def test_find_book_page_returns_existing_page_without_deleting(self, query):
        query.return_value = {"results": [{"id": "page-id"}]}

        page = cli.find_book_page("book-id")

        self.assertEqual(page["id"], "page-id")
        query.assert_called_once_with(
            filter={"property": "BookId", "rich_text": {"equals": "book-id"}},
            page_size=2,
        )

    @patch.object(cli, "get_read_info")
    def test_upsert_updates_existing_page_in_place(self, get_read_info):
        get_read_info.return_value = {
            "markedStatus": 2,
            "readingTime": 60,
            "readingProgress": 0.1,
            "finishedDate": None,
            "lastReadDate": None,
        }
        pages = SimpleNamespace(update=Mock(), create=Mock())
        cli.client = SimpleNamespace(pages=pages)

        page_id, existed = cli.upsert_to_notion(
            "测试书", "book-id", "", 123, "", None, {"id": "page-id"}
        )

        self.assertEqual(page_id, "page-id")
        self.assertTrue(existed)
        pages.update.assert_called_once()
        pages.create.assert_not_called()

    @patch.dict(os.environ, {"NOTION_EXISTING_PAGE_MODE": "preserve"}, clear=False)
    @patch.object(cli, "add_children")
    @patch.object(cli, "find_managed_content_blocks", return_value=[])
    def test_legacy_page_body_is_preserved_by_default(self, find_blocks, add_children):
        cli.client = SimpleNamespace()

        changed = cli.replace_managed_content(
            "page-id", [], {}, page_existed=True
        )

        self.assertFalse(changed)
        add_children.assert_not_called()

    @patch.object(cli, "add_children")
    @patch.object(
        cli,
        "find_managed_content_blocks",
        return_value=[{"id": "managed-id"}],
    )
    def test_only_managed_toggle_is_replaced(self, find_blocks, add_children):
        delete = Mock()
        cli.client = SimpleNamespace(blocks=SimpleNamespace(delete=delete))
        add_children.side_effect = [[{"id": "container-id"}], []]

        changed = cli.replace_managed_content(
            "page-id", [], {}, page_existed=True
        )

        self.assertTrue(changed)
        delete.assert_called_once_with(block_id="managed-id")


if __name__ == "__main__":
    unittest.main()
