# 真实动态网页样本

`robodojo-shell.html` 是 2026-10-03 通过普通 HTTP 获取的公开 RoboDojo 首页。
`robodojo-rendered.html` 是同日在独立 Cloud Run Job `kb-bot-dynamic-probe`
执行固定 Playwright 1.55.0 / Chromium 后得到的公开 DOM，无登录、无用户信息。
来源：https://robodojo-benchmark.com/ 。探针不调用模型、不写正式 Notion。

回归覆盖空壳识别、真实正文、排行榜表头/数据、图片候选以及重复解析不受缓存影响。
HTML 保持实际抓取结构，测试中的其他小片段仅用于边界条件。
