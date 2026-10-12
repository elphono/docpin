---
description: A request for something to review leads to an HTML document under docs/, with a diagram, and the link handed over
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit]
---

I'd like to review how a token-bucket rate limiter for our public API would work before anyone writes code: how tokens refill, what a client sees when it runs out, and the two or three settings we would have to choose. Write it up for me to read and comment on.
