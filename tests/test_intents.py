"""Query-intent detector: examples per intent, precision guards, cue coverage, speed."""

import dataclasses
import random
import re
import time
from pathlib import Path

import pytest

from wsp_core import intents
from wsp_core.intents import INTENTS, IntentDecision, classify_intent

# Clear examples written for these tests (none is taken from the benchmark files).
CLEAR = {
    "academic": [
        "systematic review of sleep interventions in adolescents",
        "Metaanalyse zur Wirksamkeit von Akupunktur bei Rückenschmerzen",
        "revue systématique sur l'efficacité de la méditation",
        "ensayo aleatorizado doble ciego sobre vitamina D",
        "revisione sistematica e metanalisi sul digiuno intermittente",
        "papers on graph neural networks for molecule generation",
        "doi 10.1016/j.cell.2020.01.001",
        "Studie zu Mikroplastik im Trinkwasser",
        "preprint on quantum error correction",
    ],
    "docs": [
        "python asyncio gather documentation",
        "Dokumentation zur REST API Authentifizierung",
        "how to parse json in javascript",
        "git rebase --onto explained",
        "TypeError: cannot read properties of undefined",
        "documentation officielle de PostgreSQL index partiels",
        "cómo instalar pandas con pip",
        "documentazione ufficiale di Kubernetes ConfigMap",
        "site:docs.example.org cache control headers",
        "os.path.join() with absolute paths",
    ],
    "security": [
        "CVE-2023-4863 heap overflow details",
        "Sicherheitslücke im Router Firmware Patch verfügbar",
        "faille de sécurité critique zero-day navigateur",
        "vulnerabilidad crítica en el navegador parche",
        "attacco ransomware ospedale dati rubati",
        "ransomware attack on a hospital network",
        "sql injection prevention cheat sheet",
    ],
    "shopping": [
        "best wireless earbuds under $100",
        "Waschmaschine kaufen günstig Preisvergleich",
        "meilleur aspirateur robot pas cher prix",
        "comprar portátil barato oferta",
        "miglior smartphone sotto i 400 euro",
        "buy standing desk online free shipping",
        "best noise cancelling headphones",
    ],
    "local": [
        "restaurants near me open now",
        "Öffnungszeiten Apotheke Hauptplatz",
        "pharmacie ouverte dimanche près de chez moi",
        "tiempo mañana lluvia pronóstico",
        "cosa fare stasera concerti eventi",
        "weather tomorrow rain forecast",
    ],
    "community": [
        "reddit best budget mechanical keyboard",
        "Erfahrungen mit Solaranlage Forum",
        "has anyone tried self hosting this",
        "opiniones sobre estufas de pellets foro",
        "avis forum fibre optique problèmes",
        "discussione forum auto elettriche pro e contro",
        "r/gardening composting discussion",
    ],
    "news": [
        "breaking news earthquake today",
        "latest announcement from the central bank this week",
        "Spieltag Ergebnisse heute Tabelle",
        "dernières nouvelles tremblement de terre aujourd'hui",
        "noticias de hoy última hora",
        "ultime notizie oggi",
        "quarterly results revenue guidance announced",
    ],
}

# Ambiguous or cue-free queries, and look-alikes of the precise labels: all general.
AMBIGUOUS = [
    "how does photosynthesis work",
    "capital of Australia",
    "wie funktioniert eine Wärmepumpe",
    "qu'est-ce que l'inflation",
    "historia del imperio romano",
    "ricetta lasagne al forno",
    "meilleur modèle open source 2026 benchmark français",
    "python snake size",
    "java coffee beans origin",
    "price of gold per ounce",
    "price elasticity of demand explained",
    "toilet paper brands",
    "case study marketing example",
    "journal writing prompts for beginners",
    "great deal of effort meaning",
    "paper airplane designs",
    "study tips for exams",
    "movie review of a classic western",
    "how to write a thesis statement",
    "Studienplatz Medizin Wartezeit",
    "patente de conducir renovar",
    "survey questions for customer satisfaction",
    "install windows on an old pc",
    "bitcoin price chart",
]


@pytest.mark.parametrize("intent,query", [(i, q) for i, qs in CLEAR.items() for q in qs])
def test_clear_examples(intent, query):
    decision = classify_intent(query)
    assert decision.intent == intent, (decision, query)
    assert decision.signals and all(s.startswith(f"{intent}:") for s in decision.signals)


@pytest.mark.parametrize("query", AMBIGUOUS)
def test_ambiguous_queries_fall_back_to_general(query):
    decision = classify_intent(query)
    assert decision.intent == "general", (decision, query)


def test_every_intent_has_clear_examples():
    assert set(CLEAR) | {"general"} == set(INTENTS)


def test_result_shape():
    decision = classify_intent("python asyncio documentation")
    assert isinstance(decision, IntentDecision)
    assert decision.intent in INTENTS
    assert 0.0 <= decision.confidence <= 1.0
    assert isinstance(decision.signals, tuple) and all(isinstance(s, str) for s in decision.signals)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.intent = "news"  # type: ignore[misc]


def test_confidence_stays_in_range_and_grows_with_evidence():
    queries = [q for qs in CLEAR.values() for q in qs] + AMBIGUOUS + ["", "x", "?!", "a" * 500]
    for query in queries:
        assert 0.0 <= classify_intent(query).confidence <= 1.0
    weak = classify_intent("public exploit")
    strong = classify_intent("CVE-2024-1234 public exploit zero-day")
    assert weak.intent == strong.intent == "security"
    assert strong.confidence > weak.confidence
    assert classify_intent("how does photosynthesis work").confidence > classify_intent(
        "python snake size"
    ).confidence


def test_blank_and_odd_input_is_general():
    for query in ("", "   ", "\n\t", None, 42, b"bytes", ["list"]):
        decision = classify_intent(query)  # type: ignore[arg-type]
        assert decision.intent == "general" and decision.confidence == 0.0 and decision.signals == ()
    for query in ("?!?!", "🙂🙂🙂", "最新 オープンソース ニュース", "…", "a" * 5000, "\x00\x01"):
        assert classify_intent(query).intent in INTENTS


def test_deterministic():
    query = "Sony-style model price specs"
    assert len({classify_intent(query) for _ in range(20)}) == 1


# --- precision rules ---------------------------------------------------------

def test_forum_request_beats_best_under_price():
    decision = classify_intent("best dac under 300 euros forum")
    assert decision.intent == "community"


def test_precise_labels_need_a_margin_over_competing_intents():
    # docs: 3.5 (docs) against security: 3.5 (cve) is a tie, so the precise label is withheld.
    decision = classify_intent("cve docs")
    assert decision.intent != "docs"


def test_precise_labels_need_one_substantial_cue():
    # Only weak cues (example, install, vs, review, dataset, survey): no label.
    for query in ("example install tutorial", "dataset survey research abstract", "review vs spec"):
        assert classify_intent(query).intent == "general", query


def test_weak_cues_still_help_next_to_a_strong_one():
    assert classify_intent("python install example").intent == "docs"
    assert classify_intent("price spec model xm100 review").intent == "shopping"
    assert classify_intent("wissenschaftliche Studie Auswirkungen Ernährung").intent == "academic"


def test_source_type_platforms_are_community():
    assert classify_intent("site:reddit.com/r/books best fantasy").intent == "community"
    assert classify_intent("site:news.example.org show hn something").intent == "community"


def test_dotted_framework_names_are_not_also_file_names_or_code_tokens():
    # "node.js" is already a language cue; it must not also count as a file name and a dotted identifier.
    assert classify_intent("what are the differences between python and node.js").intent == "general"
    assert classify_intent("edit server.js and config.json").intent == "docs"
    assert classify_intent("node.js fs.promises readfile example").intent == "docs"


def test_news_question_is_not_a_domain_news_cue():
    # "news." inside a host name must not count as a news word.
    assert classify_intent("site:news.example.org terminal tool").intent == "general"


# --- no named entities, no fixed years ---------------------------------------

FORBIDDEN = [
    "graz", "sturm", "lask", "salzburg", "nvidia", "openssl", "hifi",
    "amazon", "ebay", "google", "apple", "samsung", "sony", "openai", "anthropic", "claude",
    "mistral", "tsmc", "asml", "vienna", "wien", "berlin",
]


def test_module_source_has_no_named_entity_cues():
    source = Path(intents.__file__).read_text(encoding="utf-8").lower()
    found = [word for word in FORBIDDEN if word in source]
    assert not found, f"named entities in wsp_core/intents.py: {found}"


def test_module_source_has_no_hard_coded_years():
    source = Path(intents.__file__).read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.-])(?:19|20)\d\d(?![\w.-])", source)


# --- cue coverage and the prefix index ---------------------------------------

# One query per cue. The cue must fire on it; a new cue needs a new entry.
CUE_EXAMPLES = {
    "preprint_repo": "arxiv transformer survey",
    "doi": "find paper by doi 10.1145/3292500.3330701",
    "peer_review": "peer-reviewed sources only",
    "meta_analysis": "meta-analysis of exercise trials",
    "systematic_review": "a systematic review of sleep interventions",
    "literature_review": "literature review on remote teaching",
    "trial_design": "double-blind randomized trial",
    "research_paper": "recent research paper on batteries",
    "paper": "papers on graph theory",
    "scholar": "scholarly articles about migration",
    "patent": "patent for solid state battery",
    "proceedings": "conference proceedings robotics",
    "impact_index": "journal impact factor ranking",
    "journal_of": "Journal of Applied Physics",
    "journal": "journal articles on urban planning",
    "citation": "citation analysis of cited works",
    "thesis": "phd thesis on soil erosion",
    "math_proof": "proof of the four colour theorem",
    "scientific": "scientific evidence for acupuncture",
    "study_on": "a study on childhood obesity",
    "study": "studies about caffeine and memory",
    "effects_of": "the effects of noise on sleep",
    "abstract": "abstract only",
    "method_terms": "statistically significant p-value",
    "dataset": "open dataset of satellite images",
    "survey_on": "a survey on federated learning",
    "survey": "survey of methods",
    "research": "research on bees",
    "site_docs": "site:docs.example.org caching headers",
    "how_to_lang": "how do I sort a list in python",
    "error_phrase": "bash: foo: command not found",
    "code_syntax": "def parse(x) python function",
    "code_include": "#include <stdio.h> hello",
    "git_cmd": "git rebase interactive squash",
    "docs_word": "official docs for the client",
    "documentation_word": "installation documentation for the library",
    "reference_tech": "sdk reference for the client",
    "sdk": "mobile sdk integration",
    "changelog": "package changelog 2.0",
    "readme": "readme badges markdown",
    "cheat_sheet": "vim cheat sheet",
    "pkg_cmd": "pip install requests",
    "code_call": "os.path.join() example",
    "code_assign": "set max_connections=100 in config",
    "code_flag": "ls --all option",
    "code_backtick": "what does `chmod 755` do",
    "code_snake": "read_csv parameters",
    "code_camel": "useEffect cleanup",
    "error_class": "ValueError when parsing",
    "error_code": "error 404 on request",
    "api": "weather api rate limit",
    "cli": "cli arguments",
    "shell_cmd": "sudo systemctl restart service",
    "file_ext": "edit settings.json file",
    "code_dotted": "np.array reshape",
    "breaking_changes": "breaking changes in version 3",
    "hex_literal": "0xdeadbeef meaning",
    "lang_clear": "python slicing",
    "lang_symbols": "c++ templates",
    "lang_tools": "docker networking",
    "lang_data": "json formatting",
    "lang_ambiguous": "java streams",
    "tutorial": "beginner tutorial",
    "guide_words": "migration guide best practices",
    "manual": "user manual",
    "snippet": "code snippet",
    "example": "usage example",
    "install_setup": "setup instructions",
    "tech_terms": "endpoint middleware schema",
    "trouble_words": "bug crashes",
    "library_words": "package module",
    "reference_plain": "reference table",
    "release_notes_doc": "release notes",
    "open_source": "open source tools",
    "alternatives": "alternatives to the tool",
    "self_hosted": "self-hosted setup",
    "cve_id": "CVE-2024-3094 details",
    "cve": "recent cves list",
    "zero_day": "zero-day exploit",
    "vulnerability": "critical vulnerability disclosed",
    "exploit": "public exploit code",
    "threat": "ransomware gang",
    "advisory_sec": "security advisory published",
    "advisory": "advisory for operators",
    "attack": "supply chain attack",
    "vuln_class": "sql injection payloads",
    "cyber_words": "cybersecurity basics",
    "hacker": "hackers claim",
    "patch": "patch available",
    "mitigation": "mitigation steps",
    "security_plain": "security settings",
    "near_me": "coffee near me",
    "hours": "opening hours sunday",
    "weather": "weather today",
    "forecast": "forecast for the weekend",
    "precipitation": "rain expected",
    "tonight": "plans for tonight",
    "poi": "pharmacy open",
    "events": "events downtown",
    "culture": "culture program",
    "address": "address of the museum",
    "contact": "Kontakt Anfahrt",
    "platform": "r/gardening composting",
    "forum": "forum thread tips",
    "community_word": "community recommendations",
    "anyone": "has anyone tried this",
    "discussion": "discussion about pricing",
    "opinions": "opinions on the new model",
    "avis": "avis des utilisateurs",
    "experiences": "Erfahrungen mit dem Anbieter",
    "worth_it": "is it worth it",
    "should_i": "should I buy now",
    "long_term": "long-term review after two years",
    "real_users": "real users report",
    "recommend": "recommendations please",
    "advice": "need advice",
    "thread": "thread about it",
    "news_word": "news about the merger",
    "breaking": "breaking story",
    "what_happened": "what happened last night",
    "earnings": "earnings call",
    "investor_relations": "investor relations page",
    "markets": "stock market moves",
    "fin_metric": "gross margin",
    "fiscal_period": "fiscal year results",
    "periodic_report": "monthly revenue",
    "announce": "announced a partnership",
    "release_notes": "release notes for the update",
    "release_word": "launch event",
    "this_period": "this week",
    "today": "today in history",
    "latest": "latest version",
    "blog": "official blog post",
    "sports_terms": "league table standings",
    "sports_weak": "scores",
    "month_year": "march 2031 report",
    "year": "events of 2030",
    "buy": "where to buy it",
    "purchase": "purchase online",
    "price": "price list",
    "price_compare": "Preisvergleich online",
    "best_under": "best tablet under 300 dollars",
    "under_amount": "something under $50",
    "currency_amount": "costs €120",
    "deals": "weekend deals",
    "deal_sale": "on sale now",
    "cheap": "cheap and cheerful",
    "budget": "budget option",
    "shipping": "free shipping",
    "retail": "online shop",
    "shop_word": "shopping list",
    "review_phrase": "Test und Erfahrungen",
    "review_word": "detailed review",
    "test_word": "im Test",
    "versus": "a vs b",
    "model_token": "wh-1000xm5 headphones",
    "model_variant": "model 15 pro",
    "spec_word": "full specs",
    "best_category": "best espresso machine",
    "category": "oled tv",
}


def _fired(query):
    raw = query[: intents._MAX_QUERY_CHARS]
    text = intents._normalize(raw)
    _, _, fired = intents._evaluate(intents._candidates(text), text, raw)
    return fired


def test_every_cue_has_an_example_and_fires_on_it():
    names = {spec.name: spec for spec in intents._SPECS}
    assert len(names) == len(intents._SPECS), "cue names must be unique"
    assert set(CUE_EXAMPLES) == set(names)
    for name, query in CUE_EXAMPLES.items():
        assert name in _fired(query).get(names[name].intent, []), (name, query)


def test_cue_table_is_well_formed():
    for spec in intents._SPECS:
        assert spec.intent in INTENTS and spec.intent != "general"
        assert spec.weight > 0
        assert re.compile(spec.pattern).groups == 0, spec.name
    for (_, rx) in intents._UNITS:
        assert rx.groups == 0


def test_prefix_index_never_hides_a_match():
    rng = random.Random(7)
    pool = list(CUE_EXAMPLES.values()) + [q for qs in CLEAR.values() for q in qs] + AMBIGUOUS
    queries = list(pool)
    for _ in range(600):
        queries.append(" ".join(rng.choice(pool) for _ in range(rng.randint(1, 4))))
    for _ in range(200):
        words = " ".join(rng.choice(pool) for _ in range(3)).split()
        rng.shuffle(words)
        queries.append(" ".join(words[: rng.randint(2, 12)]))
    for query in queries:
        assert classify_intent(query) == intents._classify_exhaustive(query), query


def test_normalisation_handles_case_accents_and_sharp_s():
    assert classify_intent("ÖFFNUNGSZEITEN APOTHEKE").intent == "local"
    assert classify_intent("Oeffnungszeiten Apotheke").intent == "local"
    assert classify_intent("Sicherheitsluecke Router Patch").intent == "security"
    assert classify_intent("GÜNSTIG KAUFEN Staubsauger").intent == "shopping"
    assert classify_intent("Preisvergleich   Waschmaschine\tkaufen").intent == "shopping"


# --- speed --------------------------------------------------------------------

def test_one_thousand_classifications_take_well_under_half_a_second():
    pool = [q for qs in CLEAR.values() for q in qs] + AMBIGUOUS
    queries = [pool[i % len(pool)] for i in range(1000)]
    classify_intent(queries[0])
    start = time.perf_counter()
    for query in queries:
        classify_intent(query)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"1000 classifications took {elapsed:.3f}s"


def test_pathological_inputs_stay_fast():
    nasty = ["a(" * 300, "best " * 120, "word(x,y) " * 60, "1" * 400, "a_" * 200, "`a` " * 100]
    start = time.perf_counter()
    for query in nasty:
        classify_intent(query)
    assert time.perf_counter() - start < 0.5
