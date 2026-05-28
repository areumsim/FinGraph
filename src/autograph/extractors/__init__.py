"""AutoGraph P3 추출 패키지.

- ``auto_relation_extractor.AutoRelationExtractor``  — BaseExtractor 구현체.
- ``chunk_selector.select_auto_chunks``               — 자동차 도메인 chunk 선별 SQL.
- ``staging_writer.upsert_staging``                   — auto.staging_relations 적재.
- ``cross_validate.run_p4``                            — P3 산출 → P2 비교 후 Neo4j 적재.
- ``run_p3.main``                                      — CLI 진입점.
"""
