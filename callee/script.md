# PitchLoop consented teammate call script

Every live Zero.xyz call in this demo goes to the one consenting teammate number supplied through `CALLEE_PHONE_E164`. The queue contains fictional owners and businesses; it is not a phone list.

## Setup

- Confirm the teammate owns the configured number and consents to each call and any transcript capture.
- Use the dashboard's current queue card to play that fictional owner.
- Never say a real employer, customer, phone number, or personal detail.
- Allow a brief, natural back-and-forth. Pauses, “um,” “yeah,” and clarification questions are encouraged.
- End with the exact outcome sentence shown for the current objection so the deterministic parser can receipt it.

## Queue outcomes

1. Nina / missing website audit: “Mm, okay, but you haven't actually looked at our website, have you? I think I'm going to pass for now.”
2. Samir / unclear outcome: “I hear you, but I'm still not sure what that changes for the business. Let me think about it and, uh, don't book anything yet.”
3. Carla / wants proof: “Maybe, but can you show me something you've actually done for a business like ours? Without that, I'm not ready to take a meeting.”
4. Ben / too much work: “Oof, that sounds like a whole project, and we're already stretched thin. I can't take that on right now.”
5. Tasha / timing: “Yeah, the idea makes sense, but the timing is rough. I can't justify a big website project this month.”
6. Derek / meeting: “Yeah, okay, that actually sounds useful. Send me a 20-minute invite for Tuesday at 2 PM.”

All artifacts must keep the number redacted. Do not commit raw audio, credentials, tokens, or wallet details.
