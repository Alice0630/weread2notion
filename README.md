# WeRead2Notion

将微信读书的书籍、划线和笔记同步到 Notion。

本项目使用微信读书 API Key 读取数据，并通过 GitHub Actions 定时同步到 Notion。新版不再需要复制微信读书 Cookie。

预览效果：https://malinkang.notion.site/weread2notion

> [!WARNING]
> 本分支不会删除已有书籍页面。已有页面默认只更新数据库属性；同步划线和笔记只会替换名为“微信读书同步内容（自动更新）”的受管区域，其他正文保持不变。

## 使用现有 Notion 书架

本分支支持把微信读书同步到已有数据库，而不是要求复制新的 Notion 模板。

定时任务会更新所有书籍的阅读状态、时长和进度；只有新增书籍、`Sort` 游标变化或手动选择全量同步时，才会重新读取划线和笔记正文。

数据库至少需要以下可写属性：

| 属性 | 类型 | 用途 |
| --- | --- | --- |
| `书名` | Title | 书名，实际名称可不同，程序会自动识别 Title 字段 |
| `BookId` | Text | 微信读书书籍 ID，用于匹配已有页面 |
| `Sort` | Number | 增量同步游标 |
| `阅读状态` | Status | 选项为 `想读`、`在读`、`已读` |
| `阅读时长` | Number | 微信读书累计阅读秒数 |
| `微信读书进度` | Number，Percent 格式 | 可写的阅读进度，数值范围 0 到 1 |
| `时间` | Date | 完成时间 |
| `最后阅读时间` | Date | 最近阅读时间 |

`作者`和`分类`如果是 Relation 字段，本分支会保留原值，不会用文本覆盖关系。公式字段也不会被写入。

### GitHub Secrets

在仓库的 `Settings > Secrets and variables > Actions` 中配置：

- `WEREAD_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

`NOTION_DATABASE_ID` 必须是书架数据库 ID，不能填写包含数据库视图的入口或仪表盘页面 ID。使用数据库 ID 时应删除旧的 `NOTION_PAGE` Secret，避免填错目标。

### 首次迁移

在 `Actions > weread sync > Run workflow` 中：

1. 将 `full-sync` 设为 `true`，全量匹配已有 `BookId`。
2. `existing-page-mode=preserve` 只更新属性，完全保留旧正文。
3. `existing-page-mode=append` 会在旧正文后追加受管同步区域；以后只替换该区域。
4. 可以填写 `book-id`，先只测试一本书；留空时处理所有书籍。

建议第一次先使用 `preserve` 检查属性结果。确认无误后，如果需要刷新旧页面的划线，再使用 `append` 运行一次。旧正文不会自动删除，可能需要人工清理一次历史同步内容。

### 自定义字段名

复用此 Action 的其他数据库可以通过 Action inputs 修改以下字段名：

- `notion-status-property`
- `notion-duration-property`
- `notion-progress-property`
- `notion-completed-date-property`
- `notion-last-read-date-property`

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
