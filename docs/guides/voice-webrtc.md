# Talk WebRTC 链路与本地验收

更新日期：2026-10-15。

## 产品链路

用户启动 Talk 后，浏览器取得麦克风权限，本地 API 校验会话、既有 agent 初始化与 consent 状态，并准备一次人格、世界观、历史与记忆上下文。已有真实用户文字时使用该文字检索记忆；没有文字时不制造检索语句。

浏览器创建原生 PeerConnection 与 DataChannel，经本地 API 代理交换 Offer／Answer SDP。准备期间音频 sender 使用 `replaceTrack(null)`；收到云端配置回显且宿主确认后恢复音轨。输入和回复走 RTP，控制事件走 DataChannel 与本地控制 WebSocket，不经过 PCM 分块或 Base64 音频中转。

会话配置开启 `input_audio_transcription={"model":"qwen3-asr-flash-realtime"}`，使用 `semantic_vad`。普通轮次由云端自动提交和回复；用户插话、下一轮及工具续答不重复准备初始上下文。前台保留连接、错误及任务选择状态，没有 Speaking／Listening，也不会因插话主动停播、清缓存或筛除音频。主动结束、切换聊天和连接失败会清理媒体资源。

最终转录通过输入 `item_id` 关联已提交的音频轮次，分别启动用户消息保存和独立情绪评估。两支任务互不等待，语音生成和播放不等待任何一支。转录早于提交时暂存，重复结果不重放；配置、转录、保存或评估失败仅产生支路诊断，不关闭正常语音会话。关闭会话取消未完成任务。

情绪支路复用既有四类人格上下文和 14 个标签，通过 `HEXIS_M4_VOICE_CONFIG` 选择配置，未设置时读取 `config/m4-voice-runtime.json`。配置和凭据在支路调用时校验；不复用旧 WS 回复闸门。Avatar 仅投影当前已提交输入的合法结果，不覆盖更新轮次。

## 配置与接口

`config/qwen-realtime.json` 增加 `webrtc_endpoint`，使用对应业务空间的 HTTPS `/api/v1/webrtc/realtime` 地址。模型、音色、凭据环境变量和超时仍来自显式配置；密钥只在服务器读取，不返回浏览器。

| 接口 | 用途 |
|---|---|
| `POST /api/voice/realtime/sessions` | 一次性上下文准备及媒体会话描述 |
| `POST /api/voice/realtime/sessions/{media_session_id}/offer` | `application/sdp` 信令代理 |
| 会话的 `/control` WebSocket | 云端事件与宿主命令转发，不传音频 |
| `DELETE /api/voice/realtime/sessions/{media_session_id}` | 主动关闭及幂等资源回收 |

`HEXIS_BROWSER_API_URL` 指向浏览器可访问的本地 API 地址，`HEXIS_UI_ORIGIN` 与实际页面来源一致。一个聊天只允许一个活动语音会话；RTC 会话使用进程所有权和租约，在租约过期后回收异常退出遗留的会话及未完成 turn，不关闭其他仍在续租的实例。

## 存储与工具

- `0279_voice_rtc_metadata.sql` 保存云端输入身份、顺序、接收时间和上下文版本；不保存用户录音。`0281_rtc_user_transcript.sql` 基于同一输入身份保存最终转录为用户消息，不重复创建 turn。转录来源只存内部元数据，前端显示普通用户消息，不添加 ASR 标签。
- 用户消息按稳定输入身份去重；前向 SQL 函数在会话锁下处理晚到转录的顺序，历史回读不会生成重复消息。它不修改已完成 turn 的状态或已受理 M5 任务参数。新运行时显式标记转录开启，历史输入元数据保持原值。
- 助手生成文字按媒体会话和云端响应身份去重，保存为助手消息，标记 `content_source=model_generation`、`playback_scope=unknown`；生成完成不表示用户已经听完。
- `0280_m5_rtc_source_input.sql` 允许语音任务通过已持久化音频元数据绑定原始输入，原始用户文字为空；文字任务仍保留非空原始文字校验。
- 工具仍由后台校验参数、绑定会话与输入、执行并记录真实结果；结果由浏览器回传 DataChannel。Function Calling 续答保留 `response.create`。
- `sent` 回执只表示浏览器已发送。工具续答还需匹配云端 `response.created`；超时或结果未知时报错，不自动重放命令，也不撤销已受理的后台任务事实。

迁移通过正常前向迁移流程部署。0281 的数据库回归在独立临时数据库中执行；主库应用须在代码提交和备份后走正式迁移 runner，不手工改写迁移 ledger。

## 本地浏览器验收

在 `hexis-ui` 目录执行以下命令。

命令执行意图：让 Playwright 启动外部 Google Chrome 和本地受控 RTC 对端，检查原生媒体与控制链路。

```sh
npm run test:rtc
```

预期输出：`rtc-external-chrome` 的 3 个浏览器用例通过。若默认端口被占用，通过 `HEXIS_UI_PORT` 与 `HEXIS_RTC_FIXTURE_PORT` 显式选择空闲端口；不会复用正在运行的页面服务。

验证使用 Chrome 的受控虚拟麦克风和对端音频轨道，真实创建 PeerConnection、交换 SDP、建立 ICE 与 DataChannel；以 RTP 收发统计验证双向音轨。用例覆盖配置确认前不发送音轨、元数据与生成文字、工具控制回传、权限拒绝、主动关闭及断线清理。测试浏览器静音，不验证实际扬声器效果。

浏览器上下文隔离，测试工具不执行业务副作用，供应商信令被显式替换为本地 gateway；不读取真实模型凭据，不请求真实 Qwen 接口。上下文准备和数据库持久化在浏览器夹具中为受控替身，实际 SQL 另由隔离数据库测试验证。

证据位于 `$XDG_CACHE_HOME/hexis/voice-rtc/browser`，未设置时为 `~/.cache/hexis/voice-rtc/browser`，包含报告、失败 trace／截图、连接统计及宿主日志。本地链路通过不代表真实 Qwen 自动回复、打断、工具调用或实际音频效果已经验收。

## 退役与兼容

生产 Talk 不再依赖 Silero、ORT、采集 worklet、前滚、原生 PCM 播放器或播放回执；相关前台模块、资源、构建准备和旧验收页面已退役。其他语音附件、TTS 和独立播放功能保留。

历史 WS 客户端、配置快照及 Python 探针只用于既有回归或历史证据；`apps/voice_realtime_harness.py` 不再是生产 Talk 的验收入口。旧 WS 验收策略不能被新 RTC 路径静默复用。

## 规范依据摘录

- 阿里云 WebRTC 指南要求生产环境由业务 AppServer 代理 SDP；媒体经 RTP，控制经 DataChannel。[官方接入指南](https://help.aliyun.com/zh/model-studio/best-practice-webrtc-omni-realtime)
- 输入转录“设为 `null` 可关闭”；Function Calling 回传结果后仍显式触发续答。[客户端事件](https://help.aliyun.com/zh/model-studio/client-events)
- `response.done` 表示响应生成完成，不是实际播放完成。[服务端事件](https://help.aliyun.com/zh/model-studio/server-events)

## 2026-10-13 收尾验证

本次收尾修复三项问题：异步建连等待结束后再次校验连接代次，避免用户结束后旧任务创建媒体连接或发送 SDP；迟到生成文字仍可保存，但不覆写已关闭 turn 的状态、原因和完成时间；配置回显同时检查音色及存在的云端会话 ID／模型。音色采用官方新字段 `audio.output.voice` 优先，兼容旧 `session.voice`。

已补充 RTC 客户端关闭竞态、数据库迟到输出、音色与云端身份不匹配、初始化失败、SDP 拒绝与信令超时的回归。数据库用例按既有约定回滚；独立代码复审确认三项产品问题均已关闭。

官方协议静态复核覆盖服务端 SDP 代理、完整 ICE、客户端及服务端 DataChannel、输入转录关闭、云端 VAD、Function Calling 续答和响应完成语义。官方示例在 `session.created` 后恢复媒体；本项目按已确认产品约束继续等待匹配的 `session.updated` 和宿主确认。读取官方文档与本地受控对端通过，均不能证明真实供应商兼容性或效果已经验收。

| 检查 | 最终结果及边界 |
|---|---|
| RTC 后台会话、初始化与 API | 29 项通过；供应商信令为受控替身 |
| RTC 历史与既有 turn 完成 | 11 项通过；仅任务专属 PostgreSQL 测试实例 |
| 相关前台回归 | 99 项通过；其中 19 项是保留的旧 WS 客户端回归，新增 2 项直接测试 RTC 客户端关闭竞态 |
| 外部 Chrome 原生 WebRTC | 3 项通过；使用 `channel: "chrome"`、非无头模式及隔离上下文 |
| 生产构建及独立 TypeScript 检查 | 均通过；构建与类型检查顺序执行 |
| Python 格式及差异检查 | 通过 |

浏览器证据显示：确认配置前音频 sender 已分离，对端接收字节为 0；确认后客户端发送 311 字节、接收 614 字节，对端接收 311 字节。宿主准备上下文 1 次，受控工具执行 1 次，供应商请求数为 0。数字仅对应本次受控样本，不是吞吐或音频质量指标。

本轮日志与提取后的连接统计、宿主诊断保存在 `~/.cache/hexis/voice-rtc/final-20261013-015943/`；浏览器报告、截图及完整内嵌附件仍在上述 `browser` 目录。没有请求真实 Qwen，没有执行 commit、push、部署或用户数据库迁移。
