# 工具折叠块显示文案对照表

本文档描述**当前代码实际渲染**的折叠块标题：工具组折叠块（外层 `<details>`）
与单工具折叠块（内层 `<details>`）在进行时与完成态分别显示什么。
对应实现：`src/ai/tool_summary.py`（单块摘要）、`src/ai/rich_message_builder.py`
（组摘要与组类型派生）、`src/tool_result_format.py`（失败标题与展开正文）。

> 维护约定：修改任一摘要文案时，请同步更新本文件三张表，并跑一遍
> `python -m pytest tests/unit/test_tool_ui_fixes.py` 回归。

## 表1 · 运行时 vs 运行后：每个工具的折叠块标题

`{n}` = 组内同类成功调用数；组标题"单数 / 复数"两个模板，调用数为 1 用前者。

| 工具 | 运行时·工具组折叠块 | 运行时·工具折叠块 | 运行后·工具组折叠块（成功） | 运行后·工具折叠块（成功） |
|---|---|---|---|---|
| web_search | Searching the web | 搜索词本身（如 `球球大作战 最新活动`） | Searched the web（单复同形） | `搜索词 N results`（解析不出数量时 Searched the web） |
| bash | 命令前 30 字符（带 `description` 时显示意图文本） | 同左 | Ran a command / Ran {n} commands | `description` 文本（必填通常有）；无则 Ran a command；复合命令部分成功为 Ran a command (partial success) |
| text_editor · view | Viewing file {文件名} | 同左 | Viewed a file / Viewed {n} files | Viewed file {文件名} |
| text_editor · create | Creating file {文件名} +n | 同左 | Created a file / Created {n} files | Created file {文件名} +n |
| text_editor · str_replace / insert | Editing file {文件名} +n -n | 同左 | Edited a file / Edited {n} files | Edited file {文件名} +n -n |
| text_editor · delete | Editing file {文件名} | 同左 | Deleted a file / Deleted {n} files | Edited file {文件名} |
| fetch_url | Fetching from {域名} | 同左 | Fetched a page / Fetched {n} pages | Fetched: {页面标题}（退化 Fetched: {域名}） |
| wikipedia | Looking up on Wikipedia | 同左 | Looked up on Wikipedia（单复同形） | Looked up: {词条标题} |
| weather | Fetching weather | 同左 | Fetched weather / Fetched weather for {n} cities | Fetched weather |
| exchange_rate | Checking exchange rates | 同左 | Checked exchange rates（单复同形） | Checked exchange rates |
| geocode | Geocoding address | 同左 | Geocoded an address / Geocoded {n} addresses | Geocoded an address |
| route | Planning route | 同左 | Planned a route / Planned {n} routes | Planned a route |
| distance | Measuring distance | 同左 | Measured a distance（单复同形） | Measured a distance |
| poi_keyword_search | Searching POI by keyword | 同左 | Searched POIs by keyword（单复同形） | Searched POIs by keyword |
| poi_nearby_search | Searching nearby POI | 同左 | Searched nearby POIs（单复同形） | Searched nearby POIs |
| poi_details | Fetching POI details | 同左 | Fetched POI details / Fetched details for {n} POIs | Fetched POI details |
| generate_image · 不带 image_url（文生图） | Generating an image / Generating {n} images | 同左 | Generated an image / Generated {n} images | 同组标题 |
| generate_image · 带 image_url（编辑） | Editing an image | 同左 | Edited an image | 同组标题 |
| generate_video | Generating a video | 同左 | Generated a video / Generated {n} videos | Generated a video |
| message_user | Waiting for your answer | Waiting for your answer | Messaged you（单复同形） | Selected: {选项…} / User provided a custom answer / User cancelled / User is away (no reply) / User answered |
| present_files | Presenting file(s) | 同左 | Presented a file / Presented {n} files | Presented file（≤1）/ Presented {n} files |
| todo · list | Listing todos... | Listing todos | Listed todos（单复同形） | Listed todos |
| todo · add | Adding a todo... | Adding a todo | Added a todo / Added {n} todos | Added todo {标题}（无标题 Added a todo） |
| todo · done | Completing a todo... | Completing a todo | Completed a todo / Completed {n} todos | Completed todo {标题} |
| todo · undone | Reopening a todo... | Reopening a todo | Reopened a todo / Reopened {n} todos | Reopened todo {标题} |
| todo · toggle | Updating a todo... | Updating a todo | Completed a todo / Reopened a todo（按结果方向） | Completed / Reopened todo {标题}（按 `todo.done`） |
| todo · edit | Updating a todo... | Updating a todo | Updated a todo / Updated {n} todos | Updated todo {标题} |
| todo · delete | Deleting a todo... | Deleting a todo | Deleted a todo / Deleted {n} todos | Deleted todo {标题} |
| todo · clear | Clearing the todo list... | Clearing the todo list | Cleared the todo list（单复同形） | Cleared {n} todos（0 条时 Cleared the todo list） |
| memory · list | Listing memories... | Listing memories | Listed memories（单复同形） | Listed memories |
| memory · search | Searching memories... | Searching memories | Searched memories（单复同形） | Searched memories |
| memory · add | Saving a memory... | Saving a memory | Saved a memory / Saved {n} memories | Saved memory: {内容摘要} |
| memory · get | Retrieving a memory... | Retrieving a memory | Retrieved a memory / Retrieved {n} memories | Retrieved memory: {内容摘要} |
| memory · update | Updating a memory... | Updating a memory | Updated a memory / Updated {n} memories | Updated memory: {内容摘要} |
| memory · delete | Deleting a memory... | Deleting a memory | Deleted a memory / Deleted {n} memories | Deleted memory: {内容摘要} |
| memory · clear | Clearing memories... | Clearing memories | Cleared memories（单复同形） | Cleared {n} memories（0 条时 Cleared memories） |
| subagent | Delegating to a subagent... | Running a subagent（数秒内被进度卡片替换为 🤖 子 agent 运行中） | Ran a subagent / Ran {n} subagents | Ran a subagent |
| deliver_reply · send=true | Delivering the final reply... | Delivering the final reply | Delivered the final reply（单复同形） | Delivered the final reply |
| deliver_reply · send=false / TIMER 未填 | Delivering the final reply... | Delivering the final reply | Skipped the final reply（单复同形） | Skipped the final reply |

标题截短规则：todo 标题 / memory 内容摘要压缩空白后按 24 字符截断（超出加 `…`）。

## 表2 · 失败 / 特殊状态

| 场景 | 工具折叠块显示 | 对工具组折叠块的影响 |
|---|---|---|
| 部分失败（如 4 中 1 败） | 各失败块显示自己的失败标题 | Ran a command, fetched 2 pages, **(failed 1)** |
| 全部失败（如 2 中 2 败） | 同上 | 只显示 **(failed 2)** |
| 无成功也无失败（异常兜底） | — | Tools failed |
| 工具超时 | ⏱️ {label} timed out（label：Web search / Page fetch / Wikipedia lookup / Weather fetch / File presentation 等，其余用工具名） | 计入 (failed n) |
| bash 退出码≠0 | ❌ Bash 执行失败 | 计入 (failed n) |
| bash 复合命令部分成功（127） | Ran a command (partial success) | **算成功**，不计 failed |
| web_search 失败 | Search failed | 计入 (failed n) |
| fetch_url 失败 | 🌐 Failed to fetch {域名} | 计入 (failed n) |
| weather 失败 | 🌤️ 天气查询失败 | 计入 (failed n) |
| 地图类失败 | ❌ 📍 地理编码失败 / ❌ 🚗 路线规划失败 等 | 计入 (failed n) |
| text_editor 失败 | ❌ 文件操作未完成 | 计入 (failed n) |
| todo 失败 | ❌ 待办操作失败：{code} | 计入 (failed n) |
| memory 失败 | ❌ 记忆操作失败：{code} | 计入 (failed n) |
| subagent 失败 | ❌ {模型} 失败 · {n} 轮 · {错误} | 计入 (failed n) |
| deliver_reply 失败（无正文 / 发送异常） | ❌ 最终回复未交付 | 计入 (failed n) |
| 图片 / 视频失败 | 🎨 图片生成失败 / 🎨 图片编辑失败 / 🎬 视频生成失败 | 计入 (failed n) |
| 执行器未捕获异常 | ⚠️ {工具名} failed | 计入 (failed n) |
| 单轮预算耗尽被跳过 | Not executed (budget) | 计入 (failed n) |
| 流式占位（工具名未到达） | Preparing tool call...（能识别 text_editor 参数时显示 Creating file 等） | Working... |
| message_user 等待作答 | Waiting for your answer（正文占位 Waiting...） | Waiting for your answer |
| 无输出时的展开正文占位 | running→Running... / waiting→Waiting... / done→Done / error→Failed | — |

## 表3 · 通用规则（适用于所有工具）

| 规则 | 说明 |
|---|---|
| 组标题跟随进度 | 运行时组标题取组内**最后一个活跃工具**的文案，批量执行时随进度切换 |
| 动作型工具实时刷新 | todo / memory 的进行态标题随参数流中的 `action` 实时变化（如 Adding a todo... → Clearing the todo list...） |
| 完成态大小写 | 组内第一条描述首字母大写，后续全部小写（如 `Ran a command, saved a memory, added a todo`）；无任何豁免 |
| 完成态聚合 | 成功工具按「组类型」聚合计数；todo / memory 按 action 派生组类型，generate_image 按 image_url 是否携带派生（image_generate / image_edit，对标 text_editor 按 command 派生），toggle 方向与 deliver_reply 是否静默从单块最终摘要回推 |
| 统一图像工具新旧名 | generate_image 为统一入口（image_url 缺省=文生图、提供=编辑）；旧名 generate_image_from_text / edit_image_with_reference 仍可分发但不再进入工具清单，折叠块文案按各自语义（文生图/编辑）显示，与同名新工具一致 |
| `description` 优先 | 参数带 `description/_summary` 时，组标题与工具标题（运行时+完成后）优先显示它；现仅 bash 声明；web_search / text_editor / todo / memory / subagent / deliver_reply 始终按规范文案生成 |
| 单复数 | 组内同类工具 ≥2 时切复数模板；不可数对象的动作（Listed todos / Searched memories 等）单复同形 |
| "Ran an action" 兜底 | 仅当未知工具名漏过所有分支时出现；当前全部已声明工具均有专属文案 |
| 展开正文形态 | bash / text_editor / message_user / exchange_rate / deliver_reply 等为 `pre/code` 等宽面板；web_search 为富文本紧凑列表（标题链接 + 域名/时间/评分徽标，**不渲染** `🔍 「query」 引擎 · N/M 条` section 头与摘要 snippet——摘要只进模型上下文，前端多结果累计太长）；images / videos / lens 各 section 仍带头行；todo / memory / subagent / weather 为富文本卡片；图片 / 视频为媒体卡片 |
