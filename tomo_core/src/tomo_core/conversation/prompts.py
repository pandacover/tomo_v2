from __future__ import annotations

from ..models import InboundEnvelope, ResponseContract
from .models import ConversationMove, MovePlan
from .moves import render_move_procedures


def build_move_selection_messages(soul: str, history: tuple[dict[str, str], ...], envelope: InboundEnvelope) -> list[dict[str, str]]:
    moves = ", ".join(move.value for move in ConversationMove)
    system = (
        "you choose tomo's next conversational move.\n"
        "read the full soul, recent conversation, and latest user message.\n"
        "choose exactly one primary move and zero to two unique supporting moves.\n"
        f"choose from: {moves}.\n"
        "do not write the reply. do not output chain-of-thought, analysis, markdown, or extra keys.\n"
        'return exactly one json object: {"primary_move":"answer","supporting_moves":[],"response_goal":"...","confidence":"low|medium|high"}\n\n'
        "selection principles:\n"
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
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": envelope.text}]


def build_realization_messages(
    soul: str,
    history: tuple[dict[str, str], ...],
    envelope: InboundEnvelope,
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
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": envelope.text}]


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
