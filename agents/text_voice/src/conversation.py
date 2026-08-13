"""Conversational Q&A flow + TwiML generation for the Text/Voice Agent.

Two flows, per the LQABR design:

  Flow A — no answer / voicemail:
      deliver a customized voicemail message, then send a customized SMS.

  Flow B — the lead answers:
      engage with a specific question-and-answer pattern (speech Gather).
      Each answered step is captured; completing the flow with interest
      records CALL_ENGAGED on top of CALL_ANSWERED.

TwiML is generated as plain XML strings — no SDK dependency, trivially
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional
from xml.sax.saxutils import escape

VOICE = "Polly.Joanna"

# ---------------------------------------------------------------- templates

VOICEMAIL_TEMPLATE = (
    "Hi {first_name}, this is {sender_name} reaching out regarding {company}. "
    "We recently shared some material on how teams in {industry} are qualifying "
    "leads automatically, and I'd love two minutes of your time. "
    "I'll follow up with a text message you can reply to. Thank you!"
)

FOLLOWUP_SMS_TEMPLATE = (
    "Hi {first_name}, {sender_name} here — just left you a voicemail. "
    "When you have a moment, reply YES and I'll send over a short overview "
    "for {company}. Reply STOP to opt out."
)

# The specific Q&A pattern used when a lead answers the call.
QA_STEPS: Dict[int, Dict[str, str]] = {
    1: {
        "question": ("Hi {first_name}, this is {sender_name}'s assistant calling for "
                     "{company}. Do you have a quick minute to answer two short "
                     "questions? Please say yes or no."),
        "on_no": "No problem at all, {first_name}. I'll send a short text instead. Have a great day!",
    },
    2: {
        "question": ("Great, thank you! Is {company} currently looking at ways to "
                     "improve how you find and qualify new prospects? Please say yes or no."),
        "on_no": "Understood — thanks for your time. If anything changes we'd love to help. Goodbye!",
    },
    3: {
        "question": ("Perfect. Would it be helpful if we emailed you a calendar link to "
                     "pick a time with our team — mornings or afternoons both work. "
                     "Please say yes or no."),
        "on_no": "No worries. I'll email a summary you can read anytime. Thanks again — goodbye!",
    },
}
FINAL_STEP = max(QA_STEPS)

CLOSING_ENGAGED = ("Wonderful! You'll receive an email with available times shortly. "
                   "Thanks so much, {first_name} — talk soon!")


@dataclass(frozen=True)
class QAResult:
    completed: bool          # reached the end of the flow
    engaged: bool            # said yes through the final step
    last_step: int


# ------------------------------------------------------------------- twiml

def _say(text: str) -> str:
    return f'<Say voice="{VOICE}">{escape(text)}</Say>'


def twiml_voicemail(context: Dict[str, str]) -> str:
    """Flow A: message delivered after the answering machine's beep."""
    message = VOICEMAIL_TEMPLATE.format(**context)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{_say(message)}<Hangup/></Response>'


def twiml_question(step: int, context: Dict[str, str], action_url: str) -> str:
    """Flow B: ask QA step `step` and gather a speech answer."""
    question = QA_STEPS[step]["question"].format(**context)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" timeout="4" speechTimeout="auto" '
        f'action="{escape(action_url, {chr(34): "&quot;"})}" method="POST">'
        f"{_say(question)}"
        "</Gather>"
        f"{_say('Sorry, I did not catch that. I will follow up by text. Goodbye!')}"
        "<Hangup/></Response>"
    )


def twiml_goodbye(text: str, context: Dict[str, str]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f"{_say(text.format(**context))}<Hangup/></Response>")


def is_affirmative(speech: Optional[str]) -> bool:
    if not speech:
        return False
    words = speech.lower()
    return any(w in words for w in ("yes", "yeah", "yep", "sure", "ok", "okay", "definitely", "absolutely"))


def next_action(step: int, speech: Optional[str], context: Dict[str, str],
                action_url_for: "callable") -> tuple[str, Optional[QAResult]]:
    """State machine for the Q&A pattern.

    Returns (twiml, result). `result` is set when the flow ends; `None` while
    more steps remain. `action_url_for(step)` builds the gather callback URL.
    """
    if not is_affirmative(speech):
        return (twiml_goodbye(QA_STEPS[step]["on_no"], context),
                QAResult(completed=True, engaged=False, last_step=step))
    if step >= FINAL_STEP:
        return (twiml_goodbye(CLOSING_ENGAGED, context),
                QAResult(completed=True, engaged=True, last_step=step))
    next_step = step + 1
    return (twiml_question(next_step, context, action_url_for(next_step)), None)
