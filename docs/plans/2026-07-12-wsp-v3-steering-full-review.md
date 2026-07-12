# WSP 3.0 — Steering an Hermi (nach dem KOMPLETTEN Review, 35 S.)

*2026-07-12 · Robby → Hermi. Andy hat das ganze Review gelesen. Bestätigt + präzisiert die Re-Sequenzierung. Kernbotschaft des Reviews: 3.0 muss um **„source-only evidence preservation"** organisiert sein, nicht nur Schema-Vereinheitlichung. „Ohne diese Korrektur würde WSP 3.0 die heutigen Inkonsistenzen und Charter-Verletzungen formalisieren statt sie zu beseitigen."*

**Wichtige Änderung ggü. vorher:** das ist mehr als Re-Sequenzierung — es braucht ein kleines **M0-Contract-Amendment 002** (observations[] + policy_actions[] als Wire-Felder, Rename independence→diversity). M0/M1-Struktur bleibt, aber Amendment 002 zuerst mit Andy einfrieren.

---

```
Hermi — Robby hier. Andy hat das komplette Review (35 S.) gelesen. Stopp die reine M2-Weiterarbeit an der alten Sequenz; hier die vollständige, review-informierte Reihenfolge. M2 (typed errors/StateStore) läuft NACH WS-0 und wird Teil von WS-1.

WS-0 — CHARTER-PURGE (release-blocking). Dein Live-Diff hat den Scope bestätigt. Umsetzung mit 3 mechanischen Gate-Ebenen (Provider-Request / Observation / Public-Response):
- Provider-Descriptor-Gate: jeder Provider-Mode deklariert output_semantics = source_results | source_text. Modi die answer/synthesis/reasoning/claim/verification bewerben werden abgelehnt (Perplexity/Kilo/Exa-deep fallen durch).
- Outbound-Request-Gate: kein Provider-Request enthält chat-messages oder system-answer-instruction; Tavily include_answer=False; Linkup nur outputType=searchResults; Exa nie deep/deep-reasoning; Perplexity/Kilo failen vor Netzwerk außer echt source-only.
- Response-Allowlist: kanonische Objekte dürfen answer/full_synthesis/claim/verification/truth_confidence/type:synthesis NICHT führen; unbekannte Provider-Felder nicht auto-durchreichen (provider_fields nur descriptor-allowlisted + size-bounded, kein Raw-Payload-Escape).
- Single-Source-Content-Invariant (MECHANISCH, das Herzstück): jedes content-tragende Feld → content.provenance.observation_id muss existieren UND content.text = deterministische Transformation GENAU EINER Source-Observation. Erlaubt: newline/whitespace-norm, deterministische Truncation, mechanische Segmentierung, base64-Image-Replace. Verboten: mehrere Snippets kombinieren, umschreiben, zusammenfassen, Widersprüche auflösen, Claim erzeugen.
- Release-blocking Tests (Andy baut die Suite): Sentinel-Composition (Provider A snippet=SOURCE_A_ONLY, B=SOURCE_B_ONLY → KEIN Response-Feld enthält beide Tokens; fällt heute gegen Parallel/You); Capability-Registration (exakt search+extract, kein answer-Tool/-Feature, kein verify/watch, kein synthesis-Schema, Formatter kann kein "Answer:"); Legacy-Cache (Eintrag mit answer/type:synthesis/full_synthesis → legacy_hit, banned Felder droppen, valide Results re-normalisieren, Warnung LEGACY_FIELD_DROPPED, alte Datei byte-unverändert, Request failt wenn keine Source-only-Observation überlebt); Router/Kill-Switch (kein Routing/Shadow/Fallback/Kill-Switch-Pfad darf einen banned Mode wählen); Mechanical-Offset-Round-Trip (concat(segments.text)==canonical_text, sha256(canonical_text_utf8)==text.sha256).

WS-1 — LOSSLESS OBSERVATIONS (vor dem Serializer). Jede Provider-Ausgabe wird Observation VOR Dedup/Filter/Rerank/Truncation. Vollständiges Provider-Attempt-Log mit tries[] (jeder Retry ein eigener Try mit typed error + retry_after), endpoint_id, decision (attempted|skipped). SKIPPED-Provider bekommen AUCH Attempt-Records (decision=skipped + skip_reason) — sonst sind fallback_reason/cooldown aus dem Log nicht ableitbar. Non-destruktive Dedup-Cluster: alle Mitglieder erhalten (unterschiedliche titles/snippets/dates/scores/IDs).

M0-CONTRACT-AMENDMENT 002 (zuerst mit Andy einfrieren): ResponseV3 bekommt top-level observations[] (Evidenz getrennt von results[]) + policy_actions[] (excluded:spam_domain / reranked:intent_authority / demoted:domain_diversity / selected_as_representative / truncated_by_limit). source_independence_estimate → RENAME source_diversity (misst Vielfalt, NICHT Wahrheit/Independence/Original-Reporting); Skalar → 3.1, in 3.0 nur Komponenten (provider_count/host_count/source_family_count/unique_cluster_count/method/method_version/method_degraded). Scores typisiert (provider_score{value,semantics} + engine_rank), KEIN nackter cross-Provider-score. results[] = Consumer-Projektion mit representative_observation_id + observation_ids[] + dedup_cluster_id.

ROUTING/STATE: Routing-v2 + Fallback-v2 = legacy-autoritativ, neue Policies Shadow-only (Terminologie-Fix: es ist KEINE statische Kette). SQLite migriert provider_health.json UND provider_stats.json zusammen. Circuit-Key = (provider × capability × endpoint × credential-fingerprint = HMAC-SHA256(local_secret, credential), intern, nie public — nur opaque slot-ID nach außen). Fail-closed = bekannten auth/quota/config/provider_contract-Block nicht umgehen, Fallback auf anderen Provider erlaubt; SQLite-unavailable darf NIE WSP komplett abschießen.

CACHE v3: KEIN verbatim ResponseV3 cachen (enthält alte routing/attempts → Replay wäre Lüge). Cache normalisierte Observations + Origin-Metadaten (origin_execution_id, origin_provider, endpoint_id, normalizer_version, contract_version). Bei Hit NEUE ResponseV3 bauen: current execution_id + origin_execution_id, KEINE fake current provider_attempts, akkurates cache_age. Namespaced Keys, v2 read-only + sanitize (legacy byte-immutable), Fallback-Aliasing-Loch fixen (Lookup unter initialem, Write unter erfolgreichem Provider).

3.0-SCOPE (Review Final, 14): Charter-Purge · Canonical Request/Response · Lossless Observations · vollständiges Attempt-Log · typed errors · per-result Provenance · non-destruktive Dedup-Cluster · Search-Cache-v3 + Legacy-Sanitize · Routing-v2-Receipt · Shadow-Policy-Interface · 2-Level-Kill-Switch · minimaler SQLite Circuit+Adaptive-Store · internes Provider-Adapter-Protokoll · mechanische Text-Segmente NUR mit exakter Text-Identität.
3.1: kalibrierter source_diversity-Skalar · public Provider-SDK · Rich Budget/Credit-Preflight · Extraction-Caching · semantische Span-Extraction.
NIE im Engine-Kern: answer-synthesis · claim-generation · verification-judgments · watch-logic · orchestrator-behavior.

M0/M1-Struktur bleibt, Amendment 002 zuerst mit Andy einfrieren. Andy baut die release-blocking Charter-Gate-Suite. Kein Push/Go-Live ohne Robbys GO.
Report zurück: Purge-Diff + invertierter Contract-Test + Sentinel-Test grün, BEVOR der Engine-State weiterläuft.
```
