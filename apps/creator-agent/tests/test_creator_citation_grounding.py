from __future__ import annotations

import unittest

from app.creator.agents.schemas import DraftCitationDocument, DraftDocument
from app.creator.agents.specialists import _ground_draft_citations


class CreatorCitationGroundingTests(unittest.TestCase):
    def test_keeps_only_grounded_unique_citations_and_trusts_source_metadata(
        self,
    ) -> None:
        document = DraftDocument(
            title="可靠性设计",
            body_markdown="Checkpoint 恢复计算状态，Outbox 负责可靠投递。",
            citations=(
                DraftCitationDocument(
                    claim_text="Checkpoint 恢复计算状态",
                    evidence_id="evidence-1",
                    source_title="模型伪造标题",
                    source_url="https://untrusted.example",
                ),
                DraftCitationDocument(
                    claim_text="Checkpoint 恢复计算状态",
                    evidence_id="evidence-1",
                ),
                DraftCitationDocument(
                    claim_text="正文中不存在的结论",
                    evidence_id="evidence-1",
                ),
                DraftCitationDocument(
                    claim_text="Outbox 负责可靠投递",
                    evidence_id="unknown",
                ),
            ),
        )

        grounded = _ground_draft_citations(
            document,
            (
                {
                    "evidence_id": "evidence-1",
                    "title": "可信恢复机制笔记",
                    "source_url": "https://trusted.example/recovery",
                },
            ),
        )

        self.assertEqual(len(grounded.citations), 1)
        citation = grounded.citations[0]
        self.assertEqual(citation.source_title, "可信恢复机制笔记")
        self.assertEqual(citation.source_url, "https://trusted.example/recovery")
