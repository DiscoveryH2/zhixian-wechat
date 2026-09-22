Source snapshot downloaded 2026-09-22 from https://github.com/jev-chat/jev-chat-jarvis/tree/main.

Only judgment questions, HTTP-facing model orchestration, knowledge context models, and original LICENSE/NOTICE are included. No upstream program was executed.

The seven question keys and rubric are compatible with JevQuestions.kt. The desktop implementation adds the upstream background note to judgment and ranking, carries background/history separately in state, and retains the seven question meanings. In particular, should_reply_now means “should the next message contain substantive content”, not “send immediately”.

The local core does not duplicate Android's retry-without-background behavior, fabricate missing confidence as zero, or silently synthesize candidates. Missing answers remain unknown; generation failure leaves valid judgments available.
