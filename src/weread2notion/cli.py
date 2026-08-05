import argparse
import logging
import os
import re
import time
from pathlib import Path
from notion_client import Client
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
from dotenv import load_dotenv
from notion_client.errors import APIResponseError
from retrying import retry
from .blocks import (
    get_callout,
    get_date,
    get_file,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_title,
    get_toggle,
    get_url,
)

client = None
data_source_id = None
data_source_property_types = {}
data_source_properties = {}
title_property_name = None
skipped_property_names = set()
relation_target_cache = {}
relation_page_cache = {}
relation_error_names = set()
data_source_schema_cache = {}
existing_pages_by_book_id = None
resolved_progress_property_name = None
weread = None

load_dotenv()
WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
NOTION_VERSION = "2026-03-11"
BOOKMARK_CALLOUT_ICON = "〰️"
NOTE_CALLOUT_ICON = "✍️"
MANAGED_CONTENT_TITLE = "微信读书同步内容（自动更新）"
SHANGHAI_TIME_ZONE = ZoneInfo("Asia/Shanghai")
STATUS_MAP = {
    1: "想读",
    2: "在读",
    4: "已读",
}
PROPERTY_DEFAULTS = {
    "status": ("NOTION_STATUS_PROPERTY", "阅读状态"),
    "duration": ("NOTION_DURATION_PROPERTY", "阅读时长"),
    "progress": ("NOTION_PROGRESS_PROPERTY", "阅读进度"),
    "completed_date": ("NOTION_COMPLETED_DATE_PROPERTY", "时间"),
    "last_read_date": ("NOTION_LAST_READ_DATE_PROPERTY", "最后阅读时间"),
    "start_read_date": ("NOTION_START_READ_DATE_PROPERTY", "开始阅读时间"),
}
NOTION_TOKEN_PATTERN = re.compile(r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$")
WEREAD_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{10,}$")
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


class ConfigError(Exception):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{safe}")
    else:
        print(f"配置错误: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)
    if raw is None:
        if required:
            fail_config(f"缺少 {name}，请在 GitHub Actions Secrets 中配置")
        return None
    value = re.sub(r"\s+", "", raw)
    if value:
        os.environ[name] = value
        return value
    if required:
        fail_config(f"{name} 为空，请检查 GitHub Actions Secrets")
    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} 格式不正确：{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value("WEREAD_API_KEY", required=True)
    notion_token = clean_secret_value("NOTION_TOKEN", required=True)
    notion_page = clean_secret_value("NOTION_PAGE")
    notion_database_id = clean_secret_value("NOTION_DATABASE_ID")
    notion_data_source_id = clean_secret_value("NOTION_DATA_SOURCE_ID")

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "应为微信读书 Gateway API Key，不能包含空格或换行",
    )
    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "应以 secret_ 或 ntn_ 开头，不能包含空格或换行",
    )
    for name, value in (
        ("NOTION_DATA_SOURCE_ID", notion_data_source_id),
        ("NOTION_DATABASE_ID", notion_database_id),
    ):
        validate_regex(
            name,
            value,
            NOTION_ID_PATTERN,
            "应为 32 位 Notion ID 或带连字符的 UUID",
        )
    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_page):
        fail_config("NOTION_PAGE 格式不正确：请填写 Notion 页面链接、数据库链接或 ID")
    if not (notion_data_source_id or notion_page or notion_database_id):
        fail_config(
            "缺少 NOTION_PAGE / NOTION_DATA_SOURCE_ID / NOTION_DATABASE_ID，"
            "请至少配置其中一个"
        )
    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
    }


def get_property_name(key):
    env_name, default = PROPERTY_DEFAULTS[key]
    return os.getenv(env_name, default).strip() or default


def get_progress_property_name():
    """Choose the writable progress number property without touching formulas."""
    global resolved_progress_property_name
    if resolved_progress_property_name:
        return resolved_progress_property_name
    configured = get_property_name("progress")
    if configured in data_source_property_types:
        resolved_progress_property_name = configured
        return resolved_progress_property_name
    for fallback in ("阅读进度", "微信阅读进度", "微信读书进度"):
        if data_source_property_types.get(fallback) == "number":
            print(f"进度字段映射: {configured} 不可写，改用数字字段 {fallback}")
            resolved_progress_property_name = fallback
            return resolved_progress_property_name
    if configured in data_source_property_types:
        print(
            f"提示: {configured} 是 {data_source_property_types[configured]} 字段，"
            "不能直接写入。请保留一个 Number/Percent 类型的阅读进度字段"
        )
    resolved_progress_property_name = configured
    return resolved_progress_property_name


def get_writable_progress_properties():
    """Keep compatible progress number fields in sync without touching formulas."""
    primary = get_progress_property_name()
    names = [primary]
    for name in ("阅读进度", "微信阅读进度", "微信读书进度"):
        if data_source_property_types.get(name) == "number" and name not in names:
            names.append(name)
    return names


def is_enabled(name):
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def get_nonnegative_int(name, default):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        fail_config(f"{name} 必须是整数")
    if value < 0:
        fail_config(f"{name} 不能小于 0")
    return value


def get_positive_int(name, default):
    value = get_nonnegative_int(name, default)
    if value == 0:
        fail_config(f"{name} 必须大于 0")
    return value


def get_existing_page_mode():
    mode = (os.getenv("NOTION_EXISTING_PAGE_MODE") or "append").strip().lower()
    if mode not in {"preserve", "append"}:
        fail_config("NOTION_EXISTING_PAGE_MODE 只能是 preserve 或 append")
    return mode


def matches_book_filter(book_id):
    target_book_id = (os.getenv("WEREAD_BOOK_ID") or "").strip()
    return not target_book_id or book_id == target_book_id


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config("没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def request(self, api_name, **kwargs):
        payload = {
            "api_name": api_name,
            "skill_version": WEREAD_SKILL_VERSION,
            **kwargs,
        }
        response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("upgrade_info"):
            raise Exception(f"微信读书 skill 需要升级: {data.get('upgrade_info')}")
        if data.get("errcode", 0) != 0:
            raise Exception(f"微信读书 Gateway 请求失败: {api_name}, errcode={data.get('errcode')}, response={data}")
        return data


def get_range_start(item):
    note_range = item.get("range") or ""
    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None
    if chapter:
        chapter_info = chapter.get(chapter_uid) or chapter.get(str(chapter_uid))
    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )
    return (chapter_idx, get_range_start(item))


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookmark_list(bookId):
    """获取我的划线"""
    data = weread.request("/book/bookmarklist", bookId=bookId)
    updated = data.get("updated") or []
    return sorted(updated, key=get_note_sort_key)


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_read_info(bookId):
    data = weread.request("/book/getprogress", bookId=bookId)
    book = data.get("book") or {}
    progress = to_number(book.get("progress")) or 0
    reading_progress = normalize_reading_progress(progress)
    finish_time = book.get("finishTime") or 0
    update_time = book.get("updateTime") or 0
    if finish_time or progress >= 100:
        marked_status = 4
    elif update_time or book.get("isStartReading") or progress > 0:
        marked_status = 2
    else:
        marked_status = 1
    return {
        "markedStatus": marked_status,
        # The live Gateway currently exposes total duration as readingTime for
        # many books while recordReadingTime remains zero.
        "readingTime": book.get("readingTime") or book.get("recordReadingTime") or 0,
        "readingProgress": reading_progress,
        "finishedDate": finish_time or None,
        "lastReadDate": update_time or None,
        "startReadDate": book.get("startReadingTime") or None,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0
    if value > 1:
        value = value / 100
    return round(min(max(value, 0), 1), 4)


def normalize_rating(value):
    value = value or 0
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_bookinfo(bookId):
    """获取书的详情"""
    data = weread.request("/book/info", bookId=bookId)
    metadata = {
        "isbn": data.get("isbn") or None,
        "rating": normalize_rating(data.get("newRating")) or None,
        "author": data.get("author") or None,
        "cover": data.get("cover") or None,
        "intro": data.get("intro") or None,
        "category": data.get("category") or None,
        "publisher": data.get("publisher") or None,
        "publishTime": data.get("publishTime") or None,
    }
    return {name: value for name, value in metadata.items() if value is not None}


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_review_list(bookId):
    """获取当前用户的想法、章节点评和整本书评。"""
    reviews_data = []
    hasMore = 1
    synckey = 0
    seen_synckeys = set()
    while hasMore:
        data = weread.request(
            "/review/list/mine", bookid=bookId, synckey=synckey, count=100
        )
        hasMore = data.get("hasMore", 0)
        next_synckey = data.get("synckey", 0)
        batch = data.get("reviews") or []
        reviews_data.extend(batch)
        if not batch or (hasMore and next_synckey in seen_synckeys):
            hasMore = 0
        seen_synckeys.add(next_synckey)
        synckey = next_synckey
    summary = []
    reviews = []
    for item in reviews_data:
        review = dict(item.get("review") or {})
        if not review.get("content"):
            continue
        has_location = any(
            review.get(name) not in (None, "", 0)
            for name in ("abstract", "range", "chapterUid", "chapterIdx", "chapterName")
        )
        if not has_location:
            summary.append({"review": review})
            continue
        review["markText"] = review.pop("content", "")
        review["_callout_icon"] = NOTE_CALLOUT_ICON
        reviews.append(review)
    return summary, reviews


def find_book_page(bookId):
    """按 BookId 查找已有页面，避免删除并重建整个页面。"""
    if existing_pages_by_book_id is not None:
        return existing_pages_by_book_id.get(bookId)
    filter = build_equals_filter("BookId", bookId)
    response = query_data_source(filter=filter, page_size=2)
    results = response.get("results") or []
    if len(results) > 1:
        print(f"警告: BookId={bookId} 匹配到多个页面，将更新第一个页面")
    return results[0] if results else None


def get_page_text_property(property_value):
    if not property_value:
        return ""
    prop_type = property_value.get("type")
    values = property_value.get(prop_type) or []
    if isinstance(values, list):
        return "".join(
            item.get("plain_text")
            or (item.get("text") or {}).get("content")
            or ""
            for item in values
        )
    return ""


def preload_existing_pages():
    """Load existing BookId pages once, avoiding one Notion query per book."""
    pages_by_book_id = {}
    start_cursor = None
    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        response = query_data_source(**body)
        for page in response.get("results") or []:
            properties = page.get("properties") or {}
            book_id = get_page_text_property(properties.get("BookId"))
            if book_id and book_id not in pages_by_book_id:
                pages_by_book_id[book_id] = page
        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            break
    print(f"已载入现有 Notion 页面索引: {len(pages_by_book_id)} 本")
    return pages_by_book_id


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def get_chapter_info(bookId):
    """获取章节信息"""
    data = weread.request("/book/chapterinfo", bookId=bookId)
    chapters = data.get("chapters") or []
    return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}


def build_book_properties(book, sort, metadata, read_info):
    book_name = book.get("title") or "未命名书籍"
    book_id = book.get("bookId")
    metadata = metadata or {}
    status_property = get_property_name("status")
    duration_property = get_property_name("duration")
    progress_properties = get_writable_progress_properties()
    completed_date_property = get_property_name("completed_date")
    last_read_date_property = get_property_name("last_read_date")
    start_read_date_property = get_property_name("start_read_date")

    cover = metadata.get("cover") or book.get("cover")
    author = metadata.get("author") or book.get("author")
    category = metadata.get("category") or book.get("category")
    deep_link = book.get("deepLink")
    web_link = f"https://weread.qq.com/web/reader/{calculate_book_str_id(book_id)}"
    if deep_link and str(deep_link).startswith(("http://", "https://")):
        web_link = deep_link

    raw_properties = {
        title_property_name: book_name,
        "BookId": book_id,
        "ISBN": metadata.get("isbn"),
        "链接": web_link,
        "Sort": sort,
        "评分": metadata.get("rating"),
        "封面": cover if cover and str(cover).startswith("http") else None,
        "简介": metadata.get("intro"),
    }
    optional_properties = {
        "作者": author,
        "分类": category,
        "出版社": metadata.get("publisher"),
        "出版时间": metadata.get("publishTime"),
        "书架分类": (book.get("archiveNames") or [None])[0],
    }
    raw_properties.update(
        {
            name: value
            for name, value in optional_properties.items()
            if name in data_source_property_types
        }
    )
    if read_info:
        raw_properties.update(
            {
                status_property: STATUS_MAP.get(
                    read_info.get("markedStatus"), "想读"
                ),
                duration_property: read_info.get("readingTime", 0),
                completed_date_property: read_info.get("finishedDate"),
                last_read_date_property: read_info.get("lastReadDate"),
                start_read_date_property: read_info.get("startReadDate"),
            }
        )
        for progress_property in progress_properties:
            raw_properties[progress_property] = read_info.get("readingProgress", 0)
        for period_name, period_title in get_reading_periods(read_info).items():
            if period_name in data_source_property_types:
                raw_properties[period_name] = period_title
    return build_notion_properties(raw_properties)


def upsert_to_notion(book, sort, metadata, existing_page):
    """创建页面或原位更新已有页面，不删除用户页面。"""
    book_id = book.get("bookId")
    metadata = metadata or {}
    cover = metadata.get("cover") or book.get("cover")
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    read_property_names = tuple(
        get_progress_property_name()
        if key == "progress"
        else get_property_name(key)
        for key in PROPERTY_DEFAULTS
    )
    read_info = (
        get_read_info(bookId=book_id)
        if has_any_property(read_property_names)
        else None
    )
    properties = build_book_properties(book, sort, metadata, read_info)
    icon = get_icon(cover)
    if existing_page:
        page_id = existing_page["id"]
        client.pages.update(
            page_id=page_id,
            icon=icon,
            cover=icon,
            properties=properties,
        )
        return page_id, True

    parent = {"type": "data_source_id", "data_source_id": data_source_id}
    response = client.pages.create(
        parent=parent,
        icon=icon,
        cover=icon,
        properties=properties,
    )
    return response["id"], False


def add_children(id, children):
    results = []
    for start in range(0, len(children), 100):
        time.sleep(0.3)
        response = client.blocks.children.append(
            block_id=id, children=children[start : start + 100]
        )
        results.extend(response.get("results") or [])
    return results if len(results) == len(children) else None


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(0.3)
        id = results[key].get("id")
        client.blocks.children.append(block_id=id, children=[value])


def get_block_text(block):
    block_type = block.get("type")
    block_data = block.get(block_type) or {}
    return "".join(
        item.get("plain_text") or (item.get("text") or {}).get("content") or ""
        for item in block_data.get("rich_text") or []
    )


def list_page_children(page_id):
    results = []
    start_cursor = None
    while True:
        params = {"block_id": page_id, "page_size": 100}
        if start_cursor:
            params["start_cursor"] = start_cursor
        response = client.blocks.children.list(**params)
        results.extend(response.get("results") or [])
        if not response.get("has_more"):
            break
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            break
    return results


def find_managed_content_blocks(page_id):
    return [
        block
        for block in list_page_children(page_id)
        if block.get("type") == "toggle"
        and get_block_text(block) == MANAGED_CONTENT_TITLE
    ]


def replace_managed_content(page_id, children, grandchild, page_existed):
    managed_blocks = find_managed_content_blocks(page_id)
    if page_existed and not managed_blocks and get_existing_page_mode() == "preserve":
        print(
            "已有页面没有受管同步区域：为保护原正文，本次只更新数据库属性。"
            "如需追加受管区域，请手动运行时选择 existing-page-mode=append。"
        )
        return False

    container_results = add_children(page_id, [get_toggle(MANAGED_CONTENT_TITLE)])
    if not container_results:
        raise Exception("创建微信读书受管同步区域失败")
    container_id = container_results[0]["id"]
    try:
        results = add_children(container_id, children)
        if results is None:
            raise Exception("写入微信读书受管同步内容失败")
        if grandchild:
            add_grandchild(grandchild, results)
    except Exception:
        client.blocks.delete(block_id=container_id)
        raise

    for block in managed_blocks:
        client.blocks.delete(block_id=block["id"])
    return True


def get_notebooklist():
    """获取所有包含个人笔记的书，按 lastSort 游标完整分页。"""
    books = []
    hasMore = 1
    lastSort = None
    seen_cursors = set()
    while hasMore:
        params = {"count": 100}
        if lastSort is not None:
            params["lastSort"] = lastSort
        data = weread.request("/user/notebooks", **params)
        hasMore = data.get("hasMore", 0)
        batch = data.get("books") or []
        books.extend(batch)
        if batch:
            next_sort = batch[-1].get("sort")
            if next_sort is None or next_sort in seen_cursors:
                break
            seen_cursors.add(next_sort)
            lastSort = next_sort
        else:
            hasMore = 0
    books.sort(key=lambda x: x.get("sort") or 0)
    return books


def get_books_to_sync():
    """合并整个电子书书架与笔记本数据，确保无笔记书籍也能同步。"""
    shelf = weread.request("/shelf/sync")
    shelf_books = shelf.get("books") or []
    notebooks = get_notebooklist()
    notebook_by_id = {}
    for notebook in notebooks:
        nested_book = notebook.get("book") or {}
        book_id = notebook.get("bookId") or nested_book.get("bookId")
        if book_id:
            notebook_by_id[book_id] = notebook

    archive_names_by_book = {}
    for archive in shelf.get("archive") or []:
        archive_name = archive.get("name")
        if not archive_name:
            continue
        for book_id in archive.get("bookIds") or []:
            archive_names_by_book.setdefault(book_id, []).append(archive_name)

    merged = []
    seen_book_ids = set()
    for shelf_book in shelf_books:
        book_id = shelf_book.get("bookId")
        if not book_id:
            continue
        notebook = notebook_by_id.get(book_id) or {}
        notebook_book = notebook.get("book") or {}
        item = {**shelf_book, **notebook_book}
        item.update(
            {
                "bookId": book_id,
                "contentSort": notebook.get("sort") or 0,
                "activityTime": max(
                    shelf_book.get("readUpdateTime") or 0,
                    notebook.get("sort") or 0,
                ),
                "lastReadTime": shelf_book.get("readUpdateTime") or 0,
                "sort": notebook.get("sort")
                or shelf_book.get("readUpdateTime")
                or shelf_book.get("updateTime")
                or 0,
                "noteCount": notebook.get("noteCount") or 0,
                "reviewCount": notebook.get("reviewCount") or 0,
                "bookmarkCount": notebook.get("bookmarkCount") or 0,
                "hasNotebook": bool(notebook),
                "archiveNames": archive_names_by_book.get(book_id, []),
            }
        )
        merged.append(item)
        seen_book_ids.add(book_id)

    # Imported or removed books can remain in notebooks even when absent from
    # the current shelf response. Keep them so their notes are not lost.
    for book_id, notebook in notebook_by_id.items():
        if book_id in seen_book_ids:
            continue
        item = {**(notebook.get("book") or {})}
        item.update(
            {
                "bookId": book_id,
                "contentSort": notebook.get("sort") or 0,
                "activityTime": notebook.get("sort") or 0,
                "lastReadTime": 0,
                "sort": notebook.get("sort") or 0,
                "noteCount": notebook.get("noteCount") or 0,
                "reviewCount": notebook.get("reviewCount") or 0,
                "bookmarkCount": notebook.get("bookmarkCount") or 0,
                "hasNotebook": True,
                "archiveNames": archive_names_by_book.get(book_id, []),
            }
        )
        merged.append(item)

    album_count = len(shelf.get("albums") or [])
    if album_count:
        print(f"提示: 书架还有 {album_count} 个专辑/有声书，当前 Notion 书籍结构暂不支持同步")
    if shelf.get("mp"):
        print("提示: 书架包含文章收藏入口，Gateway 不返回其中的文章列表")
    merged.sort(key=lambda item: item.get("sort") or 0)
    print(
        f"微信读书数据: {len(shelf_books)} 本电子书，"
        f"{len(notebooks)} 本包含个人笔记，本次合并同步 {len(merged)} 本"
    )
    return merged


def select_books_for_run(books, full_sync):
    """Limit manual full syncs to a stable, resumable batch."""
    target_book_id = (os.getenv("WEREAD_BOOK_ID") or "").strip()
    target_year = (os.getenv("SYNC_YEAR") or "").strip()
    candidates = [
        book
        for book in books
        if (not target_book_id or book.get("bookId") == target_book_id)
        and (
            not target_year
            or get_reading_year({"lastReadDate": book.get("lastReadTime")})
            == target_year
        )
    ]
    if target_year:
        print(f"已启用年份过滤: 只处理最后阅读年份为 {target_year} 的书")
    if not full_sync:
        return sorted(
            candidates,
            key=lambda book: book.get("activityTime") or 0,
            reverse=True,
        )
    candidates.sort(key=lambda book: str(book.get("bookId") or ""))
    batch_size = get_positive_int("BATCH_SIZE", 25)
    batch_index = get_nonnegative_int("BATCH_INDEX", 0)
    total_batches = (len(candidates) + batch_size - 1) // batch_size
    start = batch_index * batch_size
    end = min(start + batch_size, len(candidates))
    if start >= len(candidates):
        print(
            f"全量批次 {batch_index} 超出范围，本次没有书籍。"
            f"有效批次为 0-{max(total_batches - 1, 0)}"
        )
        return []
    print(
        f"全量批次: {batch_index}/{max(total_batches - 1, 0)}，"
        f"本批 {start + 1}-{end}/{len(candidates)} 本，批大小 {batch_size}"
    )
    return candidates[start:end]


def get_existing_book_sort(existing_page):
    if not existing_page:
        return 0
    properties = existing_page.get("properties") or {}
    return get_number_property_value(properties.get("Sort"))


def get_existing_last_read_date(existing_page):
    if not existing_page:
        return 0
    properties = existing_page.get("properties") or {}
    property_value = properties.get(get_property_name("last_read_date")) or {}
    date_value = property_value.get("date") or {}
    start = date_value.get("start")
    if not start:
        return 0
    try:
        parsed = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SHANGHAI_TIME_ZONE)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def should_refresh_content(sort, existing_sort, full_sync, existing_page):
    return full_sync or not existing_page or (sort or 0) > (existing_sort or 0)


def get_children(chapter, summary, bookmark_list):
    children = []
    grandchild = {}
    all_chapters = []
    if chapter:
        for uid, info in chapter.items():
            item = dict(info)
            item["chapterUid"] = item.get("chapterUid", uid)
            all_chapters.append(item)
        all_chapters.sort(key=lambda x: x.get("chapterIdx", 0))
    chapter_nodes = {node.get("chapterUid"): node for node in all_chapters}

    def get_ancestor_chain(current_chapter_info):
        if not current_chapter_info:
            return []
        try:
            current_pos = all_chapters.index(current_chapter_info)
        except ValueError:
            return [current_chapter_info]

        chain = []
        target_level = current_chapter_info.get("level", 1)
        for index in range(current_pos - 1, -1, -1):
            candidate = all_chapters[index]
            if candidate.get("level", 1) < target_level:
                chain.insert(0, candidate)
                target_level = candidate.get("level", 1)
                if target_level <= 1:
                    break
        chain.append(current_chapter_info)
        return chain

    if chapter:
        grouped_bookmarks = []
        last_uid = None
        current_group = None

        for data in bookmark_list:
            uid = data.get("chapterUid", 1)
            if uid != last_uid:
                if current_group:
                    grouped_bookmarks.append(current_group)
                info = chapter.get(uid) or chapter.get(str(uid))
                current_group = {
                    "chapterUid": uid,
                    "bookmarks": [],
                    "chapterInfo": info,
                }
                last_uid = uid
            current_group["bookmarks"].append(data)
        if current_group:
            grouped_bookmarks.append(current_group)

        previous_path_uids = []
        for group in grouped_bookmarks:
            info = group["chapterInfo"]
            if info:
                current_info = chapter_nodes.get(group["chapterUid"]) or chapter_nodes.get(
                    str(group["chapterUid"])
                )
                if current_info is None:
                    current_info = dict(info)
                    current_info["chapterUid"] = current_info.get("chapterUid", group["chapterUid"])
                path = get_ancestor_chain(current_info)

                divergence_index = 0
                min_len = min(len(path), len(previous_path_uids))
                while divergence_index < min_len:
                    path_uid = path[divergence_index].get("chapterUid")
                    if path_uid != previous_path_uids[divergence_index]:
                        break
                    divergence_index += 1

                for chapter_node in path[divergence_index:]:
                    children.append(
                        get_heading(
                            chapter_node.get("level"), chapter_node.get("title")
                        )
                    )
                previous_path_uids = [node.get("chapterUid") for node in path]
            else:
                previous_path_uids = []

            for i in group["bookmarks"]:
                markText = i.get("markText") or ""
                if not markText:
                    continue
                callout_icon = i.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
                for j in range(0, len(markText) // 2000 + 1):
                    children.append(
                        get_callout(
                            markText[j * 2000 : (j + 1) * 2000],
                            icon=callout_icon,
                        )
                    )
                if i.get("abstract") != None and i.get("abstract") != "":
                    quote = get_quote(i.get("abstract"))
                    grandchild[len(children) - 1] = quote

    else:
        # 如果没有章节信息
        for data in bookmark_list:
            markText = data.get("markText") or ""
            if not markText:
                continue
            callout_icon = data.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
            for i in range(0, len(markText) // 2000 + 1):
                children.append(
                    get_callout(
                        markText[i * 2000 : (i + 1) * 2000],
                        icon=callout_icon,
                    )
                )
    if summary != None and len(summary) > 0:
        children.append(get_heading(1, "点评"))
        for i in summary:
            content = (i.get("review") or {}).get("content") or ""
            if not content:
                continue
            for j in range(0, len(content) // 2000 + 1):
                children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        icon=NOTE_CALLOUT_ICON,
                    )
                )
    return children, grandchild


def transform_id(book_id):
    id_length = len(book_id)

    if re.match(r"^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def extract_notion_id():
    url_or_id = (
        # A database ID is the authoritative target when both IDs are set.
        # Older workflow versions accepted a stale data source ID first,
        # which could silently sync a different 21-property data source.
        os.getenv("NOTION_DATABASE_ID")
        or os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_PAGE")
    )
    if not url_or_id:
        fail_config("没有找到 NOTION_PAGE / NOTION_DATA_SOURCE_ID，请按照文档填写")
    match = NOTION_ID_IN_TEXT_PATTERN.search(url_or_id)
    if match:
        return match.group(0)

    fail_config("获取 Notion ID 失败，请检查 NOTION_PAGE / NOTION_DATA_SOURCE_ID")


def query_data_source(**body):
    return client.request(
        path=f"data_sources/{data_source_id}/query",
        method="POST",
        body=body,
    )


def load_data_source_schema():
    """读取当前 data source 的真实属性，只强制要求同步游标需要的字段。"""
    global data_source_properties, data_source_property_types
    global title_property_name, skipped_property_names, resolved_progress_property_name
    response = client.request(path=f"data_sources/{data_source_id}", method="GET")
    properties = response.get("properties") or {}
    data_source_properties = properties
    data_source_schema_cache[data_source_id] = properties
    resolved_progress_property_name = None
    data_source_property_types = {
        name: (config or {}).get("type") for name, config in properties.items()
    }
    title_property_name = next(
        (
            name
            for name, prop_type in data_source_property_types.items()
            if prop_type == "title"
        ),
        None,
    )
    skipped_property_names = set()
    if not title_property_name:
        raise Exception("Notion data source 缺少标题属性，请保留一个 Title 类型属性")

    missing = [
        name for name in ("BookId", "Sort") if name not in data_source_property_types
    ]
    if missing:
        raise Exception(
            f"Notion data source 缺少必填属性: {', '.join(missing)}。"
            "请在模板中补充后重试"
        )

    print(
        f"已读取 Notion 属性 {len(data_source_property_types)} 个，"
        f"标题属性: {title_property_name}"
    )
    relation_names = ("作者", "分类", "年", "月", "周", "日", "章节", "划线", "读书笔记")
    invisible_relations = [
        name for name in relation_names if name not in data_source_property_types
    ]
    if invisible_relations:
        print(
            "提示: 以下关联字段对 Integration 不可见: "
            f"{', '.join(invisible_relations)}。"
            "请把同一个 Notion Integration 连接到这些关联数据库。"
        )
    print(
        "字段映射: "
        f"状态={get_property_name('status')}, "
        f"时长={get_property_name('duration')}, "
        f"进度={get_progress_property_name()}, "
        f"完成时间={get_property_name('completed_date')}, "
        f"最后阅读={get_property_name('last_read_date')}, "
        f"开始阅读={get_property_name('start_read_date')}"
    )


def get_property_type(name):
    return data_source_property_types.get(name)


def has_any_property(names):
    return any(name in data_source_property_types for name in names)


def build_equals_filter(name, value):
    prop_type = get_property_type(name)
    if prop_type in {"title", "rich_text", "url", "email", "phone_number"}:
        return {"property": name, prop_type: {"equals": str(value)}}
    if prop_type == "number":
        return {"property": name, "number": {"equals": to_number(value)}}
    if prop_type == "select":
        return {"property": name, "select": {"equals": str(value)}}
    if prop_type == "status":
        return {"property": name, "status": {"equals": str(value)}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def build_is_not_empty_filter(name):
    prop_type = get_property_type(name)
    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
        "number",
        "select",
        "status",
        "date",
    }:
        return {"property": name, prop_type: {"is_not_empty": True}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(item) for item in value if item is not None)
    return str(value)


def to_name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_date_value(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, SHANGHAI_TIME_ZONE).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    return value


def get_reading_year(read_info):
    periods = get_reading_periods(read_info)
    return periods.get("年")


def get_reading_timestamp(read_info):
    if not read_info:
        return None
    return (
        read_info.get("lastReadDate")
        or read_info.get("startReadDate")
        or read_info.get("finishedDate")
    )


def get_period_titles(timestamp):
    if not timestamp:
        return {}
    try:
        value = datetime.fromtimestamp(float(timestamp), SHANGHAI_TIME_ZONE)
    except (TypeError, ValueError, OSError, OverflowError):
        return {}
    iso_year, iso_week, _ = value.isocalendar()
    return {
        "年": str(value.year),
        "月": f"{value.year}年{value.month}月",
        "周": f"{iso_year}年第{iso_week}周",
        "日": value.strftime("%Y年%m月%d日"),
    }


def get_reading_periods(read_info):
    return get_period_titles(get_reading_timestamp(read_info))


def parse_period_start(name, value):
    value = to_text(value).strip()
    try:
        if name == "年":
            match = re.fullmatch(r"(\d{4})", value)
            return datetime(int(match.group(1)), 1, 1, tzinfo=SHANGHAI_TIME_ZONE) if match else None
        if name == "月":
            match = re.fullmatch(r"(\d{4})年(\d{1,2})月", value)
            return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=SHANGHAI_TIME_ZONE) if match else None
        if name == "周":
            match = re.fullmatch(r"(\d{4})年第(\d{1,2})周", value)
            if match:
                return datetime.fromisocalendar(
                    int(match.group(1)), int(match.group(2)), 1
                ).replace(tzinfo=SHANGHAI_TIME_ZONE)
        if name == "日":
            match = re.fullmatch(r"(\d{4})年(\d{2})月(\d{2})日", value)
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=SHANGHAI_TIME_ZONE,
            ) if match else None
    except ValueError:
        return None
    return None


def build_option_property(prop_type, value):
    names = to_name_list(value)
    if not names:
        return None
    if prop_type == "status":
        return get_status(names[0])
    if prop_type == "select":
        return get_select(names[0])
    return get_multi_select(names)


def get_data_source_schema(target_id):
    if target_id not in data_source_schema_cache:
        response = client.request(path=f"data_sources/{target_id}", method="GET")
        data_source_schema_cache[target_id] = response.get("properties") or {}
    return data_source_schema_cache[target_id]


def get_relation_target(name):
    if name in relation_target_cache:
        return relation_target_cache[name]
    property_config = data_source_properties.get(name) or {}
    relation = property_config.get("relation") or {}
    target_id = relation.get("data_source_id")
    if not target_id and relation.get("database_id"):
        relation_database_id = relation["database_id"]
        try:
            client.request(
                path=f"data_sources/{relation_database_id}", method="GET"
            )
            target_id = relation_database_id
        except APIResponseError as error:
            code = getattr(error.code, "value", error.code)
            if code not in {"object_not_found", "validation_error"}:
                raise
            database = client.request(
                path=f"databases/{relation_database_id}", method="GET"
            )
            sources = database.get("data_sources") or []
            if sources:
                target_id = sources[0].get("id")
    if not target_id:
        raise Exception("Notion 未返回关联数据库 ID")
    properties = get_data_source_schema(target_id)
    target_title = next(
        (
            property_name
            for property_name, config in properties.items()
            if (config or {}).get("type") == "title"
        ),
        None,
    )
    if not target_title:
        raise Exception("关联数据库缺少 Title 属性")
    relation_target_cache[name] = (target_id, target_title)
    return relation_target_cache[name]


def build_period_page_properties(property_name, value, target_title, schema):
    properties = {target_title: get_title(value)}
    start = parse_period_start(property_name, value)
    if not start:
        return properties
    if (schema.get("日期") or {}).get("type") == "date":
        # get_date declares Asia/Shanghai separately, so the value itself must
        # not also contain a non-zero UTC offset under Notion API 2026-03-11.
        properties["日期"] = get_date(start.strftime("%Y-%m-%dT%H:%M:%S"))
    if property_name == "日" and (schema.get("时间戳") or {}).get("type") == "number":
        properties["时间戳"] = get_number(int(start.timestamp()))
    if property_name == "日":
        for related_name, related_title in get_period_titles(start.timestamp()).items():
            if related_name == "日" or (schema.get(related_name) or {}).get("type") != "relation":
                continue
            related_id = find_or_create_relation_page(related_name, related_title)
            if related_id:
                properties[related_name] = {"relation": [{"id": related_id}]}
    return properties


def find_or_create_relation_page(property_name, value):
    value = to_text(value).strip()
    if not value:
        return None
    cache_key = (property_name, value)
    if cache_key in relation_page_cache:
        return relation_page_cache[cache_key]
    target_id, target_title = get_relation_target(property_name)
    schema = get_data_source_schema(target_id)
    response = client.request(
        path=f"data_sources/{target_id}/query",
        method="POST",
        body={
            "filter": {
                "property": target_title,
                "title": {"equals": value},
            },
            "page_size": 1,
        },
    )
    results = response.get("results") or []
    if results:
        page_id = results[0]["id"]
    else:
        page = client.pages.create(
            parent={"type": "data_source_id", "data_source_id": target_id},
            properties=build_period_page_properties(
                property_name, value, target_title, schema
            ),
        )
        page_id = page["id"]
    relation_page_cache[cache_key] = page_id
    return page_id


def build_relation_property(name, value):
    try:
        page_ids = [
            page_id
            for item in to_name_list(value)
            if (page_id := find_or_create_relation_page(name, item))
        ]
        if not page_ids:
            return None
        return {"relation": [{"id": page_id} for page_id in page_ids]}
    except Exception as error:
        if name not in relation_error_names:
            print(
                f"属性 {name} 的关联库写入失败，保留原值。原因: {error}"
            )
            relation_error_names.add(name)
        return None


def build_notion_property(name, value):
    prop_type = get_property_type(name)
    if not prop_type:
        if name not in skipped_property_names:
            print(f"属性 {name} 在 Notion 模板中不存在，自动跳过")
            skipped_property_names.add(name)
        return None
    if value is None:
        return None

    if prop_type == "title":
        return get_title(to_text(value))
    if prop_type == "rich_text":
        return get_rich_text(to_text(value))
    if prop_type == "number":
        number = to_number(value)
        return get_number(number) if number is not None else None
    if prop_type == "url":
        return get_url(to_text(value))
    if prop_type in {"multi_select", "status", "select"}:
        return build_option_property(prop_type, value)
    if prop_type == "date":
        return get_date(normalize_date_value(value))
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "files":
        text = to_text(value)
        return get_file(text) if text.startswith(("http://", "https://")) else None
    if prop_type == "relation":
        return build_relation_property(name, value)

    if name not in skipped_property_names:
        print(f"属性 {name} 的类型 {prop_type} 暂不支持写入，自动跳过")
        skipped_property_names.add(name)
    return None


def build_notion_properties(raw_properties):
    return {
        name: prop
        for name, value in raw_properties.items()
        if (prop := build_notion_property(name, value)) is not None
    }


def build_properties_for_schema(schema, raw_properties):
    properties = {}
    for name, value in raw_properties.items():
        config = schema.get(name) or {}
        prop_type = config.get("type")
        if not prop_type or value is None:
            continue
        if prop_type == "title":
            properties[name] = get_title(to_text(value)[:2000])
        elif prop_type == "rich_text":
            properties[name] = get_rich_text(to_text(value)[:2000])
        elif prop_type == "number":
            number = to_number(value)
            if number is not None:
                properties[name] = get_number(number)
        elif prop_type == "date":
            properties[name] = get_date(normalize_date_value(value))
        elif prop_type == "url":
            properties[name] = get_url(to_text(value))
        elif prop_type == "relation":
            page_ids = value if isinstance(value, (list, tuple, set)) else [value]
            page_ids = [to_text(page_id) for page_id in page_ids if page_id]
            properties[name] = {"relation": [{"id": page_id} for page_id in page_ids]}
    return properties


def get_plain_property_value(property_value):
    if not property_value:
        return None
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if prop_type in {"title", "rich_text"}:
        return "".join(item.get("plain_text") or "" for item in (value or []))
    if prop_type == "date":
        return (value or {}).get("start")
    if prop_type == "relation":
        return sorted(item.get("id") for item in (value or []) if item.get("id"))
    if prop_type in {"number", "url", "checkbox"}:
        return value
    return value


def get_desired_property_value(property_value):
    if not property_value:
        return None
    if "title" in property_value:
        return "".join(
            (item.get("text") or {}).get("content") or ""
            for item in property_value["title"]
        )
    if "rich_text" in property_value:
        return "".join(
            (item.get("text") or {}).get("content") or ""
            for item in property_value["rich_text"]
        )
    if "date" in property_value:
        return (property_value.get("date") or {}).get("start")
    if "relation" in property_value:
        return sorted(
            item.get("id") for item in property_value["relation"] if item.get("id")
        )
    for name in ("number", "url", "checkbox"):
        if name in property_value:
            return property_value[name]
    return property_value


def related_properties_changed(existing_page, desired_properties):
    existing_properties = (existing_page or {}).get("properties") or {}
    for name, desired in desired_properties.items():
        actual_value = get_plain_property_value(existing_properties.get(name))
        desired_value = get_desired_property_value(desired)
        if isinstance(actual_value, str) and isinstance(desired_value, str):
            if actual_value[:19] == desired_value[:19] and "T" in desired_value:
                continue
        if actual_value != desired_value:
            return True
    return False


def normalize_stable_key(value):
    if isinstance(value, str):
        return value.strip()
    number = to_number(value)
    if number is not None:
        return str(number)
    return to_text(value).strip()


def query_related_pages(target_id, book_page_id):
    pages = []
    start_cursor = None
    while True:
        body = {
            "filter": {
                "property": "书籍",
                "relation": {"contains": book_page_id},
            },
            "page_size": 100,
        }
        if start_cursor:
            body["start_cursor"] = start_cursor
        response = client.request(
            path=f"data_sources/{target_id}/query", method="POST", body=body
        )
        pages.extend(response.get("results") or [])
        if not response.get("has_more"):
            return pages
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            return pages


def upsert_related_rows(relation_name, book_page_id, stable_property, rows):
    if not rows or get_property_type(relation_name) != "relation":
        return (0, 0, 0)
    target_id, _ = get_relation_target(relation_name)
    schema = get_data_source_schema(target_id)
    if "书籍" not in schema or stable_property not in schema:
        raise Exception(
            f"{relation_name} 数据库缺少 书籍 或 {stable_property} 属性"
        )
    existing_index = {}
    for page in query_related_pages(target_id, book_page_id):
        key = normalize_stable_key(
            get_plain_property_value((page.get("properties") or {}).get(stable_property))
        )
        if key and key not in existing_index:
            existing_index[key] = page
    created = updated = unchanged = 0
    for raw in rows:
        key = normalize_stable_key(raw.get(stable_property))
        if not key:
            continue
        desired = build_properties_for_schema(schema, raw)
        existing = existing_index.get(key)
        if not existing:
            page = client.pages.create(
                parent={"type": "data_source_id", "data_source_id": target_id},
                properties=desired,
            )
            existing_index[key] = page
            created += 1
        elif related_properties_changed(existing, desired):
            client.pages.update(page_id=existing["id"], properties=desired)
            updated += 1
        else:
            unchanged += 1
    print(
        f"{relation_name} 子库: 新增 {created}，更新 {updated}，未变化 {unchanged}"
    )
    return created, updated, unchanged


def build_time_relation_values(timestamp):
    relations = {}
    for name, title in get_period_titles(timestamp).items():
        if get_property_type(name) != "relation":
            continue
        page_id = find_or_create_relation_page(name, title)
        if page_id:
            relations[name] = [page_id]
    return relations


def make_fallback_item_id(prefix, book_id, item):
    value = "|".join(
        to_text(item.get(name))
        for name in ("createTime", "chapterUid", "range", "markText", "content")
    )
    return f"{prefix}-{hashlib.sha1(f'{book_id}|{value}'.encode()).hexdigest()}"


def sync_book_related_content(book_id, book_page_id, chapters, bookmarks, summary, reviews):
    chapter_rows = []
    for uid, chapter in (chapters or {}).items():
        chapter = dict(chapter)
        chapter_uid = chapter.get("chapterUid", uid)
        chapter_rows.append(
            {
                "Name": chapter.get("title") or f"章节 {chapter_uid}",
                "chapterUid": chapter_uid,
                "chapterIdx": chapter.get("chapterIdx"),
                "level": chapter.get("level"),
                "updateTime": chapter.get("updateTime"),
                "readAhead": chapter.get("readAhead"),
                "tar": chapter.get("tar"),
                "blockId": chapter.get("blockId"),
                "书籍": [book_page_id],
            }
        )

    bookmark_rows = []
    for bookmark in bookmarks or []:
        bookmark_id = bookmark.get("bookmarkId") or make_fallback_item_id(
            "bookmark", book_id, bookmark
        )
        bookmark_rows.append(
            {
                "Name": bookmark.get("markText") or "未命名划线",
                "bookmarkId": bookmark_id,
                "bookId": bookmark.get("bookId") or book_id,
                "Date": bookmark.get("createTime"),
                "blockId": bookmark.get("blockId"),
                "bookVersion": bookmark.get("bookVersion"),
                "chapterUid": bookmark.get("chapterUid"),
                "colorStyle": bookmark.get("colorStyle"),
                "range": bookmark.get("range"),
                "style": bookmark.get("style"),
                "type": bookmark.get("type"),
                "书籍": [book_page_id],
                **build_time_relation_values(bookmark.get("createTime")),
            }
        )

    review_items = [item.get("review") or {} for item in (summary or [])]
    review_items.extend(reviews or [])
    review_rows = []
    for review in review_items:
        review_id = review.get("reviewId") or make_fallback_item_id(
            "review", book_id, review
        )
        review_rows.append(
            {
                "Name": review.get("content") or review.get("markText") or "未命名想法",
                "reviewId": review_id,
                "bookId": review.get("bookId") or book_id,
                "Date": review.get("createTime"),
                "abstract": review.get("abstract"),
                "blockId": review.get("blockId"),
                "bookVersion": review.get("bookVersion"),
                "chapterUid": review.get("chapterUid"),
                "range": review.get("range"),
                "star": review.get("star"),
                "style": review.get("style"),
                "type": review.get("type"),
                "书籍": [book_page_id],
                **build_time_relation_values(review.get("createTime")),
            }
        )

    upsert_related_rows("章节", book_page_id, "chapterUid", chapter_rows)
    upsert_related_rows("划线", book_page_id, "bookmarkId", bookmark_rows)
    upsert_related_rows("读书笔记", book_page_id, "reviewId", review_rows)


def iter_month_starts(start_date, end_date):
    current = datetime(
        start_date.year, start_date.month, 1, tzinfo=SHANGHAI_TIME_ZONE
    )
    while current.date() <= end_date:
        yield current
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)


def normalize_daily_read_times(raw_values, stats_year, start_date, end_date):
    daily = {}
    for raw_timestamp, seconds in (raw_values or {}).items():
        try:
            timestamp = int(raw_timestamp)
        except (TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(timestamp, SHANGHAI_TIME_ZONE).date()
        if day.year != stats_year or day < start_date or day > end_date:
            continue
        daily[timestamp] = to_number(seconds) or 0
    return daily


def get_daily_read_times(stats_year, cutoff, anchor):
    base_time = int(
        datetime(stats_year, 1, 1, tzinfo=SHANGHAI_TIME_ZONE).timestamp()
    )
    detail = weread.request(
        "/readdata/detail", mode="annually", baseTime=base_time
    )
    daily = normalize_daily_read_times(
        detail.get("dailyReadTimes"), stats_year, cutoff, anchor.date()
    )
    if daily:
        return daily

    year_start = datetime(stats_year, 1, 1, tzinfo=SHANGHAI_TIME_ZONE).date()
    year_end = datetime(stats_year, 12, 31, tzinfo=SHANGHAI_TIME_ZONE).date()
    range_start = max(cutoff, year_start)
    range_end = min(anchor.date(), year_end)
    month_starts = list(iter_month_starts(range_start, range_end))
    print(
        f"阅读统计: {stats_year} 年度接口没有返回每日明细，"
        f"改用 {len(month_starts)} 个月度周期"
    )
    for month_start in month_starts:
        monthly = weread.request(
            "/readdata/detail",
            mode="monthly",
            baseTime=int(month_start.timestamp()),
        )
        daily.update(
            normalize_daily_read_times(
                monthly.get("readTimes") or monthly.get("dailyReadTimes"),
                stats_year,
                range_start,
                range_end,
            )
        )
    return daily


def get_daily_stats_from_notion(day_target_id, stats_year):
    start_cursor = None
    daily = {}
    while True:
        body = {
            "filter": {
                "and": [
                    {
                        "property": "日期",
                        "date": {"on_or_after": f"{stats_year}-01-01"},
                    },
                    {
                        "property": "日期",
                        "date": {"before": f"{stats_year + 1}-01-01"},
                    },
                ]
            },
            "sorts": [{"property": "日期", "direction": "ascending"}],
            "page_size": 100,
        }
        if start_cursor:
            body["start_cursor"] = start_cursor
        response = client.request(
            path=f"data_sources/{day_target_id}/query",
            method="POST",
            body=body,
        )
        for page in response.get("results") or []:
            properties = page.get("properties") or {}
            start = ((properties.get("日期") or {}).get("date") or {}).get("start")
            seconds = (properties.get("时长") or {}).get("number")
            if not start:
                continue
            try:
                day = datetime.fromisoformat(str(start).replace("Z", "+00:00")).date()
            except (TypeError, ValueError):
                continue
            if day.year == stats_year:
                daily[day] = to_number(seconds) or 0
        if not response.get("has_more"):
            return daily
        start_cursor = response.get("next_cursor")
        if not start_cursor:
            return daily


def render_reading_heatmap_svg(stats_year, daily_seconds):
    year_start = date(stats_year, 1, 1)
    year_end = date(stats_year, 12, 31)
    sunday_offset = (year_start.weekday() + 1) % 7
    total_days = (year_end - year_start).days + 1
    week_count = (sunday_offset + total_days + 6) // 7
    cell = 11
    gap = 3
    grid_left = 54
    grid_top = 54
    width = grid_left + week_count * (cell + gap) + 24
    height = 188
    values = [value for value in daily_seconds.values() if value > 0]
    max_value = max(values, default=0)
    total_hours = sum(values) / 3600
    active_days = len(values)
    colors = ["#ebedf0", "#c6e48b", "#7bc96f", "#239a3b", "#196127"]

    def color_for(seconds):
        if seconds <= 0 or max_value <= 0:
            return colors[0]
        level = min(4, max(1, int((seconds / max_value) * 4 + 0.999)))
        return colors[level]

    lines = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        (
            '<style>text{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;'
            'fill:#57606a}.title{font-size:16px;font-weight:600;fill:#24292f}'
            '.meta{font-size:12px}.label{font-size:10px}</style>'
        ),
        f'<text class="title" x="8" y="21">{stats_year} Reading Heatmap</text>',
        (
            f'<text class="meta" x="8" y="39">{active_days} active days '
            f'&#183; {total_hours:.1f} hours</text>'
        ),
    ]
    for label, row in (("Mon", 1), ("Wed", 3), ("Fri", 5)):
        y = grid_top + row * (cell + gap) + cell - 1
        lines.append(f'<text class="label" x="8" y="{y}">{label}</text>')
    month_names = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    for month, label in enumerate(month_names, start=1):
        first = date(stats_year, month, 1)
        offset = sunday_offset + (first - year_start).days
        x = grid_left + (offset // 7) * (cell + gap)
        lines.append(f'<text class="label" x="{x}" y="50">{label}</text>')
    for index in range(total_days):
        day = year_start + timedelta(days=index)
        offset = sunday_offset + index
        week = offset // 7
        weekday = offset % 7
        x = grid_left + week * (cell + gap)
        y = grid_top + weekday * (cell + gap)
        seconds = daily_seconds.get(day, 0)
        hours = seconds / 3600
        lines.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
            f'rx="2" fill="{color_for(seconds)}">'
            f'<title>{day.isoformat()}: {hours:.2f} hours</title></rect>'
        )
    legend_x = width - 150
    lines.append(f'<text class="label" x="{legend_x}" y="172">Less</text>')
    for level, color in enumerate(colors):
        x = legend_x + 28 + level * (cell + gap)
        lines.append(
            f'<rect x="{x}" y="162" width="{cell}" height="{cell}" rx="2" fill="{color}"/>'
        )
    lines.append(f'<text class="label" x="{legend_x + 103}" y="172">More</text>')
    lines.append("</svg>")
    return "\n".join(lines)


def write_reading_heatmap(day_target_id, stats_year):
    try:
        daily = get_daily_stats_from_notion(day_target_id, stats_year)
        output = Path(
            os.getenv("HEATMAP_OUTPUT") or "OUT_FOLDER/reading-heatmap.svg"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            render_reading_heatmap_svg(stats_year, daily), encoding="utf-8"
        )
        print(f"阅读热力图: 已生成 {output}，包含 {len(daily)} 天")
    except Exception as error:
        print(f"阅读热力图生成失败，书籍同步继续。原因: {error}")


def sync_reading_stats():
    if not is_enabled("SYNC_READING_STATS"):
        return
    raw_year = (os.getenv("SYNC_YEAR") or "").strip()
    stats_year = int(raw_year) if raw_year.isdigit() else datetime.now(SHANGHAI_TIME_ZONE).year
    now = datetime.now(SHANGHAI_TIME_ZONE)
    anchor = now if stats_year == now.year else datetime(
        stats_year, 12, 31, 23, 59, tzinfo=SHANGHAI_TIME_ZONE
    )
    lookback_days = get_positive_int("STATS_LOOKBACK_DAYS", 14)
    cutoff = (anchor - timedelta(days=lookback_days - 1)).date()
    daily_read_times = get_daily_read_times(stats_year, cutoff, anchor)
    day_target_id, _ = get_relation_target("日")
    if not daily_read_times:
        print(f"阅读统计: {stats_year} 年没有可写入的每日阅读明细")
        write_reading_heatmap(day_target_id, stats_year)
        return
    day_schema = get_data_source_schema(day_target_id)
    written = unchanged = 0
    for timestamp, seconds in sorted(daily_read_times.items()):
        day = datetime.fromtimestamp(timestamp, SHANGHAI_TIME_ZONE)
        if day.year != stats_year or day.date() < cutoff:
            continue
        title = get_period_titles(timestamp).get("日")
        if not title:
            continue
        page_id = find_or_create_relation_page("日", title)
        raw = {
            "标题": title,
            "日期": timestamp,
            "时间戳": timestamp,
            "时长": to_number(seconds) or 0,
            "阅读小时数": (to_number(seconds) or 0) / 3600,
            **build_time_relation_values(timestamp),
        }
        desired = build_properties_for_schema(day_schema, raw)
        page = client.pages.retrieve(page_id=page_id)
        if related_properties_changed(page, desired):
            client.pages.update(page_id=page_id, properties=desired)
            written += 1
        else:
            unchanged += 1
    print(
        f"阅读统计 {stats_year}: 写入 {written} 天，"
        f"未变化 {unchanged} 天（回看 {lookback_days} 天）"
    )
    write_reading_heatmap(day_target_id, stats_year)


def get_number_property_value(property_value):
    if not property_value:
        return 0
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if prop_type == "number":
        return value or 0
    if prop_type in {"title", "rich_text"} and value:
        return to_number(value[0].get("plain_text")) or 0
    if prop_type in {"select", "status"} and value:
        return to_number(value.get("name")) or 0
    return 0


def resolve_data_source_id(notion_id):
    try:
        client.request(path=f"data_sources/{notion_id}", method="GET")
        return notion_id
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise

    database = client.request(path=f"databases/{notion_id}", method="GET")
    sources = database.get("data_sources") or []
    if not sources:
        raise Exception(f"数据库 {notion_id} 下没有可用的 data source")
    configured_data_source = os.getenv("NOTION_DATA_SOURCE_ID")
    if configured_data_source and configured_data_source != sources[0].get("id"):
        print(
            "提示: NOTION_DATA_SOURCE_ID 与 NOTION_DATABASE_ID 不匹配，"
            f"已改用数据库的 data source: {sources[0].get('id')}"
        )
    if len(sources) > 1:
        print(
            f"数据库 {notion_id} 包含 {len(sources)} 个 data sources，默认使用第一个: {sources[0].get('id')}"
        )
    return sources[0]["id"]


def sync():
    global client, data_source_id, weread, existing_pages_by_book_id
    secrets = validate_secret_inputs()
    notion_id = extract_notion_id()
    notion_token = secrets["notion_token"]
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    client = Client(
        auth=notion_token,
        log_level=logging.ERROR,
        notion_version=NOTION_VERSION,
    )
    data_source_id = resolve_data_source_id(notion_id)
    print(f"Notion API Version: {NOTION_VERSION}")
    print(f"Notion Data Source ID: {data_source_id}")
    relation_target_cache.clear()
    relation_page_cache.clear()
    relation_error_names.clear()
    data_source_schema_cache.clear()
    load_data_source_schema()
    sync_reading_stats()
    if is_enabled("STATS_ONLY"):
        print("已启用仅同步阅读统计，跳过书籍同步")
        return
    full_sync = is_enabled("FULL_SYNC")
    if full_sync:
        print("已启用全量同步：将逐本匹配 BookId 并原位更新")
    existing_pages_by_book_id = preload_existing_pages()
    books = select_books_for_run(get_books_to_sync(), full_sync)
    failures = []
    metadata_properties = (
        "ISBN",
        "评分",
        "封面",
        "简介",
        "作者",
        "分类",
        "出版社",
        "出版时间",
    )
    max_changes = get_positive_int("MAX_CHANGES_PER_RUN", 50)
    changed_count = 0
    for index, book in enumerate(books):
        sort = book.get("sort") or 0
        content_sort = book.get("contentSort") or 0
        activity_time = book.get("activityTime") or sort
        title = book.get("title") or "未命名书籍"
        cover = (book.get("cover") or "").replace("/s_", "/t7_")
        book["cover"] = cover
        book_id = book.get("bookId")
        if not book_id or not matches_book_filter(book_id):
            continue
        print(f"正在同步 {title}，一共 {len(books)} 本，当前是第 {index + 1} 本。")
        try:
            existing_page = find_book_page(book_id)
            existing_sort = get_existing_book_sort(existing_page)
            refresh_content = should_refresh_content(
                content_sort, existing_sort, full_sync, existing_page
            )
            property_sync = (
                full_sync
                or not existing_page
                or refresh_content
                or activity_time > get_existing_last_read_date(existing_page)
            )
            if not property_sync:
                print(f"跳过未变化书籍: {title}")
                continue
            if not full_sync and changed_count >= max_changes:
                print(
                    f"本次已达到增量写入上限 {max_changes} 本，"
                    "其余变化将在下次定时任务继续处理"
                )
                break
            changed_count += 1
            metadata = {}
            if (
                property_sync
                and has_any_property(metadata_properties)
            ):
                metadata = get_bookinfo(book_id)
            page_id, page_existed = upsert_to_notion(
                book, sort, metadata, existing_page
            )
            if not refresh_content or not book.get("hasNotebook"):
                continue
            chapter = get_chapter_info(book_id)
            bookmark_list = get_bookmark_list(book_id)
            summary, reviews = get_review_list(book_id)
            sync_book_related_content(
                book_id,
                page_id,
                chapter,
                bookmark_list,
                summary,
                reviews,
            )
            annotations = sorted(
                [*bookmark_list, *reviews],
                key=lambda item: get_note_sort_key(item, chapter),
            )
            children, grandchild = get_children(chapter, summary, annotations)
            replace_managed_content(
                page_id,
                children,
                grandchild,
                page_existed=page_existed,
            )
        except Exception as error:
            failures.append((title, book_id, str(error)))
            print(f"同步失败，继续处理下一本: {title} ({book_id}): {error}")

    if failures:
        failed_titles = "、".join(title for title, _, _ in failures[:10])
        raise Exception(
            f"本次有 {len(failures)} 本书同步失败: {failed_titles}。"
            "其余书籍已处理，请查看上方日志定位原因。"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description="Sync WeRead highlights and notes to Notion.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync"],
        help="Command to run. Defaults to sync.",
    )
    parser.parse_args(argv)
    try:
        sync()
    except ConfigError:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
