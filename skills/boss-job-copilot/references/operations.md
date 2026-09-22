# 运行方式

需要 Python 3.11+、桌面 Chrome 与本地文件/进程工具。已实测 Windows、Python 3.13、Zendriver 0.16.0；其他系统未实机验收。Skill 本身不提供云端浏览器。

## 工作目录和初始化

默认数据目录是调用时 cwd 下的 `.boss-workspace/`，不是脚本目录或全局 home。先确认并告知实际目录，再读其中 user/USER.md 及引用。脚本用绝对路径执行，cwd 保持用户打开的目录；也可显式 `--workspace` 选择另一个方向，每条命令保持一致。不自动导入别处材料或登录资料。

下面 PY 是隔离环境 Python 的绝对路径，BOSS 是本 Skill 的 scripts/boss.py 绝对路径，W 是本次数据目录。实际执行时替换代称：

```text
<已有Python> -m venv .boss-venv
<环境Python> -m pip install -r <Skill目录>/scripts/requirements.txt
PY BOSS --workspace W init --chrome <Chrome可执行文件>
PY BOSS --workspace W doctor
```

运行环境放工作目录的 .boss-venv/，不要装在 Skill 内。可复用已有隔离环境。完整求职请求开始时尽早准备环境与浏览器，让用户登录和材料分析同时进行；安装或浏览器暂时受阻时，继续可做的分析。只做职业分析时无需准备 Chrome；材料分析、字典查询、init、plan、collect --dry-run 本身都不依赖浏览器。

init 创建空白资料模板和 SQLite，无方案确认，不含个人预设；重复执行只补缺失文件。首次 init 可用 `--profile <已有专用目录>` 明确引用旧 profile，不复制 Cookie。未指定则用 W/browser-profile/。同一 profile 只能由一个进程占用，不能因多个求职方向同时启动多份。

```text
W/
  user/USER.md                   必要交接与资料索引
  user/PROFILE.md                背景、项目证据、优势与能力边界
  user/SEARCH_PLAN.md            已确认方向、范围与条件宽严
  user/OUTREACH.md               语气、参考稿与反馈
  user/materials/               简历、项目材料原件
  user/examples/                用户话术例子
  user/search-config.json       搜索参数（按实际方案生成）
  user/outreach-config.json     活跃范围及首次校准数量
  user/search-plan-confirmation.json
  jobs.sqlite                  岗位、审查与沟通记录
  browser-profile/              专用登录态
  runtime/                      浏览器进程、采集请求、发送授权
  outputs/                      按需生成的报告
  tmp/                          可复用的本组审查 JSON
```

未初始化的数据目录返回 workspace_not_initialized，不向其他目录找数据。一个人不同方向分别使用不同工作目录即可。用户明确要求复用材料时才导入；共享登录 profile 不等于共享资料或岗位库。

## 浏览器和登录

完整求职流程在开始分析材料时准备登录，不等搜索方案确认。先看已有后台，复用可用的 BOSS 页并检查；尚未打开页面时才打开首页，不为检查而刷新用户正在登录的页面：

```text
PY BOSS --workspace W browser status
# 仅尚未打开 BOSS 页时执行：
PY BOSS --workspace W browser open --boss-home
# open 返回 request_id 时先用 result 读取该次跳转结果
PY BOSS --workspace W browser check
PY BOSS --workspace W result --id <检查请求ID>
```

browser open 默认只开空白窗口，不检查或采集；已开时复用。用户说“只开窗口”就用这个命令并停在这里，让其手动使用。只做职业分析或明确暂不开浏览器时不启动。BOSS 单登录可能挤掉其他会话，不额外打开其他 profile 抢登录，尊重正在使用或主动暂停的窗口。

open --boss-home 在已有窗口也会提交一次首页跳转。check 的 about:blank、其他网站或尚未加载的页面返回 login: not_checked，不能说用户未登录。明确未登录时简短提示用户可先在此窗口手动登录，然后继续材料分析与澄清，不以“等你回复已登录”为由结束本轮，不反复轮询。不要求密码或验证码；验证码、登录失效及浏览器故障只阻塞网页操作，职业分析仍可继续。

## 确认方案后开始采集

按 [背景分析](intake.md) 展示职业画像、求职策略与招呼参考稿，同时简述浏览器准备情况。用户实际同意后记录确认，并在采集前再检查一次页面：

```text
PY BOSS --workspace W plan confirm --user-message <用户实际确认原话>
PY BOSS --workspace W plan status
PY BOSS --workspace W collect --config <搜索配置绝对路径> --dry-run
PY BOSS --workspace W browser check
PY BOSS --workspace W result --id <请求ID>
# 方案已确认且上述检查确认登录、页面可用后：
PY BOSS --workspace W collect --config <搜索配置绝对路径>
PY BOSS --workspace W result --id <请求ID>
```

检查通过就直接采集，不另要求用户回复“已登录”；还未登录或需要验证时才等待用户手动处理。只回复登录完成不代表认可方案。浏览器未能准备好时先恢复原窗口再检查，不重复要求方案确认。

不得自行编造用户同意；已有同范围确认直接沿用。计划正文只放方案，不写登录提醒或每批计数以反复改变确认。直接修订的用户指令已构成该修改的授权时，记录真实原话，无需再问一次。准备首页和登录不需要 plan confirm，搜索、详情与消息仍遵守各自确认或授权要求。

网页命令提交给常驻浏览器，返回 request_id，用 result 读取结果。它只是复用登录窗口的执行进程，不决定应审查哪批岗位。采集进度用于继续翻页，与岗位审查进度分开。

collect/details/send 等页面动作默认串行间隔至少 5 秒；数据已就绪即可继续，没有小时/每日配额或总时长上限。本地查询、分析、保存无需等待。网页明确限发、访问拒绝、验证或未登录时停止网页；用户处理后显式 check 恢复。局部解析或详情失败继续其他候选；采集允许缺页，别为了补齐而耽误本地审查。

## 统一取数：首批 10 个，后续 20 个

```text
# 首批校准改为 --limit 10；后续默认 20：
PY BOSS --workspace W jobs --unreviewed --view review --limit 20
PY BOSS --workspace W details --ids <本组相关或不确定的ID...>
PY BOSS --workspace W result --id <请求ID>
PY BOSS --workspace W jobs --ids <本组ID...> --view review
# Agent 完成判断后，将本组 JSON 写入一个可复用的私有文件：
PY BOSS --workspace W review --file <本组审查JSON绝对路径>
```

jobs --unreviewed 是统一的待审取数入口，默认 --limit 20，不认领任务或改变状态；首批用 10，数量可按需调整，不强制批次上限。--view review 保留全部列表事实和已有完整 JD、工作地址、公司介绍、活跃与联系记录，省去采集来源等冗余元数据，不截断正文或按语义筛选。不再编写临时查询、裁剪字段或筛关键词的脚本。

详情尚未读取时 detail 为 null，读取后用同一视图查看。需要原始记录时使用 jobs --ids ... --view raw；原查询格式仍兼容。完整详情和判断已保存就不必重做；中断丢失的少数未保存判断允许重审。详情失败另计并继续其他岗位，不假装不匹配。语义判断规范见 [审查说明](review.md)。

review --file 返回 saved、failed、failed_ids 及逐项结果。部分保存失败时退出码 1，全部保存成功为 0，命令级错误为 2。成功条目不回滚；核对失败原因后只修正并重新提交失败项，不把它们算作已完成。

首次目标 10 个完整详情审查，列表已排除项不再读详情，以其他待审候选补足。发现未对齐且影响结论的策略问题时，可提前带样本向用户澄清，涉及项用 hold 保留；没有策略疑问且无可联系者才再加 10 个。完成当前组后在聊天中展示累计结果与招呼，preview 默认不生成 Markdown，需要文件才传 --out。有发送授权后每组审完，串行发送本组合适者，再取新的 20 个。命令不返回工作流指令，也没有 next、packet 或审查批次登记入口。

按需查询：

```text
PY BOSS --workspace W jobs --unsent --view review --limit 20
PY BOSS --workspace W reviews --ids <岗位ID...>
PY BOSS --workspace W jobs --id <岗位ID> --view review
PY BOSS --workspace W queue
PY BOSS --workspace W stats
PY BOSS --workspace W report --out <需要的报告绝对路径>
```

queue 只是数据库分类查询，不派发任务；stats/report 汇总实际进度。jobs --out 可覆盖一个私有 JSONL，保留所选视图，避免工具输出被截断；它不改变取数范围，也不用于绕开逐批阅读。岗位结论、理由、证据、招呼都在库里，无需逐项生成文档或把队列抄到 USER.md。

模拟、首批报告及真实发送见 [招呼流程](outreach.md)。

## 续用与升级

新会话读当前资料、plan status 和必要的数据库查询即可继续，不需要寻找旧批次游标。已审未发可用 jobs --unsent 和 reviews 接续；若发送结果不确定，保留记录并只读核实，不因丢失请求而重发。

用户资料或规则更新只影响此后的判断，历史结论不自动作废。重复采集可以更新岗位事实，但不会把已审岗位自动变回待审；暂缓岗位也不会因在线或策略改变自动恢复。仅用户要求重审时使用 review-reset --ids ... --reason ...。已沟通和发送不确定的记录不会被重开。

正常升级替换公共 Skill 文件，保留数据、登录态与环境。旧 runtime/review-packets、current-batch 和审查 batches 文件不再被读取；无需清库或自动删除用户文件。搜索 plan confirm 仍核对实际方案，防止拿未确认的新搜索范围执行；它不使历史审查过期。发送授权不因升级或文档变化自动扩大。

页面命令要求 runtime_version 16。若复用的后台版本不同，需要按下文维护并重开原 profile；只换文件不会热更新运行进程。旧目录明确配置的 initial_review_count 会保留，未指定时默认 10；用户要求改数量可用 outreach policy --initial-review-count。

## 暂停、关闭与故障

```text
PY BOSS --workspace W browser pause
PY BOSS --workspace W browser close
# 故障维护：等待旧后台退出后，复用原 profile 重开并进入首页
PY BOSS --workspace W browser restart
```

正常结束留窗；pause 停止当前动作并留窗，close 等待本工具专用窗口及后台退出。restart 默认复用原 profile 重开并进入 BOSS 首页；兼容 --boss-home，只有显式 --blank 才留空白窗口。browser open 仍默认只开窗口。已有故障维护授权时可直接恢复，先尊重用户正在操作、主动暂停或要求保留窗口的意图；不关其他 Chrome，不删 profile，不因此要求用户反复登录。

关闭先请求 Chrome 正常退出并等待，再释放 Zendriver；超时才终止本工具持有的浏览器进程。Windows 同时核对本次专用 profile 的 Chrome 子进程，以 PID 和创建时间确认归属，清理残留后再检查。返回 browser_exit 说明 graceful/forced 及退出核验；未确认关闭或后台尚未退出时返回明确错误，不创建第二个后台。不要使用按进程名杀全部 Chrome/Python 的命令。“Chrome 未正确关闭”是上次退出异常的提示，不能据此断言白屏由进程残留造成。

空白或失联按以下顺序处理：

1. 只开窗口的 about:blank 是正常行为。需要求职操作时用 open --boss-home，再 check；不能将 worker 心跳或 reused: true 当成页面可用、已登录的证据。
2. 用户关掉标签页后，新请求会从专用浏览器的实时标签页重新选择 BOSS 页；不会自动重放刚才的采集或发送。进入完整聊天页也是正常行为，身份识别超时先看当前会话和请求阶段，不因 URL 缺 ID 而重启。找不到可用页、连接超时或页面持续空白时，可维护重开一次，再 check。
3. 仍然不可用就报告具体错误，保留数据并继续可做的本地审查，不反复刷新或重启。验证码、登录失效、访问拒绝、平台限发仍须人工处理；重开不解除这些阻断。

浏览器生命周期与岗位数据独立。close/pause/restart 不改待审、已审、草稿、暂缓或联系记录，也不重开历史岗位；只留下网页请求中断回执和进程状态。连接故障不批量标记岗位读取失败。已经执行的采集、审查或发送动作，其真实记录照常保留；发送中断保留最后一次实际尝试，sending/uncertain 只读 verify，不能因关窗改判成功、失败或重新发送。

result 返回 pending_or_interrupted 时检查 browser status 和数据库，别重复提交原发送动作。采集无法恢复就记录缺口进入审查；发送不确定则只读 verify。USER.md 仅在需要交接时记录用户指令、真正阻塞、资料索引与必要报告链接，不维护每个岗位的处理过程。
