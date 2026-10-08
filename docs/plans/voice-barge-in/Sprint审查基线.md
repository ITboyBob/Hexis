# 人声检测与确认打断执行计划

> 本计划基于[架构设计稿](./架构设计.md)与[已获取资源](/Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/vad-assets-manifest.json)编写。
> 日期：2026-10-08
> 版本：v1.3
> 状态：Architect对照当前代码审查通过；未实施、未运行产品验证
> 粒度：Sprint级执行与验收计划，当前不生成产品实现代码。

**目标：**在现有Talk中增加本地人声概率检测与独立的目标打断确认，过滤非人声环境噪音，同时保留正常插话、短指令及M5媒体生命周期。

**架构：**沿用16kHz采集与运输；独立Worker运行Silero v6，候选PCM先本地缓存；输入确认与打断分开，确认后只停止匹配的输出。前后端补充目标协议及短时输出命令互斥，保留现有双终态清理。

**技术栈：**Next 16.1.3、TypeScript、Module Worker、onnxruntime-web 1.22.0、vad-web 0.0.31模型资产、FastAPI/Pydantic、Vitest、pytest、Playwright。

## 1. 状态、范围与共同前提

- 已完成：两个固定版本原始包及文件完整性校验；v6与v5在Node v24.18.0单线程WASM的全零加载与推理检查。执行前重新校验资源清单，复用资源，不把下载列为尚未完成的Sprint工作。
- 待完成：资源正式进入应用构建、浏览器模型Worker、两层确认、目标协议、生产Talk接线、真实音频及浏览器/云端测试、真人验收。
- 测试范围：纯环境噪音、普通中文、单字短指令、轻声与带噪人声；扬声器回声不作为本次主因。
- 句末520ms与pre-roll 240ms是兼容参数，不开展原始问题二的语义结束判定调优，但模型改变后的句末行为必须回归。
- 本文新增仓库文件均为实施提案，当前不存在；本轮只写缓存中的计划，不执行安装、产品修改、commit或push。正式实施的授权与提交策略另由用户明确。
- 凭据留在环境；真实云端运行清单明确配置、源码指纹、隔离数据库、UI/API地址，不能复用未知来源的常驻服务。
- 人工验收不设成功率、P95、固定次数；代理采用逐场景断言。诊断延迟仍记录，挂起超时只是测试运行边界。

### 1.1 基线与停止条件

仓库快照：fadb6a7ab1575c13b14b132eec0134e979683f0c。协议SoT：/Users/itboybob/Hexis/services/voice_realtime_transport.py，SHA256为503ce53081ad1637620c4b62371e006796885b659c39c3744897d919c58a1744。

原审查快照为07b2ffa；发现漂移后已停止旧结论、只读核对全部提交差异与八个核心文件内容。变化只为voice_m5_per_turn.md增加Active Persona及工作区voice_m5_stable.md的情绪表达文字；核心协议/采音/输出/API/构建文件未变化。保留并隔离这两项提示词变化，计划没有授权修改它们，其实际哈希纳入S5运行清单，当前基线结论由Architect再确认。检查记录见“基线差异核对.json”，不将提示词体验或模型遵循效果视为静态审查通过。

设计的基线提交指仓库快照，不要求每个目标文件的最后一次提交都等于该HEAD；比对该快照下的文件内容和记录的指纹。进入首个Sprint发现非计划内代码、配置、资源哈希或真实依赖差异时停止并报告，修订后再继续。后续Sprint比对前一阶段交付的清单，允许计划内改动，不拿原始哈希阻止本计划自己的演进。保持既有upstream.json与未跟踪config目录的工作区修改。

## 2. Sprint与依赖

| Sprint | 内容 | 依赖 | 验收主体 |
|---|---|---|---|
| S1 | 正式资源交付、配置与测试数据基线 | 已获取资源、当前源码 | 代理 |
| S2 | 模型Worker、PCM适配与生命周期 | S1 | 代理 |
| S3 | 目标协议、幂等取消与并发隔离 | S1；真实端到端留到S5 | 代理 |
| S4 | 两层确认、候选缓存与Talk产品接线 | S2、S3 | 代理 |
| S5 | 调参冻结、真实浏览器注入及云端自动验收 | S4 | 代理；能力缺口转人工 |
| S6 | 真人现场验收与必要修正 | S5通过或明确自动化缺口 | 人类判断，代理记录/回归 |

当前由一个执行链按S1→S2→S3→S4→S5→S6串行推进；此处依赖关系不授权额外并行代理或改变代码基线。

### 2.1 数据和配置索引

| 编号 | 数据/配置 | 当前状态与来源 |
|---|---|---|
| R1 | 仓库C2资源清单及缓存原始包/许可证取证 | 元数据已落盘，实施前重新校验；public资源待生成，未构成完整离线npm依赖闭包 |
| C1 | /Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/speech-detection-config.json | 已落盘，validation_status=unvalidated；继承设计初值，loader/校准待实施 |
| C2 | /Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/vad-assets-manifest.json | 已落盘，delivery_status=not_materialized；固定模型/ORT版本、同源路径、SHA256及许可证，public资源待生成 |
| D1 | Hexis缓存目录内voice-barge-in/audio/manifest.json及音频 | 拟准备；按C8长期规范绑定实际音频，纯噪音/普通中文/短词/轻声/带噪人声及分集记录尚不完整 |
| D2 | 现有/Users/itboybob/.cache/hexis/m4-test-audio-2026-10-05/manifest.json | 可复用部分；noise.wav是带噪人声正例，interrupt.wav是长句，缺短词与现场录音 |
| D3 | 概率序列、PCM分块与目标切换测试夹具 | 拟新增确定性测试输入；与真实模型效果分开 |
| C3 | 缓存中的单次运行记录 | 拟新增；按C6生成新身份，记录源码/资源/配置/实际音频指纹、API启动记录及隔离DB身份，不作为长期模板 |
| C4 | /Users/itboybob/Hexis/config/m4-voice-runtime.json | 现存水合/情绪配置，HEXIS_M4_VOICE_CONFIG读取；不能冒充Realtime配置 |
| C5 | /Users/itboybob/Hexis/config/qwen-realtime.json | 已迁入连接参数，HEXIS_REALTIME_CONFIG读取；不含历史WAV/session/input/playback身份，未激活 |
| C6 | /Users/itboybob/Hexis/config/voice-barge-in/run-profile.json | 已落盘长期运行模板，path_base=repository_root；--profile加载接口待S5实现 |
| C7 | /Users/itboybob/Hexis/config/voice-barge-in/playback-coordination.json | 已落盘服务端协调初值，unvalidated；S3加载并能力同步，不复制前端默认值 |
| C8 | /Users/itboybob/Hexis/config/voice-barge-in/audio-cases.json | 已落盘长期场景规范，dataset_status=incomplete，audio_asset=null；实际数据绑定待准备 |
| D4 | 用户真实现场发声与噪音、人工结论 | 尚未获取；由真人测试或经授权采集，不能由TTS替代 |

## 3. Sprint 1：正式资源交付、配置与测试数据基线

**任务与文件：**

- S1.1 修改/Users/itboybob/Hexis/hexis-ui/package.json与package-lock.json，固定选定版本并新增资源准备入口；新增/Users/itboybob/Hexis/hexis-ui/scripts/prepare-voice-vad-assets.mjs；校验并必要更新已落盘C2，按清单生成public/voice-realtime/vad-assets下的模型和同版本ORT资源。生产构建以Docker当前npm ci路径为准；记录bun.lock的当前用途，若仓库要求双锁同步则纳入同一任务，不能假设其已同步。
- S1.2 验证开发与生产准备都会执行资源检查；Docker使用npm ci --ignore-scripts，必须在后续npm run build路径显式准备资源，不能只依赖postinstall。修改/Users/itboybob/Hexis/ops/Dockerfile.ui只限必要构建接线，并核对Node20容器与本机Node24差异。生成资源随public打包，实例没有原准备缓存仍可加载。
- S1.3 校验并必要更新已落盘C1/C6/C7/C8，创建D1实际音频绑定、D3及数据检查入口；将来源、采样率、长度、哈希、语音onset/offset和期望行为写入清单。公共录音需可追溯；TTS/合成明确标记；调参与验收按原始录音拆分。真人素材缺失可保留到S6，不能记为已覆盖。

**数据与配置：**R1、C1/C2/C6/C7/C8、D1/D2/D3；此阶段不需要真实云端凭据。

**测试内容与方法：**先为版本/哈希/缺失文件/重复准备写失败断言，再接入准备脚本。代理检查资源URL、类型、许可证和JS/WASM版本一致；开发服务器及生产standalone分别实际提供这些资源；Node20构建路径验证准备发生且资源进入发布产物。失去个人缓存时，发布产物仍包含所需文件；npm安装可能联网，不能声称完整离线构建已具备。

**完成检查：**资源与配置清单可回读；准备可重复；错误会明确失败；D1每个案例有独立期望与来源；现有完整性检查只是前提，尚不证明真实浏览器模型推理。

**执行/验收：**代理。无用户录音时，阶段通过范围限于明确已有素材。

## 4. Sprint 2：模型Worker、PCM适配与生命周期

**任务与文件：**

- S2.1 新增/Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/speech-detector.worker.ts、speech-detector.ts与模型适配组件；从重采样后的16k PCM获取512采样帧，保留64采样上下文及循环state。原PCM运输身份独立记账，上下文与补零不进入正式音频。保留未满帧尾部，不因短词结束丢样本。
- S2.2 串行推理、有界队列、批次序号/采集代次与原始frame/timing映射；实现ready/reset/stop握手和故障上报。暂停使旧代次立即失效，恢复等待reset确认，关闭释放Worker、模型及缓存。过期/损坏模型历史不可继续用于打断。
- S2.3 新增/Users/itboybob/Hexis/hexis-ui/e2e/voice-vad-worker.spec.ts，真实浏览器验证加载、分帧推理、重置、重复启动与资源异常；Vitest使用/Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/speech-detector.test.ts验证协议及状态分支，不拿jsdom作为真实Worker证据。

**审查补充：**Int16除以32768得到归一化Float32；模型sr为16kHz int64，原始运输PCM不变。Worker返回采样水位，主线程只顺序消费已判定结果；raw尾部和尚未判定样本有界保留，不提前上传下一句。新增幅值-32768/32767、跨512帧、延迟末帧、两句紧接、过期结果断言；真实语音不靠固定幅度数组代替。

**数据与配置：**R1/C1/C2、D1的调参部分、D3的不同块长/不满帧/延迟/乱序数据。

**测试内容与方法：**代理先写状态与样本连续性断言，再实现；实际浏览器加载v6推理。测试正常中文、短词、轻声、带噪人声及纯噪音的概率轨迹；此时记录轨迹，不外推为Talk打断已正确。开发与生产standalone都要测真实Worker。故意缺文件、延迟推理、积压、reset与close交错，检查错误及旧结果隔离。

**完成检查：**真实浏览器模型加载成立；原PCM没有模型上下文污染；未满帧与采集代次处理明确；无挂起/无限积压/资源泄漏；短词低概率案例保留为S5调参输入，不能改标签使其通过。

**执行/验收：**代理；浏览器需要的能力按S5同一可见浏览器规则选择。

## 5. Sprint 3：目标协议、幂等取消与并发隔离

**任务与文件：**

- S3.1 同步修改/Users/itboybob/Hexis/services/voice_realtime_transport.py、hexis-ui/lib/voice-realtime/contracts.ts及client.ts，增加ready能力声明、barge_in_confirmed与barge_in_result；服务端读取并校验C7，将配置身份和前端所需限制随能力协商同步，缺项/错配拒绝，不复制前端默认配置。新增字段与语义来源为设计§5.5/附录A；现有封装和区间规则来源为协议SoT。Pydantic extra=forbid需要实际更新类型联合及accept路由，不能只改前端。
- S3.2 修改/Users/itboybob/Hexis/services/voice_realtime_session.py、voice_output_arbiter.py与apps/hexis_api.py的必要接线，校验会话、开放且未暂停capture、response_id/output_epoch。command_id有界幂等缓存；同一ID不同内容拒绝，过期目标返回stale_target，不操作新输出。
- S3.3 目标检查→失效标记→向云端写取消与新响应创建使用短时前景命令互斥。覆盖start_authorized_response、start_voice_tool_followup、旧取消路径及终态释放竞态；不复用会持锁等待_responses_idle的临界区作为取消锁。生成已结束但播放未结束时只结束匹配本地播放，不发无活动生成的cloud cancel；accepted表示该命令已处理，不替代provider终态。
- S3.4 发送response.create前建立可校验的输出身份与状态，失败时完整回收；检查response.created/首音频接收抢先的时序。保留生成+播放双终态规则及原M5工具授权，避免voice tool followup绕过输出互斥。

**审查补充任务S3.5：**先记录当前ServerMessage与生产发送形状的差异：ServerAudioChunk/Generation的playback_id/turn_id，以及capture_suspended/resumed、m5_selection_opened联合。以实际构造器和前端消费为依据，同步本次相关生产消息与ready/confirm/result形状，写parse_server_message及前端消费往返测试；新旧字段分开标记，不把当前extra=forbid定义与生产消息一致当成已成立事实。

**审查补充任务S3.6：**落实设计§5.9的WS真实回执前缀协调与有界收尾：目标确认携带playback_id、local_playback_state及播放器实际emit的有界前缀；先完整校验，再在短锁内预留目标并设取消中隔离，锁外协调HTTP/WS回执和历史持久化，之后短锁内处理必要cloud cancel和结果，最后允许双终态释放。生成终态、HTTP终态、释放与旧voice工具授权均遵守目标保留；回执落库期间旧音频不能继续影响前景、旧未执行工具不能启动、新响应不能替换该目标，已提交任务不回滚/重放。生成已结束仍播放时，前缀interrupt不得抢先释放目标，返回相应playback_only处理结果。

扩展/Users/itboybob/Hexis/tests/web/test_voice_playback_api.py及/Users/itboybob/Hexis/tests/voice_realtime/test_playback_watchdog.py，覆盖延迟open/audible/progress、WS抢先、provider终态抢先、HTTP相同请求重试、同序号冲突、已释放响应的合法重复、丢回执、收尾超时；追加落库期间生成完成、HTTP终态/旧工具followup/新响应创建交错。合法前缀不得409，不合身份/范围仍拒绝；不得重新播放或因opened暂缺伪造unplayed；丢失事实标unknown并明确恢复，下一轮不无限卡住。输出命令锁不等待HTTP、DB或回执协调锁，回执处理不持输出锁等待落库，状态检查只短暂取得输出锁。

**审查补充任务S3.7：**每项回执区分已校验、待持久化、已持久化；HTTP与WS相同请求共用处理记录，内存dedup不代表数据库历史已完成。复用/Users/itboybob/Hexis/services/playback_history.py稳定event_id与services/behavior_events.py现有数据库幂等路径；写失败或提交结果不明以同一请求恢复，不丢pending、不提前确认历史完成或voice turn完成。新增DB首次写失败、提交后响应丢失、HTTP/WS并发/重试断言，检查历史事实与voice_turn_completion各只持久化一次。失败超过有界收尾期明确unknown并关闭当前媒体以恢复，不能解除隔离启动下一响应或永远等待。包含相关/Users/itboybob/Hexis/tests/services/test_playback_history.py与voice_turn_completion测试。

**数据与配置：**C7服务端协调配置及能力往返夹具；D3的目标A/B切换、重复ID、paused capture、生成先结束、播放先结束、失败/close及HTTP/WS回执交错夹具；协议能力版本来自设计§5.5/5.9而非C1；pytest基础会话夹具。真实供应商证据到S5再取得。

**测试内容与方法：**扩展/Users/itboybob/Hexis/tests/voice_realtime/test_realtime_transport.py、test_realtime_session.py、test_output_arbiter.py，新增/Users/itboybob/Hexis/tests/web/test_voice_barge_in_api.py；前端扩展client.test.ts。用asyncio.Event控制“目标检查后A释放/B创建/取消写入”的交错，证明B不被取消。分别检查初答和工具followup不会死锁，重复请求不会二次取消，暂停身份拒绝、能力缺失可见、正确序号/结果关联。

**完成检查：**目标协议在前后端一致；确定性并发测试通过；双终态及409身份门禁保留。mock取消只证明宿主控制，不声称云端已取消。实现本阶段如需改变设计中未明确的返回语义，应先补充设计来源再写代码。

**执行/验收：**代理。

## 6. Sprint 4：两层确认、候选缓存与Talk产品接线

**任务与文件：**

- S4.1 新增/Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/speech-gate.ts、barge-in-gate.ts及单测，使用C1独立判定输入与打断，普通与高置信快速通道不要求整句ASR。清晰区分初始设计值和经真实音频冻结后的配置。
- S4.2 修改capture.ts/vad.ts的最小边界，接入真实Worker结果、本地候选缓存和原PCM记账。拒绝候选不上传，确认输入后顺序发送明确pre-roll至当前的原PCM；结尾hash与transport实际接收相同。每个audio block只进入一次正式输入，异步结果不得倒序结束句子或吞掉下一句。
- S4.3 修改TalkMode，仅对匹配的response/epoch本地停播并发送目标确认。现有voice_model_call_started提供生成中目标，audio_chunk及播放器提供播放目标，generation.done不能提前清除仍在播的目标。输出切换重建确认窗口，持续人声须用新目标之后的新证据确认；旧推理/旧命令不复用。
- S4.4 M5选择暂停/恢复/关闭/重开、媒体切换及模型异常都清理候选和代次；对新协议能力未ready、生成失败、播放异常、cancel失败或stale结果提供明确状态。保留原手动Stop入口、Speaking实际audible规则及Avatar一致性。

**审查补充任务S4.5：**修改client.open实际完成语义：WebSocket onopen后仍等匹配服务端ready及新能力，再等Worker ready才能capture_open。用延迟、缺失、错身份、close与ready先后顺序验证双门禁；恢复同时需要capture_resumed和模型reset确认。

**审查补充任务S4.6：**active维持概率初值0.45，达到门槛清静音，否则按实际帧长度累计到520ms；未确认候选最近3帧全低于0.60或缓存超限则拒绝，清候选证据而保留独立pre-roll与持续模型状态。不沿用旧RMS作为active保持。按已判定PCM水位先关上一句/hash，再进下一句。补测轻声连续、概率抖动、拒绝短候选后正常一句、帧边界与迟到结果；记录32ms推理粒度造成的结束时刻差异。已确认输入但未达到打断门槛时仍按现有输入授权处理，原输出自然完成后回复；明确验收没有interrupt、没有漏输入或死锁，输入活动不覆盖仍audible的Speaking事实。

**审查补充任务S4.7：**本地停播时抓取同一播放器的真实emit前缀，连同目标发送WS确认；HTTP重试保留相同请求内容。不根据音频chunk或推断构造audible。回执前缀缺失/超限、协调失败、unknown收尾与目标stale均有明确的用户恢复路径。

**数据与配置：**C1/C2（补充active与候选拒绝参数）、D3确定性概率及PCM范围、D1真实模型的调参案例；S3协议夹具和服务端目标校验。

**测试内容与方法：**先改写旧capture“speech_start立刻barge”断言；扩展capture.test.ts、vad.test.ts、TalkMode测试及Avatar投影测试。固定概率序列只测试门控；实际语音测试门控效果；纯音不再作为人声正例。重点串联拒绝候选→有效短句、连续两句、非整512尾部、推理延迟、520ms静音与pre-roll、响应切换、M5暂停与close。校验PCM连续/长度/hash、目标归属、实际播放器stop、下一轮及无迟到复活。

**完成检查：**候选保持播放；确认一次只停止相应输出；噪音候选没有外部输入副作用；收音确认与打断确认确实解耦；UI与Avatar不因候选错误改变可听状态；正式区间/句末及M5回归通过。

**执行/验收：**代理。此时产品可进入S5真实端到端，尚未真人验收。

## 7. Sprint 5：调参冻结、真实浏览器注入及云端自动验收

**任务与文件：**

- S5.1 用D1调参集实际推理/回放C1，覆盖普通、短词、轻声及噪音。记录失败与参数变更依据，冻结C1、模型/资源版本、调参结果以及独立验收集哈希；禁止用验收集反复选门槛后仍称独立验证。新增/Users/itboybob/Hexis/apps/voice_barge_in_probe.py提供仅测试使用的运行清单、隔离环境预检及证据回读，不增加产品缓存读取API。
- S5.2 新增/Users/itboybob/Hexis/hexis-ui/e2e/voice-barge-in.spec.ts，复用fixtures/voice.ts中的installVoiceWavFeed。拟新增probe通过--profile读取仓库C6，以repository_root解析配置相对路径；C3绑定同一源码、C1/C2/C4/C5/C7/C8、D1实际音频、API/UI及隔离DB，并按最新M5的fingerprint方式验证来源，替换旧startVoiceServices的历史路径与未知常驻服务复用假设。
- S5.3 真实浏览器/模型/生产Talk/真实Qwen端到端。必要时加入最小测试诊断接口，仅暴露已存在的帧位置、目标身份和播放器反馈到测试观测层，不能伪造事件或绕过gate。记录输入帧、目标、command_id、新增消息游标、playback回执、barge结果、provider终态和下一轮恢复。

**数据与配置：**冻结C1/C2、D1独立验收集、D2兼容素材、C3单次运行记录、C4/C5/C6/C7/C8及环境凭据。需要正在使用的生产UI构建与可达真实服务；隔离DB准备与清理对象限定本次运行生成的测试实例。

**测试内容与方法：**纯噪音/有效插话先等当前响应实际audible，再按本次游标与身份注入。纯噪音窗口覆盖注入及尾部恢复期间，检查没有新增确认、没有候选导致的interrupt，原输出继续；有效人声检查匹配目标恰好一次interrupt和必要云端取消，迟到旧音频无复活，下一句能正常回答。生成中未audible、生成完成但仍在播分别测试，不能把chunk到达当可听播放，不能只检查历史controls.includes(barge_in)。取消的宿主accepted、供应商终态、本地interrupt分开记录；没有真实供应商终态则该部分为Unknown。

**审查补充：**增加真实页面中人为延迟HTTP open/audible/progress请求、WS目标确认先到及provider终态先到的案例；证明前缀来自实际播放器emit、最终HTTP重复得到幂等确认、历史终态一致及下一轮恢复。测试缺ready或模型未ready时没有正式采集；有效输入但不够打断证据时原输出自然完成后正确回复，不误称发生取消。

**浏览器规则：**先核对应用内Browser是否支持grantPermissions/addInitScript；能力不足时记录缺口并采用既有headful Chrome注入路径。若注入仍不可用，保留S1-S4验证，明确缺口及已验证层级，把相关真实Talk场景移交S6直接人工测试，不把绕过前端的Python harness算作这两项功能的自动通过。

**完成检查：**独立案例逐项通过并保存来源、指纹和断言结果；必要失败保留。取消率/延迟统计仅诊断，不用95%或P95≤500ms作为统一门槛。自动化可通过部分与移交人工的缺口明确分开。参数或实现变化后重新跑受影响案例及冻结验收集，必要时准备新的独立素材。

**执行/验收：**代理；无法自动覆盖部分允许直接人工接手。

## 8. Sprint 6：真人现场验收与必要修正

**任务：**用S5的源码、资源和参数版本开启可见Chat，由用户在真实设备和现场噪音下自然插话，尝试“停/等一下”、轻声以及噪音期间继续听回答。代理可记录界面、事件及异常，用户决定误停、顺畅度、短词和首字完整性是否可接受。

**数据与配置：**D4真实现场；冻结C1/C2/C3/C4/C5/C6/C7；逐项列出S5交接的自动化缺口。录音保存需用户明确选择，真人直接测试不强制要求提供文件。

**方法与完成检查：**人工不设固定轮数、成功率、P95或数值体验门槛。用户提出问题后，保留失败现象；参数修正回到S5冻结及回归，代码修正回到所属Sprint做对应自动检查，再交用户复验。只有用户明确接受才记人工通过；未答复、代理注入、模型自评均不等于人工接受。结论记录版本和自动化缺口，不能把现场听感外推为此前缺失的供应商取消证据。

**执行/验收：**人类最终判断；代理监测、必要修正及回归。产品实施权限以届时有效用户授权为准。

## 9. 共用执行方式与检查命令

每个实现任务按“写行为断言→确认既有实现失败→最小实现→相关测试通过→记录交付边界”推进；涉及协议时先读设计来源及当前SoT。以下均为未来实施命令，本轮不执行。

命令执行意图：检查前端采集、门控、播放器和Talk回归。预期输出：相关Vitest通过；真实Worker效果由浏览器案例补充。

```bash
npm --prefix /Users/itboybob/Hexis/hexis-ui test -- lib/voice-realtime lib/voice-playback app/chat/talk-mode.test.tsx
```

命令执行意图：检查目标协议、会话/输出并发及播放API。预期输出：相关pytest通过；mock与真实云端证据分别标注。

```bash
source /Users/itboybob/Hexis/.venv/bin/activate
python -m pytest /Users/itboybob/Hexis/tests/voice_realtime /Users/itboybob/Hexis/tests/web/test_voice_playback_api.py /Users/itboybob/Hexis/tests/web/test_voice_barge_in_api.py -q
```

浏览器命令在C3生成且预检通过后使用其明确选择的运行方式，避免硬编码当前不存在的环境绑定；S2/S5执行时必须提供能复现的实际命令、命令执行意图和预期输出。

## 10. 发布恢复与全局完成检查

本计划不直接发布。将来开发/构建验证失败时保留失败证据与前一完整配置版本，停止继续切换；服务端能力与UI不匹配时明确阻止新Talk开启，不能降级为无目标取消。正式部署若另获授权，UI与API按匹配版本整体发布，恢复至匹配的上一完整版本并验证媒体可重开；不执行数据库重置或历史改写。

- [ ] 固定资源由正式构建交付，运行不依赖个人缓存。
- [ ] 浏览器Worker真实推理、身份与生命周期成立。
- [ ] 目标协议前后端同步、取消与创建无竞态或死锁。
- [ ] 候选、有效输入、确认打断解耦；正式PCM/区间/hash完整。
- [ ] M5暂停/恢复、输出双终态、下一轮及Avatar兼容。
- [ ] 真实自动验收逐项取证或明确缺口，未拿mock替代。
- [ ] 人类明确接受现场体验，参数/代码修正后的回归完成。

## 附录：审查依据

- 架构新增字段与行为：设计§5.1-5.8、附录A；现有媒体协议：/Users/itboybob/Hexis/services/voice_realtime_transport.py。
- 当前生产资源构建：/Users/itboybob/Hexis/ops/Dockerfile.ui，npm ci --ignore-scripts→npm run build→复制public。
- 当前直接打断：/Users/itboybob/Hexis/hexis-ui/lib/voice-realtime/capture.ts的speech_start分支与TalkMode本地停播。
- 当前输出创建与等待：/Users/itboybob/Hexis/services/voice_realtime_session.py的start_authorized_response/start_voice_tool_followup。
- 当前浏览器注入：/Users/itboybob/Hexis/hexis-ui/e2e/fixtures/voice.ts；显式运行来源：m5-tools-media.spec.ts。
- 审查结论记录在本目录的Architect审查记录中；v1.3已对照当前代码审查通过，仍未实施或运行产品验证。


## 长期配置落盘补充（2026-10-08）

本文件迁入Hexis仓库作为长期计划依据；原始审查结论和代码快照保留，不冒称新运行验收。长期配置见[配置说明](/Users/itboybob/Hexis/config/voice-barge-in/配置说明.md)。本次新增配置均未激活；模型/运行资源构建、加载器、真实音频校准与产品接线仍待对应Sprint实施。历史缓存中的原始取证文件保留。

本次文档维护记录HEAD为 `989bf2c539d8f0784a7ec0891e56da87d5e18e6f`；相对原fadb审查快照仅新增他人的 `docs/operations/2026-10-08-连续性上下文配置变更.md`，核心媒体代码未变。这是路径/配置迁入与用户测试裁决的补充，不代表新一轮Architect审查或运行验收；原v1.3结论和差异证据保留。

用户指定的已知存量测试类型诊断不计本轮失败；测试仍以Task定向测试、真实运行断言及本轮新增问题判定，不忽略实际运行失败，不关闭产品类型检查。Doc-Updater于2026-10-08维护；正式执行入口为[完整执行计划](</Users/itboybob/Project/geek_interview/docs/implementation plan/2026-10-08-M6-Chat人声检测与确认打断执行计划.md>)，6个Sprint均未实施。

## 执行补充（2026-10-08，完整计划v1.2）

本节对齐[完整执行计划v1.2](</Users/itboybob/Project/geek_interview/docs/implementation plan/2026-10-08-M6-Chat人声检测与确认打断执行计划.md>)，保留原v1.3 Architect审查历史、协议结论和基线证据；本节不代表新增内容已获Architect复审或运行验收。原§9及§7中统一要求C3、内置能力不足才回退外部浏览器的测试前提由本节及完整计划替代：S2组件测试只要求W1，真实Talk才要求C3，两类浏览器按Sprint分工。计划仍为6 Sprint、27 Task，全部未实施。

证据分四类且不可互代：E1记录隔离环境及owned资源/实际DB身份；W1记录无API/DB/密钥的Worker组件测试构建与运行，停止UI后保留原记录作证，不可作为release；U1记录S5真实Talk UI所回读的数据库和API upstream身份；C3在Talk运行前由probe绑定最新E1/W1/U1、API自身启动记录、冻结参数、源码/资源/样本及真实DB身份。任何相关构建、参数或源码变化须生成对应新运行记录，旧C3不得继续验收。

S5顺序为：S5.1实现probe及其纯mock单测，并运行`phase=environment`预检；此阶段不启动API/UI、不要求identity/consent、不生成C3。S5.2仅用显式W1 Worker UI和D1调参、冻结参数；结束时由精确的owned记录停止Worker UI。S5.3才依次检查talk-db、显式创建selection并按授权准备identity/consent、启动API、由API真实连接并执行迁移后自行写API startup record、有界health回读、启动Talk UI并生成U1、运行`phase=talk` probe生成新的C3，最后执行Talk E2E。Worker UI与Talk UI不得同时占用或复用3477；stop只停所选owned UI进程，保留DB/API/卷及已有证据。C3的prepared状态只表示可复现身份已核验，不表示Talk已经运行。

隔离Compose栈必须使用两个独立服务与卷：`pytest-db`供全局测试夹具创建`tmp_test_<uuid>`临时库，`talk-db`只供API/Talk runtime。二者均从Hexis `ops/Dockerfile.db`构建并有明确端口、project及身份；API不连主库。纯environment/probe pytest可直接运行，在对应测试模块局部覆盖`temp_test_db`为no-op并mock DB/network/process/cloud，不要求E1；依赖真实DB的pytest必须先授权prepare/check专属pytest-db，再通过`run-pytest`从E1注入`POSTGRES_*`。不得只改`POSTGRES_DB`伪称隔离，也不得全局skip现有DB集成测试。

仅`prepare-db`对新talk-db执行`core/schema.py` baseline及所需forward migrations；API每次启动只执行`apply_migrations`，不得重复应用全部`db/*.sql`。显式voice-barge-in API选择下迁移失败必须阻止API启动，不能将warning当成功；`api-start.json`只能由API在真实连接、迁移成功后生成。UI容器内DB/API upstream不得使用宿主`127.0.0.1`，须为Compose service name或经验证的host地址，并与浏览器公开URL分别记录。

当前C6 v1配置和JSON均未修改。Task 1.4未来升级到schema v2：`database`明确pytest-db/talk-db服务、`POSTGRES_USER/PASSWORD`环境变量名及admin DB；`runtime_environment`明确API选择`HEXIS_VOICE_BARGE_IN_ENV`、API启动记录变量和UI `HEXIS_DATABASE_URL/DATABASE_URL/HEXIS_API_URL`变量；`browser`只接受`inspection=in_app`、`automation=playwright_chrome`、`headless=false`及S1-S6 `sprint_usage`映射。Task 1.5环境loader与Task 5.1 probe共用该schema/校验规则。旧`database_url_env`、旧browser fallback字段、新旧字段混用或缺少选择时均拒绝运行；具体路径解析和字段测试以完整执行计划Task 1.4定义为准。

主计划命令按Task拆分并分别标出意图/预期结果和实际测试文件/用例数。仅所选E1中owned的Worker/Talk UI可由`stop`停止；不得清理DB/卷或覆盖W1/U1/C3旧证据。当前只更新两份文档：功能、依赖安装、C6配置升级、环境创建和真实运行均未实施；样本准备、浏览器、云端及现场验收仍待执行，不能标完成。

浏览器分工：内置Browser用于S1资源URL/错误、S2测试页/错误、S4/S6可见交互及真人判断；Playwright可见外部Chrome用于S2真实Worker/WASM、S5真实音频注入及故障自动化。它们分别记录证据，不把内置观察当可重放自动断言，也不把外部Chrome称为内置Browser。默认测试入口关闭，Worker测试构建与Talk正式构建分开取证。
