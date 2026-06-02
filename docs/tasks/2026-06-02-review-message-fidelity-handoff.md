# Task 1: Review Message Fidelity And Handoff Handling

## Summary

Improve the current WeCom review platform so customer service staff can see the real conversation context, including images, correctly separated customer and reply messages, and a clear handoff queue for manual handling.

## Goals

- Show image messages in the review page when the relay/review platform receives captured WeCom images.
- Keep captured-image success and failure states visible to reviewers.
- Classify conversation history into customer messages, AI replies, and human/customer-service replies without treating all historical messages as customer text.
- Route handoff requests into the current review platform with a prominent pending indicator.
- Distinguish direct handoff from indirect handoff:
  - Direct handoff: customer text contains terms such as "人工", "转人工", or "人工客服".
  - Indirect handoff: AI decides the case requires handoff because it cannot answer safely, detects escalation, or sees an emotional/complex issue.
- Filter Markdown special syntax in both review display and final WeCom send text, while preserving readable line breaks and list formatting.

## Implementation Notes

- Keep the change scoped to the existing review flow and queue database.
- Add a safe media-serving path for images already recorded in `conversation_messages.media_json`; do not expose arbitrary local files.
- Prefer persisting a stable `message_type` such as `customer`, `reply`, or `unknown` when conversation messages are recorded.
- Update review item payloads so the frontend can render role-specific bubbles and media attachments directly from backend data.
- Add a new or clearly separated handoff state/category in the review page, with visible badges and item-level reason text.
- When a direct handoff is detected, avoid unnecessary AI drafting and move the item to manual handling.
- When AI returns `action=handoff`, preserve the AI handoff reason and show it in the review item.
- Put Markdown cleanup in a shared helper so display, save/approve, and final send paths use consistent text normalization.

## Acceptance Criteria

- A review item containing a captured image displays the image thumbnail in the matching message bubble.
- If image capture failed, the review page shows an explicit placeholder instead of silently hiding the message.
- Historical customer-service replies display as replies, not as customer messages.
- The sample reply about the white plush toy is classified as a reply when it is historical AI/customer-service output.
- Customer messages containing "人工" enter the handoff pending category without waiting for an AI draft.
- AI handoff decisions enter the same handoff pending category and are labeled as indirect handoff.
- Reviewers can clearly see which handoff items still require handling.
- Markdown markers such as headings, bold/italic markers, quote markers, code fences, and raw special tags do not appear in customer-facing send text.
- Line breaks and bullet-list readability remain intact after cleanup.

## Suggested Tests

- Unit test image media payloads in review items and safe image route behavior.
- Unit test role classification for customer, AI reply, human reply, and unknown/system messages.
- Unit test direct handoff detection from customer text.
- Unit test indirect handoff from AI `action=handoff`.
- Unit test Markdown cleanup for display and send paths.
- Regression test review save, approve, reject, regenerate, and final send recheck behavior.
