import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from weread2notion import cli


class CliTestCase(unittest.TestCase):
    def setUp(self):
        cli.title_property_name = "书名"
        cli.skipped_property_names = set()
        cli.data_source_properties = {}
        cli.relation_target_cache.clear()
        cli.relation_page_cache.clear()
        cli.relation_error_names.clear()
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
            "开始阅读时间": "date",
            "封面": "files",
            "简介": "rich_text",
        }

    def test_build_book_properties_matches_existing_database(self):
        properties = cli.build_book_properties(
            {"title": "测试书", "bookId": "book-id"},
            123,
            {"isbn": "9780000000000", "rating": 8.6},
            {
                "markedStatus": 4,
                "readingTime": 23819,
                "readingProgress": 0.2,
                "finishedDate": 1754352000,
                "lastReadDate": 1754438400,
                "startReadDate": 1754265600,
            },
        )

        self.assertEqual(properties["阅读状态"], {"status": {"name": "已读"}})
        self.assertEqual(properties["阅读时长"], {"number": 23819})
        self.assertEqual(properties["微信读书进度"], {"number": 0.2})
        self.assertEqual(properties["BookId"]["rich_text"][0]["text"]["content"], "book-id")
        self.assertIn("时间", properties)
        self.assertIn("最后阅读时间", properties)
        self.assertIn("开始阅读时间", properties)

    def test_unfinished_book_does_not_write_completed_date(self):
        properties = cli.build_book_properties(
            {"title": "在读书", "bookId": "book-id"},
            123,
            {},
            {
                "markedStatus": 2,
                "readingTime": 60,
                "readingProgress": 0.1,
                "finishedDate": None,
                "lastReadDate": 1754438400,
                "startReadDate": None,
            },
        )

        self.assertEqual(properties["阅读状态"], {"status": {"name": "在读"}})
        self.assertNotIn("时间", properties)

    def test_optional_metadata_does_not_clear_existing_values(self):
        properties = cli.build_book_properties(
            {"title": "测试书", "bookId": "book-id"},
            123,
            {},
            None,
        )

        self.assertNotIn("ISBN", properties)
        self.assertNotIn("评分", properties)
        self.assertNotIn("简介", properties)
        self.assertNotIn("封面", properties)

    def test_cover_file_property_is_written(self):
        properties = cli.build_book_properties(
            {
                "title": "测试书",
                "bookId": "book-id",
                "cover": "https://example.com/cover.jpg",
            },
            123,
            {},
            None,
        )

        self.assertEqual(
            properties["封面"]["files"][0]["external"]["url"],
            "https://example.com/cover.jpg",
        )

    def test_normalize_reading_progress(self):
        self.assertEqual(cli.normalize_reading_progress(20), 0.2)
        self.assertEqual(cli.normalize_reading_progress(0.2), 0.2)
        self.assertEqual(cli.normalize_reading_progress(150), 1)

    def test_read_info_prefers_live_reading_time_and_maps_start_date(self):
        cli.weread = SimpleNamespace(
            request=Mock(
                return_value={
                    "book": {
                        "progress": 20,
                        "readingTime": 30674,
                        "recordReadingTime": 0,
                        "startReadingTime": 1642427133,
                        "updateTime": 1747499863,
                    }
                }
            )
        )

        result = cli.get_read_info("book-id")

        self.assertEqual(result["readingTime"], 30674)
        self.assertEqual(result["readingProgress"], 0.2)
        self.assertEqual(result["startReadDate"], 1642427133)

    def test_read_info_falls_back_to_record_reading_time(self):
        cli.weread = SimpleNamespace(
            request=Mock(
                return_value={
                    "book": {
                        "progress": 1,
                        "recordReadingTime": 90,
                    }
                }
            )
        )

        self.assertEqual(cli.get_read_info("book-id")["readingTime"], 90)

    def test_personal_reviews_are_not_filtered_by_undocumented_type(self):
        cli.weread = SimpleNamespace(
            request=Mock(
                return_value={
                    "hasMore": 0,
                    "synckey": 1,
                    "reviews": [
                        {
                            "review": {
                                "type": 1,
                                "content": "整本书评论",
                            }
                        },
                        {
                            "review": {
                                "type": 99,
                                "content": "划线想法",
                                "abstract": "对应原文",
                                "chapterUid": 1,
                            }
                        },
                    ],
                }
            )
        )

        summary, notes = cli.get_review_list("book-id")

        self.assertEqual(summary[0]["review"]["content"], "整本书评论")
        self.assertEqual(notes[0]["markText"], "划线想法")
        self.assertEqual(notes[0]["abstract"], "对应原文")

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

    def test_existing_book_sort_is_read_from_its_own_page(self):
        page = {
            "properties": {
                "Sort": {"type": "number", "number": 123},
            }
        }

        self.assertEqual(cli.get_existing_book_sort(page), 123)

    @patch.object(cli, "get_notebooklist")
    def test_shelf_and_notebooks_are_merged_by_book_id(self, get_notebooks):
        get_notebooks.return_value = [
            {
                "bookId": "book-1",
                "sort": 300,
                "noteCount": 2,
                "reviewCount": 1,
                "book": {"bookId": "book-1", "title": "笔记标题"},
            }
        ]
        cli.weread = SimpleNamespace(
            request=Mock(
                return_value={
                    "books": [
                        {
                            "bookId": "book-1",
                            "title": "书架标题",
                            "readUpdateTime": 200,
                        },
                        {
                            "bookId": "book-2",
                            "title": "无笔记书",
                            "readUpdateTime": 100,
                        },
                    ],
                    "archive": [{"name": "待读", "bookIds": ["book-2"]}],
                    "albums": [],
                }
            )
        )

        books = cli.get_books_to_sync()
        by_id = {book["bookId"]: book for book in books}

        self.assertEqual(len(books), 2)
        self.assertEqual(by_id["book-1"]["sort"], 300)
        self.assertTrue(by_id["book-1"]["hasNotebook"])
        self.assertFalse(by_id["book-2"]["hasNotebook"])
        self.assertEqual(by_id["book-2"]["archiveNames"], ["待读"])

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
            "startReadDate": None,
        }
        pages = SimpleNamespace(update=Mock(), create=Mock())
        cli.client = SimpleNamespace(pages=pages)

        page_id, existed = cli.upsert_to_notion(
            {"title": "测试书", "bookId": "book-id", "cover": ""},
            123,
            {},
            {"id": "page-id"},
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
