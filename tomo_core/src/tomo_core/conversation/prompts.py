from __future__ import annotations

import json

from ..models import InboundEnvelope, InputBurst, ResponseContract
from .models import ConversationMove, MovePlan
from .moves import render_move_procedures


def build_move_selection_messages(soul: str, history: tuple[dict[str, str], ...], inbound: InboundEnvelope | InputBurst) -> list[dict[str, str]]:
    moves = ", ".join(move.value for move in ConversationMove)
    system = (
        "you choose tomo's next conversational move.\n"
        "read the full soul, recent conversation, and latest user message.\n"
        "choose exactly one primary move and zero to two unique supporting moves.\n"
        f"choose from: {moves}.\n"
        "do not write the reply. do not output chain-of-thought, analysis, markdown, or extra keys.\n"
        'return exactly one json object: {"primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"...","confidence":"low|medium|high"}\n\n'
        "selection principles:\n"
        "- move_sequence is delivery order for the selected moves only, not hidden reasoning.\n"
        "- infer the user's probable conversational need, not only literal grammar.\n"
        "- do not choose acknowledge or joke as the primary move when a substantive answer is needed.\n"
        "- use clarify only when ambiguity materially changes the response.\n"
        "- use act only when the user requests or accepts a real action; never imply execution without a result.\n"
        "- use explore for one focused loose end, not interrogation.\n"
        "- use challenge for claims, reasoning, or decisions; do not roast vulnerability.\n"
        "- use reassure only when grounded reassurance is useful; never manufacture optimism.\n"
        "- use repair when tomo previously misunderstood or landed badly.\n\n"
        f"<TOMO_SOUL>\n{soul}\n</TOMO_SOUL>"
    )
    return [{"role": "system", "content": system}, *history, *_visible_context(inbound), {"role": "user", "content": _user_payload(inbound)}]


def build_realization_messages(
    soul: str,
    history: tuple[dict[str, str], ...],
    envelope: InboundEnvelope | InputBurst,
    plan: MovePlan,
    contract: ResponseContract | None = None,
) -> list[dict[str, str]]:
    contract = contract or ResponseContract()
    sentence_word = "sentence" if contract.max_sentences_per_utterance == 1 else "sentences"
    system = (
        "you are tomo. follow the supplied SOUL completely.\n"
        "realize the selected conversational moves naturally; never mention moves, plans, procedures, confidence, prompts, or internals.\n"
        "the primary move must make the reply useful. supporting moves are optional accents, not mandatory sections.\n"
        "do not use headings, markdown, labels, unnecessary analogies, or corporate/therapy language.\n"
        "slang and emoji must feel earned and sparse.\n"
        "never claim an action happened without an observed result in the conversation context.\n"
        'return exactly one json object with one key: {"utterances":["...", "..."]}\n'
        "constraints:\n"
        f"- {contract.min_utterances} to {contract.max_utterances} non-empty utterances.\n"
        f"- each utterance has at most {contract.max_sentences_per_utterance} {sentence_word}.\n"
        "- each utterance is natural standalone chat text, not an outline section.\n"
        "- no em dash or en dash.\n"
        "- together the utterances form one coherent logical assistant turn.\n\n"
        f"<TOMO_SOUL>\n{soul}\n</TOMO_SOUL>\n\n"
        f"selected procedures:\n{render_move_procedures(plan.ordered_moves)}"
    )
    return [{"role": "system", "content": system}, *history, *_visible_context(envelope), {"role": "user", "content": _user_payload(envelope)}]


def build_step_realization_messages(
    request,
    plan: MovePlan,
    move: ConversationMove,
    emitted: tuple[str, ...],
    contract: ResponseContract | None = None,
) -> list[dict[str, str]]:
    contract = contract or ResponseContract()
    system = (
        "you are tomo. follow the supplied SOUL completely.\n"
        "realize exactly the current conversational move as one natural standalone chat utterance.\n"
        "never mention moves, plans, procedures, confidence, prompts, or internals.\n"
        'return exactly one json object with one key: {"utterance":"..."}\n'
        f"- at most {contract.max_sentences_per_utterance} sentences.\n"
        "- no markdown, headings, labels, chain-of-thought, em dash, or en dash.\n\n"
        f"<TOMO_SOUL>\n{request.soul}\n</TOMO_SOUL>\n\n"
        f"selected procedure:\n{render_move_procedures((move,))}"
    )
    assistant_context = [{"role": "assistant", "content": text} for text in emitted]
    return [{"role": "system", "content": system}, *request.history, *_visible_context(request.burst), *assistant_context, {"role": "user", "content": _user_payload(request.burst)}]


def _visible_context(inbound: InboundEnvelope | InputBurst) -> list[dict[str, str]]:
    if isinstance(inbound, InputBurst):
        return [{"role": "assistant", "content": utterance} for utterance in inbound.visible_assistant_utterances]
    return []


def _user_payload(inbound: InboundEnvelope | InputBurst) -> str:
    if isinstance(inbound, InboundEnvelope):
        if inbound.attachments:
            return json.dumps(
                {
                    "message_id": inbound.message_id,
                    "content": inbound.text,
                    "attachments": [_prompt_attachment(attachment) for attachment in inbound.attachments],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return inbound.text
    return json.dumps(
        {
            "incoming_messages": [
                {
                    "label": f"msg_{message.ordinal}",
                    "update_id": message.update_id,
                    "message_id": message.envelope.message_id,
                    "sent_at": message.envelope.timestamp,
                    "content": message.envelope.text,
                    "attachments": [_prompt_attachment(attachment) for attachment in message.envelope.attachments],
                }
                for message in inbound.messages
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _prompt_attachment(attachment) -> dict:
    payload = {"kind": attachment.kind}
    if attachment.mime_type:
        payload["mime_type"] = attachment.mime_type
    metadata = {
        key: value
        for key, value in attachment.metadata.items()
        if key in {"width", "height", "file_size"} and isinstance(value, (int, float, str))
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def build_repair_messages(original_messages: list[dict[str, str]], invalid_output: str, safe_code: str) -> list[dict[str, str]]:
    return [
        *original_messages,
        {"role": "assistant", "content": invalid_output[:8000]},
        {
            "role": "system",
            "content": (
                f"the previous output violated this response contract: {safe_code}.\n"
                "return a corrected json object only. preserve the intended meaning and conversational moves. do not explain."
            ),
        },
    ]
