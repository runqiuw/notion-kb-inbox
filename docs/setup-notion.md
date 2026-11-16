# Notion 配置

Notion 是阅读界面：资料库放外部材料，想法与笔记放你自己的想法，两库双向关联。
**代码按中文属性名写入**，属性或选项改名都会导致写入 400。完整字段说明见 [notion-schema.md](notion-schema.md)。

## 1. 建 integration

1. 打开 <https://www.notion.so/profile/integrations>，新建 **Internal** integration。
2. Capabilities 勾选读取、更新、插入内容，以及读取评论（可选）。
3. 复制 token（`ntn_...`），填到 `NOTION_TOKEN`。

## 2. 准备一个父页面

在 Notion 里新建一个空页面，右上角 `···` → **Connections** → 加入刚才的 integration。
**漏了这一步，API 会对所有请求返回 404。** 子页面和子数据库会继承这个授权。

## 3. 建数据库（三选一）

### A. 复制模板（最省事）

打开 [Notion 模板页](https://glacier-beard-e5e.notion.site/41a9ba3b59818181a170c5e9c94c6bc8)，
右上角「复制」到你的工作区。模板分两层：

- **🧠 知识库**：作者实际在用的布局。首页待读卡片、最近想法 / 聊天提炼两栏、标签入口页、使用说明，
  资料页模板（图文／网页、论文、视频、聊天记录）和「记下想法」按钮，外加几条示例数据（可删）。
- **📖 Wiki**（可选）：chatbot 维护的主题条目层，带两个 skill，见 [wiki-layer.md](wiki-layer.md)。不用可以直接删掉这一页。

> 首页和标签页的视图都只显示「样例来源 = 用户提供」的条目，bot 写入时会自动填这个值；
> 想隐藏某条演示数据，把它改成「公开材料演示」即可。

复制后：

1. 对复制出的页面做第 2 步（Connections 加入 integration）。
2. 分别打开两个数据库（点数据库标题 → 以整页打开），链接里 `?v=` 前面那 32 位就是 ID，
   填进 `.env` 的 `NOTION_MATERIAL_DB`（「全部资料」）、`NOTION_THOUGHT_DB`（「全部笔记」）。
   两个库在「知识库」页底部「资料与导航 · 使用说明」折叠块里。
3. 首页 callout 里的 `t.me/你的bot用户名` 换成你自己 bot 的链接。

「记下想法」按钮复制后会自动指向你复制出来的「全部笔记」，不用改。

### B. 用脚本建（适合交给 coding agent）

```bash
NOTION_TOKEN=ntn_... python tools/setup_notion.py --parent <父页面链接或ID>
```

脚本会在父页面下新建「科研阅读与思考」，里面放两个数据库，并打印：

```
NOTION_MATERIAL_DB=...
NOTION_THOUGHT_DB=...
```

把这两行填进 `.env`。根页面标题可用 `--title` 改，不影响 bot。
加 `--wiki` 会在同一父页面下顺便建好 Wiki 层的页面和两个库（[wiki-layer.md](wiki-layer.md)）。

### C. 手工建

逐字照抄 [notion-schema.md](notion-schema.md) 的属性名和选项名。关联要先在「想法与笔记」建
「关联资料」指向资料库，再把资料库那边自动生成的反向字段改名为「关联笔记」。

## 4. API 建不了，需要手工补的部分（用 B / C 时）

这些只影响阅读体验，不影响 bot 写入，可以以后再做，也可以照着模板页做：

- **视图**：全部资料（表格）、待读（画廊或列表，筛选「阅读状态」为待读或阅读中）、
  已读、待处理（「处理状态」不等于完成）。画廊卡片预览选「缩略图」属性。
- **首页布局**：顶部放常用标签链接，主区放待读视图，下方放「最近想法」（笔记库视图）。
- **「记下想法」按钮属性**：在资料库加一个 Button 属性，动作为「在想法与笔记新建页面，
  关联资料 = 当前页」。Notion API 不支持创建按钮。
- **「记录编号」「卡片信息」**：脚本会尝试创建；如果工作区不支持，会打印警告，手工加即可。

## 5. 标签

「主题」是多选标签，初始为空。正式词表保存在 Firestore，部署后在 Telegram 里用 `/tags` 管理；
词表说明见 [tags.md](tags.md)，建议换成你自己的领域。

## 常见问题

- **写入 400 `xxx is not a property that exists`**：属性名对不上，检查是否有空格或简繁体差异。
- **写入 404**：根页面没加 Connections，或 database id 填错。
- **删除或改名已有选项**：会让写入失败。新增选项不需要改代码。
