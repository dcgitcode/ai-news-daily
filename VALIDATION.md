# 验证记录

## 中文默认版更新（2026-09-06）

默认来源已切换 IT之家和极客公园，arXiv/GitHub 默认关闭；增加标题与可见摘要中文过滤。12 项测试通过，包括拒绝英文标题/英文摘要。真实抓取分别返回 60、30 条，筛选去重后 21 个候选，展示前 12 条。examples/live 与 examples/demo 均已更新为中文版。邮件/微信与云端部署的未验证范围不变。

## 初版历史记录（以下为切换中文源之前的数据）

2026-09-06，在 Windows / Python 3.12 的独立虚拟环境中验证。

- requirements.txt 安装成功。
- `python -m unittest discover -s tests -v`：11 项测试通过。
- 覆盖关键词边界、过期/未来/无日期条目、URL 规范化、去重、HTML 转义、RSS 解析、全源/部分源失败、平台业务错误、分渠道重试、预览不修改状态。
- `python -m news_digest --demo`：成功，离线示例见 examples/demo/latest.html。
- `python -m news_digest --dry-run`：退出码 0；OpenAI 1170、Hugging Face 859、DeepMind 100、arXiv 40、GitHub 30 个原始条目；按默认规则筛选去重后 26 个候选，报告列出前 12 个。数量只是此次抓取快照，不表示每次相同。
- 真实抓取日报保存为 examples/live/latest.html，状态见 examples/live/status.json。

尚未验证：真实 SMTP/微信发送（未提供凭据）；GitHub 托管 runner 定时执行和远程状态分支写入（未创建远程仓库）。推送测试使用模拟响应，不代表真实送达。
