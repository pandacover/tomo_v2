# Conversation Architecture

## scope and architectural decisions

Tomo replies through a soul-aware conversational loop: interpret the situation, select an ordered move plan, realize that plan as natural Telegram utterances, and return compact metadata.

In scope:

- define the situation, move, move plan, utterance, and result domain language;
- mold every move's objective and procedure to `SOUL.md`;
- select one primary move and zero to two ordered supporting moves;
- generate 1-4 intentional utterances with at most three sentences each;
- keep move labels and reasoning internal while persisting compact move metadata;
- fall back to a safe direct-answer plan after malformed selection output;
- allow exactly one repair attempt after malformed realization output;
- preserve one logical assistant turn even when Telegram receives several bubbles.

`PersonalAgentRuntime` continues to own transport, current session loading and saving, and delivery. The conversation module has no connector, session-store, tool, or hosted-infrastructure dependencies.

Explicitly deferred:

- durable memory, retrieval, Dream, embeddings, and memory curation;
- session identity/schema changes, atomic writes, idempotent persistence, and delivery-retry duplication;
- tool execution, planning/execution loops, approvals, and action-result observation;
- group chat, `room_id`, other connectors, reactions, streaming, and typing refresh;
- Railway/Daytona deployment and immutable snapshot rollout, which require a separate user-approved operation.

## canonical terms

- conversation situation: the current inbound message plus recent history and Tomo's soul.
- conversational move: the social or cognitive purpose Tomo chooses next, not a wording template.
- primary move: the main purpose that must make the turn useful.
- supporting move: an optional ordered move that helps the primary move land naturally.
- move plan: the compact, non-chain-of-thought decision containing moves, goal, and confidence.
- utterance: one intentional piece of outward expression; Telegram normally maps one utterance to one bubble.
- conversation result: the move plan plus validated outward utterances.
- repair: one bounded attempt to fix invalid structured output, not a reflective agent loop.

## move catalog

| move | objective | procedure | completion |
|---|---|---|---|
| `acknowledge` | show precise attention without therapeutic warmth or fake excitement | identify what deserves recognition; react briefly; do not hijack the topic | the user can tell Tomo understood the important part |
| `answer` | give an honest, useful verdict rather than neutral assistant mush | answer first; support with strongest reasons; state uncertainty and practical consequence | the question is resolved as far as evidence allows |
| `clarify` | resolve material ambiguity without a customer-support interrogation | name what does not add up; explain why it matters; ask one sharp question | the minimum missing information has been requested |
| `explore` | follow an interesting unresolved thread with genuine curiosity | identify the loose end; connect context; ask one focused question and yield if declined | the user has a clear opening to deepen the thread |
| `challenge` | disagree plainly while staying accurate and useful | reconstruct the position; challenge the decision or reasoning, not vulnerability; offer a stronger route | the disagreement and better alternative are clear |
| `reassure` | reduce uncertainty with reality, not manufactured optimism | name the concern; separate danger from intensity; state what remains controllable | the user has a grounded picture and something useful to do |
| `joke` | build connection through observed absurdity or callbacks | find a real incongruity; keep it brief; return to substance | humor adds connection without replacing usefulness |
| `act` | move toward a real outcome without pretending work happened | confirm; identify prerequisites; execute only with capability and observed result | a real action is verified or the prerequisite is clear |
| `repair` | correct misunderstanding or bad tone without defensiveness | say `mb` when natural; identify the exact miss; correct it | shared understanding is restored |
| `refuse` | hold a real constraint without corporate policy speech | say no plainly; give the actual reason; offer the closest valid alternative | the limit and useful alternative are unambiguous |

## turn model

```text
InboundEnvelope
  -> load existing conversation context
  -> select MovePlan
      primary move
      optional supporting moves
      response goal
      confidence
  -> realize MovePlan using SOUL + history + current message
      1-4 intentional utterances
      <=3 sentences per utterance
  -> validate/repair response contract
  -> create Telegram bubbles
  -> persist one logical turn + compact move metadata
```

## invariants

1. every normal reply has exactly one primary conversational move.
2. a turn has at most two supporting moves, in order.
3. moves are internal decisions; users receive only natural utterances.
4. move plans contain no chain-of-thought.
5. one logical assistant turn may contain 1-4 physical telegram utterances.
6. each utterance has at most three sentences.
7. the full SOUL.md shapes both move selection and realization.
8. malformed planning output falls back safely; malformed realization gets one repair attempt.
9. no action is claimed without an observed result.
10. conversation architecture does not own sessions, memory, transport, or tools.
