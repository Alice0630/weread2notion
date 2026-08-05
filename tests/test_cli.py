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
        cli.data_source_schema_cache.clear()
        cli.existing_pages_by_book_id = None
        cli.resolved_progress_property_name = None
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
            "阅读进度": "number",
            "微信阅读进度": "number",
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
        self.assertEqual(properties["阅读进度"], {"number": 0.2})
        self.assertEqual(properties["微信阅读进度"], {"number": 0.2})
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

    def test_progress_falls_back_to_legacy_writable_number(self):
        cli.data_source_property_types.pop("阅读进度")

        self.assertEqual(cli.get_progress_property_name(), "微信阅读进度")

    @patch.object(cli, "build_relation_property")
    def test_reading_year_maps_last_read_date_to_year_relation(self, relation):
        cli.data_source_property_types.update(
            {"年": "relation", "月": "relation", "周": "relation", "日": "relation"}
        )
        relation.return_value = {"relation": [{"id": "year-2026"}]}

        properties = cli.build_book_properties(
            {"title": "测试书", "bookId": "book-id"},
            123,
            {},
            {
                "markedStatus": 2,
                "readingTime": 60,
                "readingProgress": 0.1,
                "finishedDate": None,
                "lastReadDate": 1785859200,
                "startReadDate": None,
            },
        )

        self.assertEqual(properties["年"], {"relation": [{"id": "year-2026"}]})
        relation.assert_any_call("年", "2026")
        relation.assert_any_call("月", "2026年8月")
        relation.assert_any_call("周", "2026年第32周")
        relation.assert_any_call("日", "2026年08月05日")

    def test_period_titles_match_existing_notion_naming(self):
        self.assertEqual(
            cli.get_period_titles(1785859200),
            {
                "年": "2026",
                "月": "2026年8月",
                "周": "2026年第32周",
                "日": "2026年08月05日",
            },
        )

    def test_period_date_does_not_duplicate_timezone_offset(self):
        properties = cli.build_period_page_properties(
            "月",
            "2026年8月",
            "标题",
            {"标题": {"type": "title"}, "日期": {"type": "date"}},
        )

        self.assertEqual(
            properties["日期"],
            {
                "date": {
                    "start": "2026-08-01T00:00:00",
                    "time_zone": "Asia/Shanghai",
                }
            },
        )

    def test_reading_stats_uses_annual_daily_details_when_available(self):
        timestamp = int(
            cli.datetime(
                2026, 8, 5, tzinfo=cli.SHANGHAI_TIME_ZONE
            ).timestamp()
        )
        cli.weread = SimpleNamespace(
            request=Mock(return_value={"dailyReadTimes": {str(timestamp): 3661}})
        )

        result = cli.get_daily_read_times(
            2026,
            cli.datetime(2026, 7, 23).date(),
            cli.datetime(2026, 8, 5, tzinfo=cli.SHANGHAI_TIME_ZONE),
        )

        self.assertEqual(result, {timestamp: 3661})
        cli.weread.request.assert_called_once()

    def test_reading_stats_falls_back_to_monthly_daily_buckets(self):
        july_1 = int(
            cli.datetime(
                2026, 7, 1, tzinfo=cli.SHANGHAI_TIME_ZONE
            ).timestamp()
        )
        july_31 = int(
            cli.datetime(
                2026, 7, 31, tzinfo=cli.SHANGHAI_TIME_ZONE
            ).timestamp()
        )
        august_5 = int(
            cli.datetime(
                2026, 8, 5, tzinfo=cli.SHANGHAI_TIME_ZONE
            ).timestamp()
        )
        cli.weread = SimpleNamespace(
            request=Mock(
                side_effect=[
                    {},
                    {"readTimes": {str(july_1): 10, str(july_31): 120}},
                    {"readTimes": {str(august_5): 3600}},
                ]
            )
        )

        result = cli.get_daily_read_times(
            2026,
            cli.datetime(2026, 7, 23).date(),
            cli.datetime(2026, 8, 5, tzinfo=cli.SHANGHAI_TIME_ZONE),
        )

        self.assertEqual(result, {july_31: 120, august_5: 3600})
        self.assertEqual(
            [call.kwargs["mode"] for call in cli.weread.request.call_args_list],
            ["annually", "monthly", "monthly"],
        )

    def test_reading_heatmap_uses_seconds_as_hours(self):
        svg = cli.render_reading_heatmap_svg(
            2026,
            {
                cli.date(2026, 1, 1): 3600,
                cli.date(2026, 1, 2): 1800,
            },
        )

        self.assertIn("2026 Reading Heatmap", svg)
        self.assertIn("2 active days &#183; 1.5 hours", svg)
        self.assertIn("2026-01-01: 1.00 hours", svg)
        self.assertEqual(svg.count("<rect"), 371)

    @patch.object(cli, "query_related_pages")
    @patch.object(cli, "get_data_source_schema")
    @patch.object(cli, "get_relation_target")
    def test_related_rows_are_upserted_by_stable_id(
        self, relation_target, schema, query_pages
    ):
        cli.data_source_property_types["划线"] = "relation"
        relation_target.return_value = ("highlight-source", "Name")
        schema.return_value = {
            "Name": {"type": "title"},
            "bookmarkId": {"type": "rich_text"},
            "书籍": {"type": "relation"},
        }
        query_pages.return_value = [
            {
                "id": "existing-highlight",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "旧划线"}],
                    },
                    "bookmarkId": {
                        "type": "rich_text",
                        "rich_text": [{"plain_text": "bookmark-1"}],
                    },
                    "书籍": {
                        "type": "relation",
                        "relation": [{"id": "book-page"}],
                    },
                },
            }
        ]
        pages = SimpleNamespace(
            create=Mock(return_value={"id": "new-highlight"}), update=Mock()
        )
        cli.client = SimpleNamespace(pages=pages)

        result = cli.upsert_related_rows(
            "划线",
            "book-page",
            "bookmarkId",
            [
                {
                    "Name": "新划线",
                    "bookmarkId": "bookmark-1",
                    "书籍": ["book-page"],
                },
                {
                    "Name": "另一条划线",
                    "bookmarkId": "bookmark-2",
                    "书籍": ["book-page"],
                },
            ],
        )

        self.assertEqual(result, (1, 1, 0))
        pages.update.assert_called_once()
        pages.create.assert_called_once()

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

    def test_existing_last_read_date_is_parsed(self):
        page = {
            "properties": {
                "最后阅读时间": {
                    "type": "date",
                    "date": {"start": "2026-08-05T02:00:00+08:00"},
                }
            }
        }

        self.assertGreater(cli.get_existing_last_read_date(page), 0)

    @patch.dict(
        os.environ,
        {
            "NOTION_DATABASE_ID": "fff7538b60b2815599fdc5d8914f4490",
            "NOTION_DATA_SOURCE_ID": "11111111111111111111111111111111",
            "NOTION_PAGE": "https://notion.so/b830a450b3924f7cbca652892c0c3b8a",
        },
        clear=False,
    )
    def test_database_id_is_authoritative_target(self):
        self.assertEqual(cli.extract_notion_id(), "fff7538b60b2815599fdc5d8914f4490")

    @patch.dict(
        os.environ,
        {"BATCH_SIZE": "2", "BATCH_INDEX": "1", "WEREAD_BOOK_ID": ""},
        clear=False,
    )
    def test_full_sync_batch_is_stable_by_book_id(self):
        books = [
            {"bookId": "d"},
            {"bookId": "a"},
            {"bookId": "c"},
            {"bookId": "b"},
            {"bookId": "e"},
        ]

        selected = cli.select_books_for_run(books, full_sync=True)

        self.assertEqual([book["bookId"] for book in selected], ["c", "d"])

    def test_incremental_sync_prioritizes_recent_activity(self):
        books = [
            {"bookId": "old", "activityTime": 100},
            {"bookId": "new", "activityTime": 300},
            {"bookId": "middle", "activityTime": 200},
        ]

        selected = cli.select_books_for_run(books, full_sync=False)

        self.assertEqual(
            [book["bookId"] for book in selected],
            ["new", "middle", "old"],
        )

    @patch.dict(
        os.environ,
        {"SYNC_YEAR": "2026", "WEREAD_BOOK_ID": ""},
        clear=False,
    )
    def test_sync_year_filters_by_last_activity_year(self):
        books = [
            {
                "bookId": "current",
                "activityTime": 1785859200,
                "lastReadTime": 1785859200,
            },
            {
                "bookId": "old",
                "activityTime": 1722816000,
                "lastReadTime": 1722816000,
            },
        ]

        selected = cli.select_books_for_run(books, full_sync=False)

        self.assertEqual([book["bookId"] for book in selected], ["current"])

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
