# 连接词塔

Lingather 获取并整理材料，词塔处理权限、预览、额度、提交与任务状态。词塔是可选输出，不阻断其他功能。现有协议 skill_version 与发布版本号不同，不手动重写官方 config.json。

## 选择连接方式

用户已连接词塔 MCP 时，优先让 Agent 用已发现的 MCP 工具完成业务请求。Lingather 本地仅准备材料，不读取或提取连接器表单 Key。工具名可能有客户端前缀，以实际 schema 为准。

用户安装普通词塔 Skill 时，继续使用 exporters/wordtower.py 的现有一键命令，通过官方 request.py 调用网关；Key 留在本地 WORDTOWER_API_KEY，不放在聊天、命令参数、日志或交接文件中。

普通安装可用 `npx skills add word-tower/skills -g`；Claude Code 也可通过插件市场添加 word-tower/skills，再安装 wordtower@wordtower。不要为同一客户端重复安装。

## 定位当前官方 Skill

从当前客户端已加载的词塔 Skill 取得实际目录。导出器依次支持：

- `--request-script /absolute/path/wordtower/scripts/request.py`（放在 epub/text/youtube/resume 子命令前）。
- `WORDTOWER_REQUEST_SCRIPT` 或 `WORDTOWER_SKILL_DIR` 本地环境变量。
- 项目和常见用户 Skill 目录自动发现。

Claude 插件缓存位置不做跨版本猜测；将实际加载目录传入上述选项。request.py 必须与同目录包的 config.json 配套，支持 JSON3 和 --prepare-only。不要只从别处复制一份脚本、修改认证地址或静默使用旧版。

## MCP 本地材料交接

已经取得文件时执行：

```sh
python3 {baseDir}/scripts/exporters/wordtower.py text article.md --prepare-only --output text-request.json
python3 {baseDir}/scripts/exporters/wordtower.py youtube "https://youtu.be/aDblEBU41nA" --subtitles captions.json3 --prepare-only --output video-request.json
python3 {baseDir}/scripts/exporters/wordtower.py epub selected.epub --prepare-only --output upload-request.json
```

这三个命令不会读取词塔 Key、调用词塔业务 API、上传或扣额度。输出为官方助手生成的请求参数；--output 拒绝覆盖已有文件。YouTube JSON3 原始 segs/utf8/tOffsetMs 必须保留，不转成 SRT 后再传。

只有视频链接时，可先使用词塔 Skill 的 youtube_subtitles.py 获取单视频自带字幕（Python 标准库，不需 yt-dlp），再交接所得字幕或 preview_request。Lingather 自带频道列表与现有字幕抓取器继续使用 yt-dlp；有 JSON3 就优先保留，无真实词偏移时明确只有片段时间。

也可运行 `fetch_youtube.py wordtower URL --prepare-only --handoff-output video-request.json`，先用 Lingather 下载字幕，再生成参数；这条命令仍需其下载依赖与 YouTube 网络，但不连接词塔或提交课程。

Agent 后续步骤：

1. 通过词塔 MCP 查询 /_list 和 /account/capabilities，核对实时 scopes、import_usage、agent_limits 与 text_parse，不按历史套餐信息推断。
2. 文本/YouTube：读取请求 JSON，去掉 api_name，用剩余字段调用对应预览工具（/imports/preview 或 /imports/youtube/preview）。不要把本地文件路径当正文传给远程服务。
3. EPUB：本地先选章保留封面，再按输出 file_name、size_bytes、sha256 申请 /imports/upload-url。保存实际返回；使用官方 request.py 的 --upload-response 与 --expected-sha256 上传原字节，凭证不附到 COS；所得 upload_id 继续 /imports/epub/preview。服务端不能读本机路径，具体上传命令按词塔已加载的导入协议执行。
4. YouTube 回包必须指向相同视频，media_type=youtube、subtitle_count>0；source_url 规范化后按视频 ID 核对。逐词能力以 word_timing_count 为准，不承诺全部字幕都有单词时间。
5. 用户要求导入且额度足够时，取得报价后继续提交，不额外要求网页批准；仅分析或预览则不提交。提交前保存随机幂等键和预览 ID，未知结果重用原键；取得 job_id 后只查询状态，succeeded 才报告成功。

MCP 请求参数不含 api_name、skill_version、user_id 或 Key。业务 API 名与工具的映射按词塔 MCP 协议，不自行调用 App 内部接口。Lingather 本地恢复文件只覆盖网关导出器；MCP 的请求/任务恢复由执行工具调用的 Agent 保留，不能把两者混用。

## 普通 Skill 一键导入与恢复

保留现有 epub、text、youtube 命令及 --preview-only。新增实时能力查询，展示每日次数、解析余额及预览报价；实际权限和额度仍由服务端最终校验。--preview-only 不提交、不写恢复状态。

提交前在当前目录 `.wordtower-imports/` 写入恢复记录（可用 --state-dir 指定位置），包含目标/凭证的哈希绑定、原预览 ID、幂等键、任务 ID 和状态，不存 Key 或正文。绑定不同 Key/目标时拒绝恢复，包括轮换 Key；需要使用原连接核对旧任务。

```sh
python3 {baseDir}/scripts/exporters/wordtower.py resume /absolute/path/recovery.json
```

- 提交响应不明确：保留恢复文件，用 resume 重放原预览和原幂等键；不能换键重试。
- 已接受但超时：resume 仅查询已有 job，不再提交；即使当日次数耗尽也能查询。
- 相同材料、标题、视频及连接的默认恢复目录会重用已保存记录，成功任务不重复提交。新预览可能被生成，但不会消费为新任务。
- failed/cancelled 保留记录并停止；用户明确要重新导入时可用原导入命令的 --new-import，它会新建记录和幂等键，并计为新任务。恢复文件不是跨设备或账号级去重，换目录、换 Key 或 MCP 导入仍需 Agent 核对已有任务。
- 导出器锁定同一状态文件防并发提交；进程异常留下 .lock 时先核对记录的 PID 已结束，再人工清理锁，不自动抢占仍在运行的导入。

恢复文件及 .wordtower-imports/ 不提交 Git。网络失败或服务端错误保留状态，不能宣称成功或悄悄降级为纯文本课程。
