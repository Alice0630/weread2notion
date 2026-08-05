# WeRead2Notion

将微信读书的书籍、划线和笔记同步到 Notion。

本项目使用微信读书 API Key 读取数据，并通过 GitHub Actions 定时同步到 Notion。新版不再需要复制微信读书 Cookie。

预览效果：https://malinkang.notion.site/weread2notion

> [!WARNING]
> 本分支不会删除已有书籍页面。划线和个人点评只会替换名为“微信读书同步内容（自动更新）”的受管区域，其他正文保持不变。

## 使用现有 Notion 书架

本分支支持把微信读书同步到已有数据库，而不是要求复制新的 Notion 模板。

程序会合并整个电子书书架和笔记本列表，因此没有划线的书也会进入 Notion。定时任务按最近阅读优先，只更新新增或最近阅读/新增笔记的书，每次最多写入 50 本；已有页面在载入一次索引后按 `BookId` 匹配，不再为 503 本书逐本查询 Notion。

数据库至少需要以下可写属性：

| 属性 | 类型 | 用途 |
| --- | --- | --- |
| `书名` | Title | 书名，实际名称可不同，程序会自动识别 Title 字段 |
| `BookId` | Text | 微信读书书籍 ID，用于匹配已有页面 |
| `Sort` | Number | 增量同步游标 |
| `阅读状态` | Status | 选项为 `想读`、`在读`、`已读` |
| `阅读时长` | Number | 微信读书累计阅读秒数 |
| `阅读进度` | Number，Percent 格式 | 当前页面显示的阅读进度，数值范围 0 到 1 |
| `时间` | Date | 完成时间 |
| `最后阅读时间` | Date | 最近阅读时间 |
| `开始阅读时间` | Date | 首次阅读时间（接口有值时写入） |

以下已有属性会在微信读书实际返回数据时自动写入；接口没有返回的空值不会覆盖 Notion 原值：

| 属性 | 类型 | 用途 |
| --- | --- | --- |
| `ISBN` | Text | ISBN |
| `评分` | Number | 微信读书评分 |
| `封面` | Files | 外链封面 |
| `简介` | Text | 书籍简介 |
| `作者` | Relation | 查找或创建同名作者并建立关联 |
| `分类` | Relation | 接口返回分类时查找或创建关联 |
| `书架分类` | Select | 微信读书书单中的第一个分类 |
| `年` / `月` / `周` / `日` | Relation | 按最后阅读时间关联现有统计数据库 |

写入关联时，Notion Integration 必须能够访问作者、分类、章节、划线、笔记和日/周/月/年数据库；无权限时程序会保留原关联并在日志中提示。

`阅读时长格式化` 等 Formula 字段由 Notion 自动计算，程序不会写入。程序写入 `阅读进度`，并同步更新模板中存在的 `微信阅读进度` / `微信读书进度` 数字字段。章节、划线、个人想法/点评会按 `chapterUid`、`bookmarkId`、`reviewId` 增量写入现有子数据库，同时保留每本书页面中的受管折叠区。微信读书书签目前只能取得数量，Gateway 不提供书签正文。每日阅读时长默认回看并校正最近 14 天，可用 `stats-only=true` 单独运行统计，避免与书籍全量任务叠加。

### 定时同步

工作流使用 `cron: "0 18 * * *"`，对应北京时间每天 `02:00`。GitHub Actions 的定时任务可能因平台排队延迟几分钟。

### GitHub Secrets

在仓库的 `Settings > Secrets and variables > Actions` 中配置：

- `WEREAD_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

`NOTION_DATABASE_ID` 必须是书架数据库 ID，不能填写包含数据库视图的入口或仪表盘页面 ID。本仓库应填写 `fff7538b60b2815599fdc5d8914f4490`。请删除旧的 `NOTION_DATA_SOURCE_ID` 和 `NOTION_PAGE` Secret，避免旧数据源覆盖正确书架；新版代码在同时存在时也会优先使用数据库 ID。

### 首次迁移

在 `Actions > weread sync > Run workflow` 中：

1. 将 `full-sync` 设为 `true`，全量匹配已有 `BookId`。
2. `batch-size` 建议保持 `25`；`batch-index` 从 `0` 开始。
3. `existing-page-mode=append` 会在旧正文后追加受管同步区域；以后只替换该区域，这是本分支默认值。
4. `existing-page-mode=preserve` 只更新属性，完全不添加受管区域。
5. 可以填写 `book-id`，先只测试一本书；留空时按批次处理。

建议先填一本书的 `book-id`，使用 `full-sync=true`、`existing-page-mode=append` 测试。确认后清空 `book-id`，依次运行 `batch-index=0, 1, 2...`。日志会显示有效批次范围，例如 503 本、每批 25 本时为 `0-20`。失败时只需重跑当前批次，不需要从头开始。旧正文不会自动删除；如果旧程序已经写过一份划线，可能需要人工清理一次旧内容。

如果只需要修复当前年份视图，可填写 `sync-year=2026`，保持 `full-sync=true`、`batch-index=0`。程序只处理最后阅读时间属于 2026 的书，并写入 `年=2026` 关系；若超过 25 本，再继续运行下一批。

首次回填 2026 阅读统计时，可使用 `sync-year=2026`、`stats-only=true`、`stats-lookback-days=366`；正常定时任务保持 `stats-only=false`、`stats-lookback-days=14`。如果年度接口不返回 `dailyReadTimes`，程序会自动按回看范围查询月度 `readTimes` 并按天合并，时长仍统一按秒写入，不再直接跳过阅读统计。

同步完成后，程序会从“日”统计库生成 `OUT_FOLDER/reading-heatmap.svg`。日库继续保留原始秒数，并额外写入数字字段 `阅读小时数` 供周/月/年图表稳定聚合。工作流只提交这一张展示文件，并用该次提交的唯一 SHA 地址更新仪表盘热力图，避免固定 `main` 地址被缓存；书籍、划线、笔记和统计数据库内容不会因此重建。首次上传新版代码后，先运行一次 `stats-only=true`、`stats-lookback-days=366`，即可回填当年每日数据并刷新热力图；以后每天 02:00 的定时任务会自动更新。

### 自定义字段名

复用此 Action 的其他数据库可以通过 Action inputs 修改以下字段名：

- `notion-status-property`
- `notion-duration-property`
- `notion-progress-property`
- `notion-completed-date-property`
- `notion-last-read-date-property`
- `notion-start-read-date-property`

## 使用文档

完整教程请查看：

https://www.notionhub.app/docs/weread2notion.html

文档里包含：

- Notion 模板复制和授权
- 微信读书 API Key 获取
- GitHub Fork 和 Actions 配置
- 常见问题排查

## 关注公众号

如果你想获取后续更新，或了解更多 Notion 自动化工具，欢迎关注公众号：**Notion自动化**。

![公众号：Notion自动化](https://cdn.notionhub.app/notionhub/gzh.jpg)
