# 备份与隔离恢复

## 当前方案

`app.backup` 导出一个独立 ZIP；`app.restore` 从 ZIP 创建全新的测试资料库和笔记库。
不调用 LLM，不重新抓原平台，不把原生产页面归档或重建。

- Notion：枚举 Bot 的资料库与笔记库（ID 来自服务配置），以及可选的 `NOTION_LIFE_*`、`NOTION_WIKI_*` 额外库（留空即跳过；Wiki 层见 [wiki-layer.md](wiki-layer.md)）；
  完整分页的正文块及嵌套结构、同步块源与引用、封面和文件属性。
- 附件：下载 Notion 文件的实际字节，避免临时 URL 过期后无法恢复；同时备份 GCS
  原件桶中的所有对象，包含对象名、generation、MIME 和自定义 metadata。
- Firestore：递归导出文档和子集合，以 protobuf 保存字段类型，包括时间、bytes、大整数。
  `records`、`urls`、`updates`、`staged`、运行配置等均包含在内；排除本工具自己的
  `restore_drills` 测试集合。运行配置可能含供应商密钥，因此 ZIP 只存入私有备份桶，
  本机文件权限为 `0600`，不要放进 Git。
- 关联完整性：某个关联指向的库如果没有共享给 kb-bot integration，Notion API 会把该属性从库结构里隐去，页面上只返回空列表，也不报错。备份会对比页面属性与库结构，发现这种情况就中止，不发布缺关联的快照。
- 完整性：每项文件有 SHA-256 和字节长度；缺附件、页在采集期间变化、源桶对象或
  Firestore 文档在采集期间变化，均中止，不发布完成备份。跨服务没有事务，采集以
  开始/结束时间记录，并再次比较源数据；快照不是任意时刻的事务级回滚点。

独立私有桶：`gs://<GCP_PROJECT>-kb-backups`。`snapshots/` 保存时间戳命名的完整备份，
`restores/<run_id>/` 保存恢复测试的 GCS 副本，`drills/` 保存验证报告。
桶启用了统一 IAM 和禁止公开访问。备份对象创建时禁止覆盖；当前不自动删除历史备份。

Bot 服务升级不会自动更新备份 Job，需要时重新运行 `tools/schedule_backup.py`。

## 准备

备份不在 `deploy.sh` 里，需要先手工建一个私有桶和一个只读服务账号：

```bash
gcloud storage buckets create gs://$GCP_PROJECT-kb-backups --location=us-central1 \
  --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create kb-bot-backup-sa --display-name="kb-bot backup"
```

`tools/schedule_backup.py` 会给这个账号绑定所需的最小权限。

## 手动备份与校验

本机先登录 `gcloud`。`--local-gcloud` 从既有 Cloud Run 服务读取库 ID/桶名，
凭据仅在内存中使用，不要求把 token 复制进命令行或文件。

```bash
.venv/bin/python -m app.backup --local-gcloud --project=<GCP_PROJECT> create \
  --output=/tmp/kb-backup-新的时间戳.zip --bucket=<GCP_PROJECT>-kb-backups
.venv/bin/python -m app.backup verify /tmp/kb-backup-新的时间戳.zip
```

只下载 ZIP 就可校验它，无需访问原 Notion 或原件桶。恢复正文及附件从 ZIP 读取，
不会使用备份里已经过期的 Notion 文件 URL。

## 隔离恢复

```bash
.venv/bin/python -m app.restore /tmp/kb-backup-新的时间戳.zip \
  --local-gcloud --project=<GCP_PROJECT> \
  --parent=<测试父页面ID> \
  --bucket=<GCP_PROJECT>-kb-backups \
  --material=备份里的一条原材料ID \
  --report=/tmp/kb-restore-新的时间戳.json
```

每次创建独立的父页面及两个新库。`--material` 可重复；省略时恢复全部页面。
会自动包含所选材料的全部关联笔记及它们的其他关联资料，避免把同一笔记的关系拆开。

恢复先创建所有新页面，再按同步块依赖顺序写正文，最后补 relation。
新页面、新块 ID、笔记引用、正文中的已知内部链接和 GCS 指针一起重映射。
Firestore 只把该小组的映射恢复到 `restore_drills/<run_id>/...`，包括指向这些
`records` 的 URL 去重索引。笔记正文块 ID 按恢复后的实际块重新定位，不复活历史失效 ID。
原始全部 Firestore 文档仍在 ZIP 中；本工具不把生产任务状态直接重放进在线队列。

恢复器逐页比对正文结构、可写属性、标签与阅读状态，逐个比较附件字节，并读回关联和
同步引用。还会在测试笔记里临时编辑一段正文，确认资料页显示随之变化，再恢复原文。
报告的 `status=verified` 代表这些检查全部完成。

如果 Notion 附件块被删过，对具有原始链接且完全缺少媒体块的材料，
会从 ZIP 中找到符合 bot 的 `<URL hash>-<序号>` 命名及 MIME 的 GCS 原图/PDF，
恢复到测试页「从 GCS 原件恢复的附件」中；按字节去重。不猜测其他文件归属。

已确认写入的批次会记录 ID；请求被拒绝或读请求失败后可用**同一条命令加 `--resume`**
继续同一测试位置。写请求响应丢失时不能假定没写进去：工具不会盲目重试该请求，
需核对报告与测试页；最终结构校验不会把多出来的内容判成成功。

### API 范围

Notion 不允许写回原页面 ID、自动编号数值或原创建时间；恢复会分配新 ID 和独立的
自动编号前缀。原编号、原时间和其他只读属性完整保留在 ZIP 和报告中。
relation、rollup 和同步引用使用新副本；按钮、视图、权限、评论与版本历史不是本次
恢复范围。API 返回的 `paragraph.icon=null` 读取时保留、写入时省略。

现有页面的段落、标题（含 heading_4）、列表、待办、折叠块、callout、书签、分隔线、表格和同步块已实测恢复；表格连同首批行一起创建（Notion 不接受空表），超过 100 行或单次请求放不下的行随后追加；
媒体块支持重新上传文件。API 无法读取的 `unsupported`、内嵌子页面/子库会使备份失败；
恢复遇到尚未支持的块会在创建测试位置前拒绝，不静默丢内容。
自定义 `status` schema 也不会在旧 API 上猜测重建；当前两个库的状态均为 `select`。

## 定期执行

安装命令只接受通过真实恢复与附件演练的报告。报告中的恢复范围有同步块时，还必须通过同步编辑演练；新格式报告还必须覆盖所有备份库：

```bash
.venv/bin/python tools/schedule_backup.py --report=/tmp/kb-restore-通过验证.json
```

它创建 `kb-bot-backup` Cloud Run Job，并先实际执行成功，之后才配置
`kb-bot-backup-daily` Cloud Scheduler：每天 03:00，`USER_TIMEZONE`（默认 Asia/Shanghai）。
需要改时区可传 `--time-zone`。采用 [Cloud Run Job 的定时执行方式](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)。

同一每日维护时段还有 `kb-bot-retry-daily`：每天 03:10，`USER_TIMEZONE`（默认 Asia/Shanghai），以 bot
现有账号的 OIDC 身份调用 `/tasks/retry`，执行与 Telegram `/retry` 相同的恢复流程。
备份与恢复各自调度，备份失败不阻止重试；03:10 是错开通常的采集时间，并非等待备份
完成的依赖。自动重试不发 Telegram 进度或汇总，结果记入 `auto-retry` 日志。
失败材料继续保留，预算用完下次继续；手动和自动重试通过 Firestore 租约避免并行。
备份账号仍保持只读，不承担这些恢复写入。

等 LLM 的消息已经由 `kb-bot-drain` 每 15 分钟自动唤醒。3 天等待上限保留，不因每日
重试重置；到期链接转入暂存区，不删除正文来源或想法，之后也可被每日重试恢复。
图片和文件到期后的恢复仍需按通知重新发送。

专用账号 `kb-bot-backup-sa` 只有 Firestore 读取、原件桶读取、备份桶创建对象、
指定 Notion secret 读取，以及执行该备份 Job 的权限；没有生产 Firestore 写入或删除
备份对象的权限。Job 仅载入 Notion token，不载入 Telegram token 或 LLM 密钥。
镜像与 HTTP bot 服务分开，后续 bot 的部署不会覆盖备份 Job。

检查最近执行及失败：

```bash
gcloud run jobs executions list --job=kb-bot-backup --region=us-central1 --project=<GCP_PROJECT>
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="kb-bot-backup" AND severity>=ERROR' \
  --project=<GCP_PROJECT> --freshness=2d --limit=20
gcloud storage ls `gs://<GCP_PROJECT>-kb-backups/snapshots/` --recursive
```

失败会使 Job 非零退出，Cloud Run 重试一次；没有生成完成 ZIP 就不是成功备份。
这里没有自动删除、备份管理界面或通知渠道；查看 Job 执行状态和日志即可。

## 正式标签词表与任务恢复

现有 Firestore 导出递归读取所有生产集合，因此自动包含 `settings/tags`、
`settings/tag_navigation`、`tag_versions`、`tag_operations`；备份镜像无需增加集合白名单。
待同步版本、上一版和按数据库保存的同步进度均随状态文档归档。

隔离恢复工具也将这些状态写入 `restore_drills/{run_id}/...` 并读回核对，
不会把测试配置发布给生产 Bot 或自动执行恢复出的操作。恢复测试包括词表版本、
待同步配置、任务进度，以及已恢复页面的导航 ID 重映射。生产灾难恢复时，应先核对
数据库 ID 与页面映射，再恢复正式配置；未完成任务可从保存的检查点继续。
