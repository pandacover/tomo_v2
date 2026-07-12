# Glossary

- **Segment**: one model-generation outcome.
- **SegmentFinish.PARTIAL**: a segment emitted at least one validated frame, then failed.
- **SegmentFinish.FAILED**: an attempted generation accepted no frame or tool batch before terminal contract, provider, or budget failure.
- **TurnRunStatus.COMPLETED_PARTIAL**: a terminal turn with at least one visible frame that ends at a partial, failed, or valid tool-batch knowledge boundary.
- **contract repair**: a bounded replacement attempt before any accepted visible frame or tool batch; it is not a segment.
