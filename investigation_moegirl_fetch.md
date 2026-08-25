# 萌娘百科 fetch 调查记录

## 页面与浏览器实测

- 目标：`https://zh.moegirl.org.cn/%E5%8F%AF%E5%A1%91%E6%80%A7%E8%AE%B0%E5%BF%86`
- 浏览器实际加载成功；页面总高度约 5,256 px。
- 页面包含“各话标题”表格，表头为“话数、日文标题、中文标题、剧本、分镜、演出、作画监督”。
- 浏览器提取出的表格连续包含 `#01` 至 `#13` 共 13 行，说明原网页和客户端 DOM 的表格数据本身完整，并非只有第一集。
- 浏览器原始 HTML 已保存到：`/home/ubuntu/browser_html/zh_moegirl_org_cn_______1787688971260.html`。

下一步将调用项目当前的 `extract_body_blocks` 与 `build_model_facing_html`，比较表格在提取、转换和 20,000-token 截断各阶段的保留情况。
## 转换链路量化对照

原始 DOM 中命中“各话标题”的普通 `wikitable` 表格具有 14 个 `tr` 行：1 行表头与 `#01` 到 `#13` 共 13 集数据。该表的属性仅为 `class="wikitable"` 和居中、小字号样式，未发现折叠、动态加载或非标准嵌套表格要求。

当前 `extract_body_blocks` 调用 Trafilatura 后，输出 XML 只剩 2 行：表头及 `#01`。默认、`favor_recall=True` 与 `favor_precision=True` 三种模式均只保留 `#01`，说明现有两级回退策略无法恢复后续行。随后 HTML 转换器忠实渲染了这个已经丢失行的 XML 表格，因此最终模型结果也只含 `#01`。

最终模型结果为 4,477 tokens，远低于 `FETCH_BODY_TOKEN_BUDGET=19,000` 与 `FETCH_RESPONSE_TOKEN_BUDGET=20,000`，而且没有截断提示。因此不能归因于 token 预算或最终 `_truncate_blocks`；丢失发生在 Trafilatura 的 HTML→XML 正文提取阶段。当前转换链路对该页面的表格保真性确实不足。

建议在 Trafilatura XML 提取之后增加“原始 DOM 表格回填”步骤：对原始 DOM 中行数显著多于同位置转换表格的 `wikitable`，按行解析为受控的 `<table><tr><td>…</td></tr></table>` 块，并在章节锚点位置替换或插入。仅提高 token 预算、调整最终截断、或切换 Trafilatura 的 recall/precision 参数均不会解决此页问题。
