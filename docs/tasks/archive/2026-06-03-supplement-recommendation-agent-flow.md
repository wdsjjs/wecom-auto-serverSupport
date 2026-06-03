# 补剂推荐 Agent 流程

## Summary

补剂推荐需要独立 Agent 处理。该流程区别于普通 AI 客服回复、新用户欢迎流程和人工回复，专门负责识别补剂推荐意图、询问基础信息、按推荐规则挖需，并在明确需求后推荐合适产品。

本文只沉淀补剂推荐 Agent 的方案、流程、状态和日志规范，不实现数据重建、Agent 代码或日志写入逻辑。

补剂推荐草稿统一使用：

```text
reply_source=supplement
```

这用于和普通 AI 回复、欢迎话术、人工回复区分，也方便 review 页面、指标统计和问题排查。

## 触发条件

补剂推荐 Agent 在以下任一条件满足时触发：

- 用户明确表达补剂推荐意图，例如 `推荐`、`适合`、`吃什么`、`怎么搭配`、`需要补什么`、`想改善某个健康需求`。
- 当前客户已有补剂推荐 Agent 的 active 状态，即使用户只回复数字、基础信息或细分需求，也继续进入补剂推荐流程。

以下场景不触发补剂推荐 Agent，继续走现有普通客服或业务工具链路：

- 价格、起拍量、优惠、链接等商品销售信息。
- 发货时间、物流、订单状态。
- 已明确产品的吃法、禁忌、规格、检测报告等产品事实咨询。
- 改地址、退款、投诉、转人工等售后或人工接管场景。
- 单纯打招呼，且没有明确补剂推荐意图。

如果新用户欢迎话术发送后，用户首句同时包含明确补剂推荐意图，可以在欢迎流程完成后进入补剂推荐 Agent。欢迎完成状态和补剂推荐状态互相独立。

## 数据来源

补剂推荐 Agent 使用以下数据源：

- 推荐规则：SQL 中的 `10 补剂推荐`，例如 `feishu_supplement_recommendations`。
- 产品事实：SQL 中的 `5 产品常规信息`，例如 `feishu_product_basic_info`。
- L0 合规边界：SQL 中的 `7 L0级注意事项`，或 `kb_docs` 中的 `safety_policy`。
- 客户记忆：Mem0 中的客户基础信息、历史购买补剂、偏好、既往沟通摘要。

数据优先级：

1. 产品事实、推荐规则、禁忌、吃法、价格、链接、免责话术必须以 SQL 为准。
2. Mem0 只用于补充客户画像和历史上下文，不能覆盖 SQL 产品事实和推荐规则。
3. SQL 与 Mem0 冲突时，以 SQL 为准，并在日志中记录冲突或降级原因。
4. 本阶段不处理数据重建；默认目标机器已具备本地 SQL、Mem0 和知识库环境。

## 客户标识

客户维度按以下优先级确定：

1. 优先使用企微 `external_user_id`。
2. 如果暂时未绑定 `external_user_id`，使用本地稳定的 `conversation_key` 兜底。

不得让多个未绑定客户共用同一个全局客户 ID，避免补剂推荐状态、客户记忆和历史购买记录串到其他客户。

## 状态阶段

补剂推荐 Agent 状态按客户维度记录：

- `collecting_profile`：正在收集基础信息，例如年龄、性别、身高、体重、用药、孕哺、儿童年龄等。
- `digging_need`：已识别需求点，正在按推荐规则挖需。
- `ready_to_recommend`：需求已经明确，准备构建推荐产品。
- `recommended`：推荐草稿已成功发送，并完成流程状态提交。
- `closed`：流程正常关闭，例如客户已确认、转普通咨询或进入人工接管。
- `skipped`：流程跳过，例如上下文过期、客户转移话题、合规阻断、人工接管。

流程状态只能在发送成功后提交。生成草稿、进入审核、审核通过、发送前等待阶段都不能提前写入 `recommended`。

## Agent 流程

1. GUI Agent 扫描未读会话并读取最近聊天记录。
2. 路由模块判断是否触发补剂推荐 Agent。
3. 查询客户补剂推荐状态。
4. 如果是老用户，优先查询 Mem0，获取基础信息、历史购买补剂和既往需求；如果 Mem0 不可用或无有效信息，继续询问。
5. 如果缺少基础信息或需求点，发送 `10 补剂推荐` 中的首段基础信息话术。
6. 用户回复后解析基础信息、需求编号、需求文本、历史购买、风险信息。
7. 根据 `10 补剂推荐` 的 `需求点`、`挖需铺垫`、`挖需问题` 进行对话挖需。
8. 挖需默认最多 2 轮，配置上限不超过 3 轮。信息足够时直接推荐，不为了凑轮次而继续追问。
9. 明确需求后，按 `需求点 + 挖需结果` 查推荐规则。
10. 如果无法匹配具体 `挖需结果`，使用 `是否为兜底推荐产品=是` 的规则。
11. 同一结果下按 `相同挖需结果下的优先级` 排序，顺序为 `高 > 中 > 空`。
12. 关联 `5 产品常规信息`，补充适用年龄、服用方法、使用禁忌、产品之间搭配禁忌、规格、链接等事实。
13. 根据 `7 L0级注意事项` 做生成前和生成后合规校验。
14. ChatGPT 根据 SQL 和 Mem0 摘要生成补剂推荐草稿。
15. 草稿进入现有队列的 `ready` 状态；review 模式下等待人工审核。
16. 发送前重新打开会话复核，确认仍是同一客户、同一上下文。
17. 发送成功后记录 `recommended` 或下一阶段状态。
18. 如果发送前客户又发新消息导致上下文变化，当前草稿标记为跳过或重新排队，不提前提交推荐完成状态。

## 合规边界

补剂推荐 Agent 必须遵守 `7 L0级注意事项`：

- 补剂和药物不建议同天使用。
- 涉及购买入口或链接时，优先发送对应产品小程序卡片；未明确产品时发送小程序主页。
- 改地址必须核对订单是否已发货；已发货不支持修改地址，未发货提交工单。
- 禁止使用一级禁用极限词，例如最高、最佳、最强、最新、全网第一、绝对安全、保证有效等。
- 禁止使用疗效承诺词，例如专治、速效、断根、永久、治愈率、返老还童、回到 18 岁等。
- 疾病词只允许在用户主动提及时用于合规说明，不能表达治疗承诺。
- 禁止疾病治疗功效表达，例如降血压、降血糖、降血脂、预防三高、软化血管、修复神经等。
- 备孕、血管、前列腺、胰岛素等特殊场景必须附带知识库中的推荐后免责话术。

ChatGPT 可以组织表达，但不能自由编造产品事实、医疗功效、免责话术或固定首段话术。

## 阶段日志

补剂推荐 Agent 需要按阶段记录日志。日志用于排查、回放、指标统计和合规审计。

日志不得记录 API key、OpenAI 完整请求体、完整客户隐私画像、完整 Mem0 原文或其他敏感信息。必要时记录摘要字段、数量、哈希或短文本预览。

### 通用字段

所有补剂推荐日志都应包含以下通用字段：

- `event_type`：日志事件名。
- `trace_id`：一次补剂推荐流程的追踪 ID。
- `job_id`：当前队列任务 ID。
- `conversation_key`：本地会话标识。
- `external_user_id`：企微客户 ID，未绑定时可为空。
- `customer_id`：Agent 使用的客户维度 ID。
- `conversation`：会话标题或客户昵称。
- `reply_source`：固定为 `supplement`。
- `stage`：当前补剂流程阶段。
- `message_hash`：当前聊天上下文 hash。
- `latest_text_preview`：最新客户消息短预览。
- `created_at`：日志创建时间。

### 路由阶段

`supplement_route_evaluated`

- 触发时机：读取聊天后，判断是否进入补剂推荐 Agent。
- 记录字段：`triggered`、`matched_terms`、`active_state_exists`、`detected_intent`。

`supplement_route_skipped`

- 触发时机：判断不进入补剂推荐 Agent。
- 记录字段：`reason`、`detected_intent`。
- 常见原因：`shipping_intent`、`price_intent`、`order_intent`、`address_change_intent`、`no_recommendation_intent`。

### 状态阶段

`supplement_state_loaded`

- 触发时机：进入补剂推荐 Agent 后读取客户状态。
- 记录字段：`current_stage`、`digging_count`、`known_profile_fields`、`selected_needs`。

`supplement_state_committed`

- 触发时机：发送成功后提交状态。
- 记录字段：`previous_stage`、`next_stage`、`commit_reason`。
- 注意：该日志只能在发送成功后出现，不能在草稿生成或审核阶段提前出现。

### 记忆阶段

`supplement_memory_lookup_started`

- 触发时机：准备查询 Mem0。
- 记录字段：`mem0_enabled`、`customer_id`。

`supplement_memory_lookup_done`

- 触发时机：Mem0 查询成功。
- 记录字段：`hit_count`、`summary_fields`、`has_purchase_history`、`has_profile`。
- 注意：只记录字段名和摘要，不记录完整隐私内容。

`supplement_memory_lookup_failed`

- 触发时机：Mem0 查询失败或不可用。
- 记录字段：`error_type`、`fallback`。
- 常见降级：继续询问基础信息，不阻断补剂推荐流程。

### SQL 检索阶段

`supplement_rule_retrieval_done`

- 触发时机：完成推荐规则检索。
- 记录字段：`need_points`、`digging_results`、`source_rows`、`rule_count`。

`supplement_product_retrieval_done`

- 触发时机：完成产品事实检索。
- 记录字段：`products`、`source_rows`、`product_count`。

`supplement_safety_retrieval_done`

- 触发时机：完成 L0 合规规则检索。
- 记录字段：`safety_categories`、`source_rows`。

`supplement_retrieval_failed`

- 触发时机：推荐规则、产品事实或合规规则检索失败。
- 记录字段：`retrieval_type`、`error_type`、`fallback`。

### 挖需阶段

`supplement_profile_prompt_ready`

- 触发时机：生成基础信息首段话术。
- 记录字段：`template_source`、`missing_profile_fields`。

`supplement_need_parsed`

- 触发时机：解析用户回复中的需求。
- 记录字段：`need_numbers`、`need_texts`、`profile_fields_detected`、`risk_tags`。

`supplement_digging_question_ready`

- 触发时机：生成挖需问题。
- 记录字段：`digging_round`、`need_point`、`prelude_source_row`、`question_source_row`。

`supplement_digging_limit_reached`

- 触发时机：达到挖需上限。
- 记录字段：`digging_count`、`limit`、`next_action`。

### 推荐阶段

`supplement_recommendation_candidates_built`

- 触发时机：构建候选推荐产品。
- 记录字段：`candidate_products`、`priorities`、`fallback_flags`、`rule_source_rows`。

`supplement_recommendation_ready`

- 触发时机：最终推荐草稿准备完成。
- 记录字段：`final_products`、`product_count`、`more_than_three`、`has_disclaimer`。

### L0 合规阶段

`supplement_l0_check_started`

- 触发时机：开始合规检查。
- 记录字段：`check_target`，例如 `prompt_input`、`draft_reply`。

`supplement_l0_check_passed`

- 触发时机：合规检查通过。
- 记录字段：`checked_categories`。

`supplement_l0_check_blocked`

- 触发时机：命中禁用词、疗效承诺或疾病治疗表达。
- 记录字段：`blocked_categories`、`rewrite_required`、`handoff_required`。
- 注意：不记录敏感长文本，只记录分类和短预览。

### ChatGPT 阶段

`supplement_llm_started`

- 触发时机：准备调用 ChatGPT。
- 记录字段：`model`、`prompt_hash`、`rule_source_count`、`product_source_count`、`memory_hit_count`。

`supplement_llm_done`

- 触发时机：ChatGPT 返回成功。
- 记录字段：`duration_ms`、`action`、`confidence`。

`supplement_llm_failed`

- 触发时机：ChatGPT 调用失败或输出不可用。
- 记录字段：`error_type`、`duration_ms`、`fallback`。
- 注意：不记录 API key、完整请求体或完整响应体。

### 队列与审核阶段

`supplement_draft_ready`

- 触发时机：补剂推荐草稿进入队列 `ready` 状态。
- 记录字段：`reply_source`、`action`、`reply_preview`、`has_attachments`。

review 流程沿用现有 `review_saved`、`review_approved` 日志，通过 `reply_source=supplement` 区分补剂推荐草稿。

`supplement_review_edited`

- 触发时机：审核人修改了补剂推荐草稿。
- 记录字段：`original_reply_hash`、`final_reply_hash`、`editor_source`。

### 发送阶段

`supplement_send_recheck_started`

- 触发时机：发送前重新打开会话并复核上下文。
- 记录字段：`expected_message_hash`、`expected_latest_preview`。

`supplement_send_recheck_passed`

- 触发时机：发送前确认仍是同一客户、同一上下文。
- 记录字段：`current_message_hash`。

`supplement_send_recheck_stale`

- 触发时机：发送前发现客户又发新消息或上下文变化。
- 记录字段：`expected_latest_preview`、`actual_latest_preview`、`next_action`。
- 注意：出现该日志时不得提交 `recommended` 状态。

`supplement_sent`

- 触发时机：补剂推荐回复发送成功并在会话中可见。
- 记录字段：`final_reply_hash`、`final_products`、`message_hash_after_send`。

`supplement_send_failed`

- 触发时机：发送失败或发送后不可见。
- 记录字段：`error_type`、`retryable`、`next_action`。

### 跳过和关闭

`supplement_skipped`

- 触发时机：补剂推荐流程被跳过。
- 记录字段：`reason`、`stage`、`next_action`。
- 常见原因：`stale_context`、`manual_handoff`、`customer_changed_topic`、`l0_blocked`、`insufficient_evidence`。

`supplement_closed`

- 触发时机：补剂推荐流程关闭。
- 记录字段：`close_reason`、`final_stage`。
- 常见原因：`recommended`、`manual_handoff`、`customer_changed_topic`、`review_rejected`。

## Test Plan

- 文档存在性测试：确认 `docs/supplement-recommendation-agent-flow.md` 存在，章节完整。
- 日志完整性测试：每个 Agent 阶段至少有开始、完成、跳过或失败日志。
- 敏感信息测试：日志不记录 API key、完整 OpenAI 请求、完整隐私画像、完整 Mem0 原文。
- 状态提交测试：只有发送成功后才出现 `supplement_state_committed`。
- 上下文过期测试：出现 `supplement_send_recheck_stale` 时不提交推荐完成状态。
- 合规测试：L0 命中时产生 `supplement_l0_check_blocked` 或合规改写日志。
- 审核测试：review 修改补剂回复后保留 `reply_source=supplement` 或记录 `supplement_review_edited`。

## Assumptions

- 目标机器已具备本地 SQL、Mem0 和知识库环境。
- 本阶段只落文档，不实现数据重建、Agent 代码或日志写入逻辑。
- 推荐首段话术、3 款及以下和超过 3 款的推荐模板后续继续补充。
