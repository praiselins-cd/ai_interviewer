"""Splits an assessment-plan expected_answer into its concept clauses.

Must stay identical to split_expected_concepts in cd-ats-ai-service
(service/services/assessment_plan_validator.py), which the evaluator uses to
score each clause; any divergence means the interviewer follows up on
different concepts than the ones being scored."""


def split_expected_concepts(expected_answer: str | None) -> list[str]:
    concepts = []
    for piece in (expected_answer or "").split(";"):
        piece = piece.strip()
        if piece.endswith("."):
            piece = piece[:-1].strip()
        if piece:
            concepts.append(piece)
    return concepts
