---
title: Design evidence
description: The published research behind LegalMemory's retrieval design (filtering before ranking, hybrid fusion, reranking, context-enriched chunking, controlled vocabularies, and authorization inside the query), with the open-source components it is assembled from.
---

Every retrieval decision in LegalMemory is a choice among published alternatives. This
page names the research each choice follows, the components it is built on, and the
places where the literature does not settle the question.

Sources are labelled by venue. Refereed conference and journal papers are marked as
such; preprints, standards documents and industry engineering reports are marked
separately and carry less weight, not none. Where the evidence is mixed, the
disagreement is stated rather than omitted.

## Filtering before ranking

LegalMemory compiles an access scope and metadata filters in SQL, then runs every
ranked leg inside that scope. It never retrieves a global nearest-neighbour set and
filters afterwards. See [How retrieval works](/concepts/retrieval/) for the mechanism.

This is the best-evidenced decision in the system: the database, systems, web and
machine-learning literatures converge on it.

| Source | Venue | What it establishes |
|---|---|---|
| Gollapudi et al., *Filtered-DiskANN: Graph Algorithms for Approximate Nearest Neighbor Search with Filters* | WWW 2023, pp. 3406–3416 · [doi](https://doi.org/10.1145/3543507.3583552) | Post-hoc filtering collapses as filters get selective: baselines "fail to achieve any meaningful accuracy, and have almost a 1000x lower QPS for the low specificity labels", while filter-aware indices hold 90%+ recall@10 down to 10⁻⁶ specificity. The mechanism: with a low-specificity filter "we may have to retrieve a very large number of candidates before coming across a single result matching the filter." |
| Patel et al., *ACORN: Performant and Predicate-Agnostic Search Over Vector Embeddings and Structured Data* | PACMMOD 2(3), Art. 120 / SIGMOD 2024 · [doi](https://doi.org/10.1145/3654923) | The complexity argument. Pre-filtering "always achieves perfect recall"; post-filtering over a graph index is efficient *only* when matching vectors are uniformly distributed, and degrades toward a full scan when the predicate is clustered, which a per-matter or per-client predicate always is. |
| Zhang et al., *VBASE: Unifying Online Vector Similarity Search and Relational Queries via Relaxed Monotonicity* | USENIX OSDI '23 · [link](https://www.usenix.org/conference/osdi23/presentation/zhang-qianxi) | Why an over-fetch multiplier cannot be chosen safely: systems that filter afterwards need "tentative indices … for a target vector's TopK nearest neighbors", which "leads to suboptimal performance due to the difficulty to predict the optimal K". |
| Li et al., *Attribute Filtering in Approximate Nearest Neighbor Search: An In-depth Experimental Study* | PACMMOD 3(6), Art. 298 · [doi](https://doi.org/10.1145/3769763) | Systematic comparison, 10 algorithms over 4 datasets with selectivity swept 0.1%–100%. Applying the predicate before distance computation "consistently handles 0.1% selectivity across all datasets", while graph-based post-filtering "often fail[s] under these conditions … due to the monotonic search behavior of HNSW". Its own honest conclusion: "No method performs reliably across all settings." |
| Chronis et al., *Filtered Vector Search: State-of-the-art and Research Opportunities* | PVLDB 18(12), pp. 5488–5492 (tutorial) · [doi](https://doi.org/10.14778/3750601.3750700) | "A vector search execution method tuned for unfiltered queries will fail to achieve high recall when filters are added." Introduces *stable recall* (consistent recall regardless of filter conditions) as the design goal. |
| Wu et al., *HQANN: Efficient and Robust Similarity Search for Hybrid Queries with Structured and Unstructured Constraints* | CIKM 2022, pp. 4580–4584 · [doi](https://doi.org/10.1145/3511808.3557610) | Performance is "hardly affected by the complexity of attributes" when filtering is fused into search: evidence that a rich metadata schema (matter, practice area, document type, date, status) costs nothing in retrieval quality if it is applied inside the query. |
| Engels et al., *Approximate Nearest Neighbor Search with Window Filters* | ICML 2024, PMLR 235:12469–12490 · [link](https://proceedings.mlr.press/v235/engels24a.html) | Formalises range-restricted ANN, motivated by "image and document search with timestamp filters", the basis for date- and effective-period-scoped retrieval. |

## Authorization inside the query, never after

The permission compiler runs before any lexical or vector score is computed, and every
returned row is re-verified against SQL. This is a correctness requirement before it is
a performance one.

| Source | Venue | What it establishes |
|---|---|---|
| Büttcher & Clarke, *A Security Model for Full-Text File System Search in Multi-User Environments* | USENIX FAST '05 · [link](https://www.usenix.org/conference/fast-05/security-model-full-text-file-system-search-multi-user-environments) | The canonical treatment of late-binding security trimming. Ranking over the full corpus with global term statistics and removing forbidden files afterwards lets a user *infer* the content of files they cannot read; the leak survives the filter, because the scores were computed from documents the user was never entitled to see. |
| Ferraiolo, Sandhu, Gavrila, Kuhn & Chandramouli, *Proposed NIST Standard for Role-Based Access Control* | ACM TISSEC 4(3), pp. 224–274 · [doi](https://doi.org/10.1145/501978.501980) | The reference model behind principal- and role-based grants. |
| Hu et al., *Guide to Attribute Based Access Control (ABAC) Definition and Considerations* | NIST SP 800-162 (standards publication) · [doi](https://doi.org/10.6028/NIST.SP.800-162) | Attribute-based authorization, the model matching mirrored source ACLs combined with local project and document grants. |
| Rose, Borchert, Mitchell & Connelly, *Zero Trust Architecture* | NIST SP 800-207 (standards publication) · [doi](https://doi.org/10.6028/NIST.SP.800-207) | Per-request authorization decisions rather than perimeter trust, the model for identity-bound MCP tool calls. |

Why the index must not become a second copy of the corpus with weaker permissions:

| Source | Venue | What it establishes |
|---|---|---|
| Song & Raghunathan, *Information Leakage in Embedding Models* | ACM CCS 2020, pp. 377–390 · [doi](https://doi.org/10.1145/3372297.3417270) | Embeddings are not anonymised text. Inversion attacks recover 50–70% of input words from popular sentence embeddings, and embeddings leak sensitive attributes independent of the semantic task. |
| Zeng et al., *The Good and The Bad: Exploring Privacy Issues in Retrieval-Augmented Generation (RAG)* | Findings of ACL 2024, pp. 4505–4524 · [link](https://aclanthology.org/2024.findings-acl.267/) | The retrieval datastore is extractable through the generation interface: of 250 untargeted prompts, 116 produced output exactly matching retrieved content. Retrieval scope is therefore a confidentiality boundary, not only a relevance one. |

## Hybrid retrieval and rank fusion

LegalMemory fuses lexical, dense and identifier legs with reciprocal rank fusion rather
than searching a single vector index.

| Source | Venue | What it establishes |
|---|---|---|
| Cormack, Clarke & Büttcher, *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods* | SIGIR 2009, pp. 758–759 · [doi](https://doi.org/10.1145/1571941.1572114) | Defines RRF and the constant `k = 60` this system uses. Notably, MAP was flat across k = 10–100 in the original pilot, so the constant is not a sensitive tuning knob. |
| Thakur et al., *BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models* | NeurIPS 2021 Datasets & Benchmarks · [link](https://datasets-benchmarks-proceedings.neurips.cc/paper/2021/hash/65b9eea6e1cc6bb9f0cd2a47751a186f-Abstract-round2.html) | The case against dense-only retrieval out of domain. Averaged over 18 datasets, *every* dense retriever tested scored below BM25 (DPR −47.7%, ANCE −7.4%, TAS-B −2.8%); BM25 with a cross-encoder reranker was best overall at +11% nDCG@10, winning on 16 of 18. A firm's corpus is by definition out of domain for any public model. |
| Robertson & Zaragoza, *The Probabilistic Relevance Framework: BM25 and Beyond* | Foundations and Trends in IR 3(4), pp. 333–389 · [doi](https://doi.org/10.1561/1500000019) | The derivation of BM25, the lexical leg's scoring function and OpenSearch's default. |
| Wang, Zhuang & Zuccon, *BERT-based Dense Retrievers Require Interpolation with BM25 for Effective Passage Retrieval* | ICTIR 2021 · [doi](https://doi.org/10.1145/3471158.3472233) | Lexical and dense signals are complementary, not redundant: even an untuned 50/50 interpolation lifts TREC DL 2019 nDCG@10 from 0.6100 to 0.6787. |
| Luan, Eisenstein, Toutanova & Collins, *Sparse, Dense, and Attentional Representations for Text Retrieval* | TACL 9, pp. 329–345 · [link](https://aclanthology.org/2021.tacl-1.20/) | The theoretical limit: a fixed-length dense vector has bounded capacity, so exact terms (case numbers, defined terms, party names) are precisely what it loses. This is why an exact-identifier leg exists alongside the dense one. |
| Chen, Zhang, Lu, Bendersky & Najork, *Out-of-Domain Semantics to the Rescue! Zero-Shot Hybrid Retrieval Models* | ECIR 2022, pp. 95–110 · [doi](https://doi.org/10.1007/978-3-030-99736-6_7) | Hybrid models generalise to unseen domains better than either component alone. |

**Where the evidence does not settle it.** Bruch, Gai & Ingber, *An Analysis of Fusion
Functions for Hybrid Retrieval* (ACM TOIS 42(1), Art. 20 ·
[doi](https://doi.org/10.1145/3596512)) find that a *tuned* convex combination of
normalised scores significantly outperforms RRF on every dataset they test. RRF's
advantage is that it needs no score normalisation and no per-corpus tuning, which is the
right default for an appliance that is installed rather than tuned per deployment, but
a deployment willing to tune has published reason to expect better than RRF. The fusion
weights are configurable for this reason.

## Reranking

Reranking the fused candidate set is available and off by default.

| Source | Venue | What it establishes |
|---|---|---|
| Wang, Lin & Metzler, *A Cascade Ranking Model for Efficient Ranked Retrieval* | SIGIR 2011, pp. 105–114 · [doi](https://doi.org/10.1145/2009916.2009934) | Formalises the multi-stage cascade (cheap retrieval over everything, expensive scoring over a small candidate set) and shows it matches or beats a monolithic ranker at roughly half the cost. |
| Sun et al., *Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents* | EMNLP 2023 · [link](https://aclanthology.org/2023.emnlp-main.923/) | Listwise LLM reranking of a BM25 top-100: on TREC DL19 nDCG@10 rises from 50.58 (BM25) to 75.59. This is the shape of the optional rerank stage. |
| Nogueira, Jiang, Pradeep & Lin, *Document Ranking with a Pretrained Sequence-to-Sequence Model* | Findings of EMNLP 2020, pp. 708–718 · [link](https://aclanthology.org/2020.findings-emnlp.63/) | monoT5: sequence-to-sequence rerankers, and their advantage in low-data regimes. |
| Zhuang et al., *RankT5: Fine-Tuning T5 for Text Ranking with Ranking Losses* | SIGIR 2023, pp. 2308–2313 · [doi](https://doi.org/10.1145/3539618.3592047) | Optimising ranking losses directly, with better zero-shot out-of-domain behaviour. |

Reranking is off by default because it adds a synchronous model call to the query path.
The evidence supports the gain; the default reflects latency and cost, not doubt.

## Chunking and context

Chunks are embedded with a context header naming the document title, type and matter,
while the stored and displayed text stays raw.

| Source | Venue | What it establishes |
|---|---|---|
| Dai & Callan, *Deeper Text Understanding for IR with Contextual Neural Language Modeling* | SIGIR 2019, pp. 985–988 · [doi](https://doi.org/10.1145/3331184.3331303) | Retrieval improves when passage representations carry document context rather than being scored in isolation. |
| Chen et al., *Dense X Retrieval: What Retrieval Granularity Should We Use?* | EMNLP 2024, pp. 15159–15177 · [link](https://aclanthology.org/2024.emnlp-main.845/) | Retrieval granularity is itself a design variable with large effects: self-contained units beat passage-level indexing by +12.0 Recall@5 for one retriever, +9.3 for another. |
| Anthropic, *Introducing Contextual Retrieval* (industry report, **not peer-reviewed**) · [link](https://www.anthropic.com/engineering/contextual-retrieval) | n/a | Prepending a document-level context blurb before embedding cut top-20 retrieval failures from 5.7% to 3.7%; adding lexical retrieval took it to 2.9%, and a reranker to 1.9%. Reported as an engineering result, not a controlled study, but it measures precisely this system's arrangement of contextual embeddings + BM25 + optional rerank. |
| Günther et al., *Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models* (preprint, **not peer-reviewed**) · [link](https://arxiv.org/abs/2409.04701) | n/a | An alternative route to the same goal: encode the document first, pool per chunk afterwards. A known option, not the implemented one. |

## Controlled vocabulary and pluggable ontologies

Document types, practice areas and matter kinds are assigned from an ontology artifact
rather than free text. The shipped artifact aligns with SALI LMSS, and the ontology is
**data, not code**; see [Ontology](/product/ontology/). Any OWL/SKOS-shaped taxonomy can
be uploaded in its place, and nodes can be disabled to scope it to a firm's practice.

| Source | Venue | What it establishes |
|---|---|---|
| Furnas, Landauer, Gomez & Dumais, *The vocabulary problem in human-system communication* | Communications of the ACM 30(11), pp. 964–971 · [doi](https://doi.org/10.1145/32206.32212) | The foundational result: two people choose the same term for the same object with probability below 0.20, so access through one person's preferred word "will result in 80-90 percent failure rates in many common situations". The argument for a controlled vocabulary in one sentence, from 1987. |
| Gross & Taylor, *What Have We Got to Lose? The Effect of Controlled Vocabulary on Keyword Searching Results* | College & Research Libraries 66(3), pp. 212–230 · [doi](https://doi.org/10.5860/crl.66.3.212) | Measures what is lost without controlled subject headings: a large share of relevant results are found only through assigned vocabulary, not through words occurring in the text. |
| Ma et al., *Incorporating Structural Information into Legal Case Retrieval* | ACM TOIS 42(2) · [doi](https://doi.org/10.1145/3609796) | Legal relevance differs from general-domain relevance, and modelling document structure improves retrieval accordingly. |
| Li et al., *SAILER: Structure-aware Pre-trained Language Model for Legal Case Retrieval* | SIGIR 2023, pp. 1035–1044 · [doi](https://doi.org/10.1145/3539618.3591761) | Structure-aware legal retrieval outperforms structure-blind baselines zero-shot (nDCG@10 0.7979 vs 0.7115 for BM25 on LeCaRD). |

Interoperable legal vocabularies and standards this design is compatible with:

- [**SALI LMSS**](https://github.com/sali-legal/LMSS): the Legal Matter Standard
  Specification, an industry taxonomy of over 18,000 tags, each with a stable IRI.
  Carried as an optional `sali_iri` annotation on practice area, matter kind, document
  type and party roles, so local labels stay firm-specific while remaining mappable.
- **LKIF-Core**: Hoekstra, Breuker, Di Bello & Boer, *The LKIF Core Ontology of Basic
  Legal Concepts*, LOAIT 2007, CEUR-WS Vol-321, pp. 43–63
  ([link](https://ceur-ws.org/Vol-321/)). An OWL-DL core ontology of basic legal concepts
  from the EU ESTRELLA project.
- **LegalRuleML**: Athan, Boley, Governatori, Palmirani, Paschke & Wyner, *OASIS
  LegalRuleML*, ICAIL 2013, pp. 3–12
  ([doi](https://doi.org/10.1145/2514601.2514603)). A rule interchange language
  expressing legal sources, time, defeasibility and deontic operators.
- **ELI**: *Council conclusions inviting the introduction of the European Legislation
  Identifier*, OJ C 325, 26.10.2012
  ([link](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52012XG1026(01))).
  A URI template and metadata vocabulary for identifying legislation across EU member
  states.
- [**CUAD**](https://www.atticusprojectai.org/cuad): Hendrycks, Burns, Chen & Ball,
  NeurIPS 2021 Datasets & Benchmarks
  ([link](https://datasets-benchmarks-proceedings.neurips.cc/paper/2021/hash/6ea9ab1baa0efb9e19094440c317e21b-Abstract-round1.html)).
  The clause-type vocabulary maps all 41 public benchmark labels, keeping public
  evaluation data usable against a firm's own taxonomy.

Relation traversal across documents follows the graph-augmented retrieval line: Edge et
al., *From Local to Global: A Graph RAG Approach to Query-Focused Summarization*
(preprint, **not peer-reviewed**, [arXiv:2404.16130](https://arxiv.org/abs/2404.16130)),
and Sarmah et al., *HybridRAG*, ICAIF '24, pp. 608–616
([doi](https://doi.org/10.1145/3677052.3698671)), which reports that combining graph and
vector retrieval outperforms either alone on domain documents.

## Grounded answers and measured evaluation

Every retrieval result carries provenance to a source object and version, and retrieval
changes are gated by a frozen benchmark rather than by impression; see
[Benchmarks](/development/benchmarks/).

| Source | Venue | What it establishes |
|---|---|---|
| Dahl, Magesh, Suzgun & Ho, *Large Legal Fictions: Profiling Legal Hallucinations in Large Language Models* | Journal of Legal Analysis 16(1), pp. 64–93 · [doi](https://doi.org/10.1093/jla/laae003) | Asked specific, verifiable questions about US federal cases, public models hallucinate between 58% and 88% of the time, and rates rise for lower-profile courts. Ungrounded generation is not a viable basis for legal work. |
| Magesh, Surani, Dahl, Suzgun, Manning & Ho, *Hallucination-Free? Assessing the Reliability of Leading AI Legal Research Tools* | Journal of Empirical Legal Studies 22(2), pp. 216–242 · [doi](https://doi.org/10.1111/jels.12413) | A preregistered evaluation of retrieval-augmented legal research systems measures hallucination rates between 17% and 33%. Retrieval augmentation reduces the problem; it does not remove it. Hence citations that resolve to a specific version, and measured evaluation rather than asserted accuracy. |
| Guha et al., *LegalBench: A Collaboratively Built Benchmark for Measuring Legal Reasoning in Large Language Models* | NeurIPS 2023 Datasets & Benchmarks · [link](https://proceedings.neurips.cc/paper_files/paper/2023/hash/89e44582fd28ddfea1ea4dcb0ebbf4b0-Abstract-Datasets_and_Benchmarks.html) | A collaboratively built benchmark of legal reasoning tasks. |
| Pipitone & Houir Alami, *LegalBench-RAG: A Benchmark for Retrieval-Augmented Generation in the Legal Domain* (preprint, **not peer-reviewed**) · [link](https://arxiv.org/abs/2408.10343) | n/a | The first public benchmark to evaluate the *retrieval* step of legal RAG rather than only generation: 6,858 annotated query/answer pairs with gold answers as character-level spans. The design principle this system's benchmark follows: score retrieval directly, against frozen labels. |
| Goebel, Kano, Kim, Rabelo, Satoh & Yoshioka, *COLIEE* competition overviews | The Review of Socionetwork Strategies | The long-running legal information extraction and entailment competition series, the standing shared-task reference for legal retrieval evaluation. |

## The components this is assembled from

LegalMemory composes established open-source projects rather than reimplementing them.
Each runs as its own container; see [Architecture](/concepts/architecture/) and
[Deployment](/operations/deployment/).

| Component | Licence | Role here | Provenance |
|---|---|---|---|
| [OpenSearch](https://opensearch.org/) | Apache-2.0 | The chunk index: BM25 lexical scoring and approximate kNN vector search in one `_msearch` request, with the compiled ACL filter applied inside every leg | Vector search uses HNSW: Malkov & Yashunin, *Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs*, IEEE TPAMI 42(4), pp. 824–836 ([doi](https://doi.org/10.1109/TPAMI.2018.2889473)). Lexical scoring is BM25 (Robertson & Zaragoza, above) |
| [Docling / Docling Serve](https://github.com/docling-project/docling) | MIT | Document conversion, layout and table structure, German and English OCR | Auer et al., *Docling Technical Report*, IBM Research ([arXiv:2408.09869](https://arxiv.org/abs/2408.09869), preprint). Built on two refereed models: DocLayNet for layout (Pfitzmann et al., KDD 2022, pp. 3743–3751, [doi](https://doi.org/10.1145/3534678.3539043)) and TableFormer for table structure (Nassar et al., CVPR 2022, pp. 4614–4623, [link](https://arxiv.org/abs/2203.01017)) |
| [PostgreSQL](https://www.postgresql.org/) + [pgvector](https://github.com/pgvector/pgvector) | PostgreSQL licence / PostgreSQL licence | System of record for documents, versions, matters, relations and grants; the authority for every authorization decision | Long-standing relational engine; pgvector adds vector types and index methods |
| [LiteLLM](https://github.com/BerriAI/litellm) | MIT (core) | The model gateway every LLM and embedding call resolves through, so a deployment can point at hosted or on-premises models without touching pipeline code | n/a |
| [Hatchet](https://github.com/hatchet-dev/hatchet) | MIT | Durable pipeline orchestration on Postgres: claims, retries and resumption after a worker crash | n/a |
| [Keycloak](https://www.keycloak.org/) + [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) | Apache-2.0 / MIT | Identity: OIDC sign-in for the console, and the token issuer for MCP clients | Standards: OAuth 2.1 (draft), [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) protected resource metadata, [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) resource indicators, [RFC 7519](https://www.rfc-editor.org/rfc/rfc7519) JWT |
| [FastMCP](https://github.com/jlowin/fastmcp) | Apache-2.0 | The MCP server exposing identity-bound retrieval tools | Implements the [Model Context Protocol](https://modelcontextprotocol.io/) open specification |
| [Langfuse](https://github.com/langfuse/langfuse) | MIT (core) | Model-call tracing, so every pipeline stage's prompts and costs are inspectable | n/a |

## What is not claimed

- **Rerank defaults.** The gains above are measured on public benchmarks with public
  models. This system ships reranking off; whether it pays for its latency on a given
  firm's corpus is a question for that firm's own benchmark run.
- **Fusion weights.** RRF is the default for its tuning-free behaviour, with the
  contrary evidence noted above.
- **Relation and version inference.** Draft→final chains, redline links and typed
  relations are inferred by a model reading folder context and document content. That
  design follows from the domain, not from a published result; it is evaluated by this
  system's own benchmark rather than by an external one.
- **Numbers quoted here are the sources' own**, measured on their datasets, not
  measurements of this system. LegalMemory's own retrieval quality is whatever its
  [benchmark](/development/benchmarks/) reports on a given corpus.
