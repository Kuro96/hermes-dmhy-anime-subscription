# ADR: Organizer 文件名解析架构

## 状态

Accepted

## 背景

Organizer 需要把已完成的动画下载安全地规划到媒体库目录。旧实现把大量字幕组命名、季度、分卷、合集、集数范围和标题清理规则都塞进私有解析函数，导致测试锁定 `_parse_episode` 等内部细节，也让新边界案例很容易继续堆正则。

当前项目没有内建 LLM 服务、外部 API 或人工回退执行器。解析失败时，默认安全行为必须保持现有 organizer 的 unsorted 路径和 `organizer_unsorted` 事件，让操作者之后人工处理。

## 决策

Organizer 文件名解析改为小型、确定性的 regex-first 解析器，只接受高置信格式：普通 `Title - 01`、`S02E03`、`S02 - 03`、`Season 2 - 03`、简单英文序数 `2nd Season - 03`、CJK `第2季 第03話`，以及连续方括号中的 `release group / title / episode / quality` 形态。

解析器不再尝试猜测复杂或含混文件名。集数范围、disc、part、cour、volume-heavy、复杂季度营销标签、纯数字标题不明确等情况标记为需要回退；没有注入 agent/parser 时，当前行为就是 `episode=None`，由 organizer 规划到 `_Unsorted` 并发出 warning event，而不是把含季度信息但无法高置信解析的标题默认放入 `Season 01`。

订阅规则和 Bangumi subject 上下文（例如 `bangumi_subject_id`）才是工作流里的季度身份来源。Organizer 标题解析器只是高置信辅助：当调用方已经以 rule/subject 表达了季的身份时，不能再从任意发布标题营销文案里推断或覆盖该身份；标题中的复杂 season label 解析不了时必须进入回退/unsorted 边界。

当调用方确实有更高层的 agent 或人工审核结果时，可以在 `organize_media` 入口注入 `episode_parser` callable。Organizer 仍然先运行确定性 regex 解析；只有简单解析拿不到标题或集数时才调用该回退。回退只返回结构化的标题、季、集、字幕组和画质字段。回退抛错、返回空值或未注入时都保持默认 unsorted 安全行为。

工作流还支持可选的 `organizer.episode_parser.mode=callback`，用于把默认 organizer 路径桥接到受信任的 Hermes callback bridge。该配置只保存 URL 环境变量名，运行时用 stdlib HTTP POST 调用；依赖注入的 `episode_parser` 仍优先于配置回调。回调请求采用隐私最小化 payload：只发送 task、待解析 title/text、源文件 basename (`source_name`)，以及 `safe_context` 中的 `rule_name`、`bangumi_subject_id`、`release_group`、`quality`、`category` allowlist。不得发送绝对路径、content/save path、torrent hash、job id、webhook URL、token、用户名、chat id、凭据、环境变量值或 raw metadata。缺少环境变量、URL 非法、超时、HTTP 错误、非 JSON、低置信度或字段非法时都返回空结果，让 organizer 继续进入 `_Unsorted`。

多文件 torrent 中的 `01.mkv`、`02.mkv` 这类纯数字 stem 只在 `prefer_stem_episode` 场景下作为高置信集数 token 使用，系列标题和季信息仍来自 torrent title 或注入回退，避免把单文件纯数字标题误判为剧名/集数。

测试改为从 `organize_media` 入口验证行为：目标路径、unsorted、字幕保留、extras/sample 过滤、路径净化、冲突、不覆盖、apply copy、Bangumi 标题注入。私有解析函数不再作为测试契约。

## 后果

支持范围更小但更诚实：简单单集发布会稳定进入媒体库，复杂发布不会被错误移动到错误季集。

新增命名格式必须先证明是高置信、行为可验证，再加入解析器和入口级测试；不能为了单个含混样例继续扩大私有正则矩阵。

DMHY RSS 的 pack 判断仍属于 `dmhy.py` / rules / workflow 层，organizer 不承担 RSS pack 分类职责。

## 非目标

不新增内建 LLM、凭据或自动猜测逻辑；agent 能力只通过调用方显式注入的 Python callable，或显式启用且隐私最小化的 HTTP callback 桥接表达。

不实现复杂合集拆分、季度包自动展开、disc/volume/part/cour 的智能语义识别。

不改变 organizer 的安全边界：发现视频和字幕、过滤 extras/sample、目标路径净化、冲突处理、apply copy、Bangumi lookup 注入和事件行为都应保持。
