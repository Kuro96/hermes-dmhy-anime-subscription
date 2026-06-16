# ADR: Organizer 文件名解析架构

## 状态

Accepted

## 背景

Organizer 需要把已完成的动画下载安全地规划到媒体库目录。旧实现把大量字幕组命名、季度、分卷、合集、集数范围和标题清理规则都塞进私有解析函数，导致测试锁定 `_parse_episode` 等内部细节，也让新边界案例很容易继续堆正则。

当前项目没有 LLM agent、外部 API 或人工回退执行器。解析失败时，唯一安全行为是保持现有 organizer 的 unsorted 路径和 `organizer_unsorted` 事件，让操作者之后人工处理。

## 决策

Organizer 文件名解析改为小型、确定性的 regex-first 解析器，只接受高置信格式：普通 `Title - 01`、`S02E03`、`S02 - 03`、`Season 2 - 03`、CJK `第2季 第03話`，以及连续方括号中的 `release group / title / episode / quality` 形态。

解析器不再尝试猜测复杂或含混文件名。集数范围、disc、part、cour、volume-heavy、纯数字标题不明确等情况标记为需要回退，当前行为就是 `episode=None`，由 organizer 规划到 `_Unsorted` 并发出 warning event。

测试改为从 `organize_media` 入口验证行为：目标路径、unsorted、字幕保留、extras/sample 过滤、路径净化、冲突、不覆盖、apply copy、Bangumi 标题注入。私有解析函数不再作为测试契约。

## 后果

支持范围更小但更诚实：简单单集发布会稳定进入媒体库，复杂发布不会被错误移动到错误季集。

新增命名格式必须先证明是高置信、行为可验证，再加入解析器和入口级测试；不能为了单个含混样例继续扩大私有正则矩阵。

DMHY RSS 的 pack 判断仍属于 `dmhy.py` / rules / workflow 层，organizer 不承担 RSS pack 分类职责。

## 非目标

不新增 LLM、agent、API、凭据或配置回退面。

不实现复杂合集拆分、季度包自动展开、disc/volume/part/cour 的智能语义识别。

不改变 organizer 的安全边界：发现视频和字幕、过滤 extras/sample、目标路径净化、冲突处理、dry-run/apply copy、Bangumi lookup 注入和事件行为都应保持。
