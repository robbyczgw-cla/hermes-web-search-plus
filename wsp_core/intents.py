"""Query-intent detector for first-provider routing.

``classify_intent(query)`` labels a query with one of eight intents
(academic, community, docs, general, local, news, security, shopping). The 5.0
router uses the label to pick the first provider: Exa first for ``academic``
and ``docs``, Serper first for ``shopping``, Brave first for everything else.
wsp_core/routing.py holds that table; this module only classifies.

Design
------
* Pure stdlib, no network, deterministic, about 50 microseconds per query. All
  patterns are compiled at import (about 160 cues, 190 regexes).
* Precision over recall for the three labels that send a query away from the
  Brave default. A wrong ``academic`` / ``docs`` / ``shopping`` label sends the
  query to a provider that is bad for it (Exa's nDCG on community queries is
  0.27); a missed one costs little because Brave is
  a good default. So those three are only emitted on clear evidence (see
  Selection); otherwise the query falls back to another intent or ``general``.
* Cues are general linguistic and structural signals in English, German,
  French, Spanish and Italian: topic words (paper, Studie, prix,
  Oeffnungszeiten, CVE ids), phrase patterns ("how to ... in <language>",
  "best ... under <amount>"), and code-like tokens (``foo()``, ``--flag``,
  ``snake_case``, error messages). The only proper nouns are a generic class
  of programming languages, frameworks and package managers, and a few
  source-type platforms (reddit, hacker news, arXiv, pubmed, code-hosting
  ``site:`` filters) that name a *kind* of source. No products, brands,
  companies, people, places, sports teams, events or fixed years.

Scoring
-------
Every cue has a weight and a *group*. Cues of one group (synonyms, or one
concept in five languages) count once, at the maximum weight, so a repeated
concept cannot add up. A cue marked ``multi`` grows with the number of distinct
matches (1.0x, 1.5x, 2.0x for one, two, three or more). An intent's score is
the sum of its group maxima. Text is casefolded, accent-stripped and ``ss``
expanded before matching (a few cues look at the original case instead), so
patterns are written without diacritics.

Selection: intents are ranked by score and the best *eligible* one wins; if
none is eligible the answer is ``general``. An intent is eligible when

* its score reaches its threshold (3.0, or 2.5 for community and news), and
* for the three precise intents only: it leads every other intent by at least
  1.0, and it has one substantial cue (a group weighing at least 2.0), so weak
  cues such as "example", "install", "review", "vs" or "dataset" cannot add up
  to a label on their own.

Confidence is a heuristic in [0, 1], not a calibrated probability. For a
winner with score ``s`` against the best other score ``r`` it is
``x / (x + 2)`` with ``x = s - r / 2``: 0.6 at the threshold, about 0.8 at
score 8. For ``general`` it is ``0.55 - 0.4 * min(1, best / 3)``: 0.55 when no
cue fired, down to 0.15 when some intent came close to its threshold (an
ambiguous query). A blank or non-string query is ``general`` with 0.0.

Speed
-----
Most cues begin with a literal word. Each cue (or the literal-initial part of
it) is indexed under the three-letter prefixes of its words; a query only runs
the cues whose prefix occurs in it, plus the few cues without a literal start
(amounts, code tokens), which have cheap ``req`` guards. ``_classify_exhaustive``
runs every cue and is used by the tests to prove the index never hides a match.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

__all__ = ["INTENTS", "IntentDecision", "classify_intent"]

INTENTS: Tuple[str, ...] = (
    "academic", "community", "docs", "general", "local", "news", "security", "shopping",
)

# Intents whose label moves the query away from the Brave default.
PRECISE_INTENTS = frozenset({"academic", "docs", "shopping"})

_THRESHOLD: Dict[str, float] = {
    "academic": 3.0, "docs": 3.0, "shopping": 3.0, "security": 3.0, "local": 3.0,
    "community": 2.5, "news": 2.5,
}
_PRECISE_MARGIN = 1.0
# A precise intent also needs one substantial cue (a group weighing at least this much):
# weak cues alone ("example", "install", "review", "vs", "dataset") must not add up to a label.
_MIN_ANCHOR = 2.0
# Tie order when two intents have the same score.
_TIE_ORDER: Tuple[str, ...] = (
    "security", "academic", "docs", "local", "community", "shopping", "news",
)
_MAX_QUERY_CHARS = 400
_DIGIT = "\\d"  # sentinel for ``req``: the text must contain a digit
_UPPER = "[A-Z]"  # sentinel for ``req``: the original query must contain an upper-case letter


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """Result of :func:`classify_intent`.

    ``signals`` holds ``"<intent>:<cue>"`` names. For a non-general intent they
    are the cues that fired for that intent; for ``general`` they are every cue
    that fired anywhere (often none), which shows what was considered.
    """

    intent: str
    confidence: float
    signals: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Cue table
# ---------------------------------------------------------------------------

class _Spec(NamedTuple):
    intent: str
    name: str
    weight: float
    group: str
    multi: bool
    pattern: str
    req: Tuple[str, ...]
    trig: Optional[Tuple[str, ...]]
    raw: bool


_SPECS: List[_Spec] = []


def _cue(intent: str, name: str, weight: float, pattern: str, *,
         group: Optional[str] = None, multi: bool = False, req: Tuple[str, ...] = (),
         trig: Optional[Tuple[str, ...]] = None, raw: bool = False) -> None:
    """Register a cue.

    ``req``: substrings (or ``_DIGIT``/``_UPPER``) of which at least one must
    occur in the text before the regex runs. ``trig``: three-letter word
    prefixes of which one must start a query word; derived from the pattern
    when omitted, and a cue without a derivable prefix is tried on every query.
    ``raw``: match the original text (case kept) instead of the normalised one.
    """
    _SPECS.append(_Spec(intent, name, float(weight), group or name, multi, pattern, req, trig, raw))


# Currency amount: "$300", "300 €", "300 euros", "£1,299.99".
_AMOUNT = (
    r"(?:[$€£]\s?\d[\d.,]*|\d[\d.,]*\s?(?:[$€£]|eur\b|euros?\b|dollars?\b|usd\b|gbp\b|pounds?\b"
    r"|chf\b|bucks\b|franken\b))"
)
_UNDER = (
    r"(?:under|below|less than|up to|unter|bis zu|bis|moins de|sous|a moins de|jusqu'a|menos de|"
    r"por menos de|hasta|sotto|sotto i|fino a|entro|meno di)"
)

# Programming languages and frameworks as a generic class, in four ambiguity tiers.
_LANG_CLEAR = (
    r"python3?|javascript|typescript|golang|kotlin|php|csharp|sql|bash|powershell|haskell|scala|"
    r"elixir|perl|lua|dart|html5?|css3?|regex|node\.?js|graphql|webassembly|wasm|jsx|tsx|fortran|cobol"
)
_LANG_SYMBOLS = r"c\+\+|c#|objective-c"
_LANG_TOOLS = (
    r"pytorch|tensorflow|numpy|pandas|django|fastapi|laravel|jquery|tailwind|kubernetes|docker|"
    r"terraform|ansible|postgres(?:ql)?|mysql|sqlite|mongodb|redis|nginx|webpack|pytest|matplotlib|"
    r"scikit-learn|sklearn|spring boot|next\.?js|nuxt|vue\.?js|svelte|git|npm|pnpm|pip3?|conda|"
    r"gradle|cmake|makefile|kubectl"
)
_LANG_DATA = r"json|yaml|toml|devops"
_LANG_AMBIG = r"java|rust|ruby|swift|react|angular|rails|spring|linux|bootstrap|cargo|kafka|vite|maven"
# Languages and tools that can complete "how to ... in <language>".
_LANG_HOWTO = (
    r"(?:python3?|javascript|typescript|golang|kotlin|php|c\+\+|c#|csharp|sql|bash|powershell|"
    r"haskell|scala|elixir|perl|lua|dart|html|css|regex|node\.?js|java|rust|ruby|swift|react|"
    r"angular|django|pandas|numpy|git|docker|kubernetes|pip3?|npm|conda|brew|apt|cargo|vba|latex|matlab)"
)

# Consumer-goods categories (generic nouns) that rarely mean anything else.
_CAT_STRICT = (
    r"(?:headphones?|earbuds?|earphones?|soundbars?|laptops?|notebooks?|smartphones?|tablets?|cameras?|"
    r"drones?|printers?|vacuums?|vacuum cleaners?|robot vacuums?|fridges?|refrigerators?|washing machines?|"
    r"dishwashers?|air fryers?|coffee machines?|espresso machines?|mattress(?:es)?|sofas?|office chairs?|"
    r"sneakers|smartwatch(?:es)?|e-?bikes?|scooters?|tents?|sleeping bags?|backpacks?|"
    r"kopfh(?:oe|o)rer|fernseher|staubsauger\w*|waschmaschine\w*|k(?:ue|u)hlschr(?:ae|a)nke?|"
    r"matratze\w*|rucksack\w*|casques?|ecouteurs?|aspirateurs?|lave-linge|refrigerateurs?|matelas|"
    r"auriculares|aspiradora\w*|lavadora\w*|frigorifico\w*|colchon\w*|zapatillas|mochilas?|"
    r"cuffie|aspirapolvere|lavatrice|frigorifero|materasso|zaino|friggitrice|robot aspirapolvere)"
)

# --- academic --------------------------------------------------------------
_cue("academic", "preprint_repo", 4.0,
     r"\barxiv\b|\b(?:bio|med|chem)rxiv\b|\bssrn\b|\bpubmed\b|\bmedline\b|\bpreprints?\b", group="repo")
_cue("academic", "doi", 4.0, r"\bdoi\b|\b10\.\d{4,9}/\S+", req=("doi", "10."))
_cue("academic", "peer_review", 4.0,
     r"\bpeer[- ]?review\w*|\bfachbegutachtet\w*|\bbegutachtet\w*|\bpar (?:les )?pairs\b|"
     r"\bpor pares\b|\btra pari\b")
_cue("academic", "meta_analysis", 4.0,
     r"\bmeta[- ]?anal(?:y|i)\w*|\bmetanalisis\b|\bmeta-?stud(?:ie|y|ies|ien)\b")
_cue("academic", "systematic_review", 4.0,
     r"\bsystematic(?:ally)? (?:literature )?review\w*|\bsystematische[nrs]? (?:ue|u)bersicht\w*|"
     r"\bsystematische[nrs]? (?:literatur)?review\w*|\brevue systematique\b|\brevision sistematica\b|"
     r"\brevisione sistematica\b|\bscoping review\b|\bumbrella review\b|\bevidence synthesis\b")
_cue("academic", "literature_review", 3.5,
     r"\bliterature (?:review|survey|search)\b|\bliteratur(?:ue|u)bersicht\b|\bliteraturrecherche\b|"
     r"\bforschungsstand\b|\bstand der forschung\b|\brevue de (?:la )?litterature\b|"
     r"\brevision de (?:la )?literatura\b|\brevisione della letteratura\b|"
     r"\bstate[- ]of[- ]the[- ]art (?:review|survey)\b", group="lit")
_cue("academic", "trial_design", 3.5,
     r"\brandomi[sz]ed\b|\brandomisierte\w*|\brandomisee?s?\b|\baleatoriz\w+|\brandomizzat\w+|"
     r"\bclinical (?:trial|stud(?:y|ies))s?\b|\bklinische[nrs]? stud\w+|\bessais? cliniques?\b|"
     r"\bensayos? clinicos?\b|\bstudio clinico\b|\bkohortenstudie\w*|\bcohort stud\w+|"
     r"\bcase[- ]control\b|\bdouble[- ]blind\b|\bplacebo\b")
_cue("academic", "research_paper", 3.5,
     r"\bresearch (?:paper|article|publication)s?\b|\bscientific (?:paper|article|publication)s?\b|"
     r"\breview (?:article|paper)s?\b|"
     r"\bwissenschaftliche[nrms]? (?:artikel|aufsatz|aufsaetze|publikation|veroeffentlichung|studie)\w*|"
     r"\barticles? scientifiques?\b|\barticulos? cientificos?\b|\barticol[io] scientific[oi]\b|"
     r"\bfachartikel\b|\bfachliteratur\b|\bfachpublikation\w*", group="paper")
_cue("academic", "paper", 3.0,
     r"(?<!toilet )(?<!white )(?<!wall )\bpapers?\b"
     r"(?!\s+(?:towels?|planes?|airplanes?|sizes?|cups?|bags?|shredders?|clips?|trading|money|mache|"
     r"cutter|trays?|currency|mill|boy|route))", group="paper")
_cue("academic", "scholar", 3.0,
     r"\bscholarly\b|\bscholar\b|\bacademic (?:paper|article|journal|literature|source|research|"
     r"publication)s?\b")
_cue("academic", "patent", 3.0,
     r"\bpatents?\b|\bpatente[n]\b|\bpatentes\b|\bpatentschrift\b|\bpatentanmeldung\w*|"
     r"\bbrevets\b|\bbrevets? d'invention\b|\bbrevett[oi]\b")
_cue("academic", "proceedings", 3.0, r"\bproceedings\b|\bconference paper\b|\btagungsband\b")
_cue("academic", "impact_index", 3.0,
     r"\bimpact factor\b|\bh[- ]index\b|\bcitation (?:count|index)\b|\bissn\b")
_cue("academic", "journal_of", 3.5, r"\bjournal (?:of|article|club|ranking|impact)\b", group="journal")
_cue("academic", "journal", 2.5,
     r"\bjournals?\b|\bfachzeitschrift\w*|\brevue scientifique\b|\brevista cientifica\b|"
     r"\brivista scientifica\b", group="journal")
_cue("academic", "citation", 2.5,
     r"\bcitations?\b|\bcited by\b|\bzitation\w*|\bzitierweise\b|\bcitazion\w+|\bcitacion\w*")
_cue("academic", "thesis", 2.5,
     r"\btheses\b|\bthesis\b(?!\s+statement)|\bdissertations?\b|\bdoktorarbeit\b|\bhabilitation\b|"
     r"\bthese de doctorat\b|\btesis doctoral\b|\btesi di dottorato\b")
_cue("academic", "math_proof", 2.5, r"\btheorem\b|\blemma\b|\bproof of\b|\bcorollary\b|\bconjecture\b|"
     r"\btheoreme\b|\bteorema\b")
_cue("academic", "scientific", 2.5,
     r"\bscientific\w*|\bwissenschaftlich\w*|\bscientifique\w*|\bcientific[oa]s?\b|"
     r"\bscientific[oa]\b|\bscientifici\b|\bscientifiche\b")
_cue("academic", "study_on", 3.0,
     r"\bstudy (?:on|of|about|shows?|finds?|found|results?)\b|\bstudie (?:zu|zum|zur|ueber|uber|von|"
     r"belegt|zeigt)\b|\betudes? (?:sur|montre|montrent)\b|\bestudios? (?:sobre|muestra|demuestra)\b",
     group="study")
_cue("academic", "study", 2.0,
     r"(?<!case )(?<!case-)\bstud(?:y|ies)\b(?!\s+(?:guide|abroad|plan|tips|room|permit|visa|music|"
     r"techniques?|notes|schedule|for|materials?|program))|\bstudien?\b|\betudes?\b|\bestudios?\b|"
     r"\bstudi\b", group="study")
_cue("academic", "effects_of", 1.0,
     r"\b(?:effects?|impact|influence|association|relationship|correlation) (?:of|between)\b|"
     r"\bauswirkung\w*|\beinfluss\b|\bzusammenhang\b|\bkorrelation\b|\beffets? de\b|\bimpact de\b|"
     r"\blien entre\b|\befectos? (?:de|del|sobre)\b|\bimpacto de\b|\brelacion entre\b|"
     r"\beffetti di\b|\bimpatto di\b|\bcorrelazione\b")
_cue("academic", "abstract", 1.5, r"\babstracts?\b")
_cue("academic", "method_terms", 2.0,
     r"\bempirical\w*|\bempirisch\w*|\bhypothes[ie]s\b|\bmethodolog\w+|\bmethodik\b|"
     r"\bp[- ]values?\b|\bsample size\b|\bconfidence interval\w*|\beffect size\b|"
     r"\bregression analysis\b|\bstatistically significant\b")
_cue("academic", "dataset", 1.5, r"\bdatasets?\b|\bcorpus\b|\bcorpora\b")
_cue("academic", "survey_on", 3.0, r"\bsurvey on\b", group="survey")
_cue("academic", "survey", 1.0, r"\bsurvey\b|\bstate[- ]of[- ]the[- ]art\b|\boverview of\b")
_cue("academic", "research", 1.0,
     r"\bresearch\w*|\bforsch\w+|\bchercheurs?\b|\binvestigadores\b|\bricercatori\b|\bevidence\b")

# --- docs ------------------------------------------------------------------
_cue("docs", "site_docs", 4.0,
     r"\bsite:\s*(?:docs?|developers?|dev|api|help|support|learn|wiki|manual|reference)\.|"
     r"\bsite:\s*(?:[\w-]+\.)*(?:github|gitlab|bitbucket|codeberg|readthedocs)\b")
_cue("docs", "how_to_lang", 4.0,
     r"\b(?:how (?:do i|to|can i|can you|would i|should i)|wie (?:kann ich|mache ich|geht|kann man)|"
     r"comment|como|come)\b[^.?!\n]{0,60}?\b(?:in|with|using|mit|en|avec|con)\s+" + _LANG_HOWTO + r"(?!\w)")
_cue("docs", "error_phrase", 3.5,
     r"\btraceback\b|\bstack ?trace\b|\bsegfault\b|\bsegmentation fault\b|\bcore dumped\b|"
     r"\bkernel panic\b|\bnull ?pointer\b|\bundefined (?:reference|symbol|variable|index|method)\b|"
     r"\bunexpected token\b|\bunhandled (?:exception|rejection)\b|\bpermission denied\b|"
     r"\bcommand not found\b|\bno such file(?: or directory)?\b|\bmodule not found\b|"
     r"\bcannot find module\b|\bis not a function\b|\bis not defined\b|\bconnection refused\b|"
     r"\beconn\w+|\benoent\b|\beacces\b|\betimedout\b|\beaddrinuse\b|\bexit code \d+|"
     r"\bsyntax error\b|\bcompil(?:e|ation) (?:error|failed)\b|\bbuild failed\b|\blinker error\b|"
     r"\bfatal error\b")
_cue("docs", "code_syntax", 3.5,
     r"\bdef \w+\(|\bfunction \w+\(|\bfrom [\w.]+ import\b|\bimport \w+(?:\.\w+)+|"
     r"\bconsole\.log\b|\bselect\b.{1,60}\bfrom\b|\bcreate table\b|\binsert into\b|\balter table\b|"
     r"\bpublic static\b|\bnew [a-z]\w+\(")
_cue("docs", "code_include", 3.5, r"#include\s*<", group="code_syntax", req=("#include",))
_cue("docs", "git_cmd", 3.5,
     r"\bgit (?:clone|commit|rebase|merge|checkout|switch|push|pull|fetch|stash|reset|revert|branch|"
     r"diff|log|cherry-pick|tag|bisect|submodule|worktree|add|status|init|remote|restore|blame)\b")
_cue("docs", "docs_word", 3.5, r"\bdocs\b", group="doc")
_cue("docs", "documentation_word", 2.5,
     r"\bdocumentation\b|\bdokumentation\b|\bdocumentacion\b|\bdocumentazione\b", group="doc")
_cue("docs", "reference_tech", 3.5,
     r"\b(?:api|sdk|cli|command|syntax|language|class|method|function|config\w*|parameter|schema)s? "
     r"reference\b|\breference (?:docs?|manual|guide|implementation|documentation)\b|"
     r"\b(?:api|sdk)[- ]referenz\b|\bnachschlagewerk\b")
_cue("docs", "sdk", 3.0, r"\bsdks?\b")
_cue("docs", "changelog", 3.0, r"\bchangelogs?\b|\bchange log\b")
_cue("docs", "readme", 3.0, r"\breadme\b|\bman pages?\b|\bmanpages?\b")
_cue("docs", "cheat_sheet", 2.5, r"\bcheat ?sheets?\b|\bspickzettel\b|\baide-memoire\b", group="readme")
_cue("docs", "pkg_cmd", 3.0,
     r"\b(?:pip3?|npm|pnpm|yarn|cargo|gem|brew|apt(?:-get)?|dnf|composer|nuget|conda|pacman|go get|"
     r"go install)\s+(?:install|add|update|upgrade|remove|uninstall|run|i|ci|init|build|test)\b|"
     r"\bpython3?\s+-m\b|\b(?:docker|podman)\s+(?:run|build|compose|exec|ps|pull|push|logs|image|"
     r"network|volume|stop|rm)\b|\bkubectl\s+\w+")
_cue("docs", "code_call", 3.0,
     r"(?<![\w.])[a-z_][\w]*(?:\.[a-z_]\w*)*\(\)|\b[a-z_]\w*\.[a-z_]\w*\([^)\n]{0,40}\)|"
     r"\b[a-z_]\w*\([^)\n]{0,40}[,'\"=_.\d][^)\n]{0,40}\)", req=("(",))
_cue("docs", "code_assign", 2.5, r"\b[a-z_]{3,}=[a-z0-9_\"'-]+", req=("=",))
_cue("docs", "code_flag", 3.0, r"(?<![\w-])--[a-z][a-z0-9-]+", req=("--",))
_cue("docs", "code_backtick", 3.0, r"`[^`\n]{1,60}`", req=("`",))
_cue("docs", "code_snake", 2.0, r"(?<![\w.@/-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w@/-])", req=("_",))
_cue("docs", "code_camel", 2.0, r"\b[a-z]{2,}[A-Z][a-z]{2,}\w*", raw=True, req=(_UPPER,))
_cue("docs", "error_class", 3.0, r"\b[a-z]{3,}(?:error|exception)\b")
_cue("docs", "error_code", 2.5,
     r"\b(?:error|fehler|erreur)\s*(?:code\s*)?(?:0x[0-9a-f]+|[a-z]*\d{2,})\b|\bhttp ?[1-5]\d\d\b|"
     r"\bstatus code\b", req=("error", "fehler", "erreur", "http", "status"))
_cue("docs", "api", 2.0, r"\bapis?\b")
_cue("docs", "cli", 2.0, r"\bcli\b")
_cue("docs", "shell_cmd", 2.0,
     r"\b(?:chmod|chown|sudo|systemctl|journalctl|curl|wget|rsync|crontab|iptables|awk|grep)\s+[-./~\w]")
_cue("docs", "file_ext", 2.0,
     r"\b(?!(?:node|vue|next|nuxt|express)\.js\b)[\w-]+\.(?:py|js|ts|tsx|jsx|rs|go|java|kt|rb|php|cpp|hpp|sql|json|ya?ml|toml|ini|cfg|conf|env|"
     r"ps1|html|css|scss|xml|gradle|ipynb|dockerfile|makefile)\b|\brequirements\.txt\b", req=(".",))
_cue("docs", "code_dotted", 2.0,
     r"(?<![\w./@-])(?!(?:node|vue|next|nuxt|express)\.js\b)[a-z_][a-z0-9_]+(?:\.[a-z_][a-z0-9_]+)+(?![\w.@/-])"
     r"(?<!\.com)(?<!\.org)(?<!\.net)"
     r"(?<!\.edu)(?<!\.gov)(?<!\.eu)(?<!\.de)(?<!\.at)(?<!\.fr)(?<!\.es)(?<!\.it)(?<!\.uk)(?<!\.ch)"
     r"(?<!\.info)(?<!\.co)(?<!\.us)(?<!\.ru)(?<!\.jp)(?<!\.cn)", req=(".",))
_cue("docs", "breaking_changes", 2.0, r"\bbreaking changes?\b|\bdeprecat\w+")
_cue("docs", "hex_literal", 2.0, r"\b0x[0-9a-f]{3,}\b", req=("0x",))
_cue("docs", "lang_clear", 2.0, r"\b(?:" + _LANG_CLEAR + r")(?![\w])", group="lang")
_cue("docs", "lang_symbols", 2.0, r"(?<![\w])(?:" + _LANG_SYMBOLS + r")(?![\w])", group="lang")
_cue("docs", "lang_tools", 2.0, r"\b(?:" + _LANG_TOOLS + r")(?![\w])", group="lang")
_cue("docs", "lang_data", 1.5, r"\b(?:" + _LANG_DATA + r")(?![\w])", group="lang")
_cue("docs", "lang_ambiguous", 1.0, r"\b(?:" + _LANG_AMBIG + r")\b", group="lang")
_cue("docs", "tutorial", 1.5,
     r"\btutorials?\b|\bhow-?tos?\b|\bwalkthrough\b|\bgetting started\b|\bquick-?start\b|"
     r"\btutoriel\b|\btutoriales\b|\bhandbook\b|\bcookbook\b", group="tut")
_cue("docs", "guide_words", 1.0,
     r"\banleitung\b|\bguide\b|\bguida\b|\bguia\b|\bbest practices?\b|\bwhat's new\b|"
     r"\bwhats new\b|\bfaq\b|\broadmap\b", group="tut")
_cue("docs", "manual", 1.5, r"\bmanual\b|\bhandbuch\b|\bmanuel\b|\bmanuale\b")
_cue("docs", "snippet", 1.5, r"\bsnippets?\b|\bsample code\b|\bboilerplate\b", group="example")
_cue("docs", "example", 1.0,
     r"\bexamples?\b|\btemplates?\b|\bbeispiel\w*|\bexemples?\b|\bejemplos?\b|\besempi\b|\besempio\b",
     group="example")
_cue("docs", "install_setup", 1.0,
     r"\binstall\w*|\bsetup\b|\bset up\b|\bconfigur\w+|\bconfig\b|\beinricht\w+|\binstallier\w+|"
     r"\bkonfigur\w+|\binstaller\b|\binstalar\b|\binstalacion\b|\binstallare\b")
_cue("docs", "tech_terms", 1.0,
     r"\b(?:yaml|xml|csv|html|http|https|tcp|udp|ssh|ssl|tls|dns|url|uri|uuid|jwt|oauth|cors|"
     r"webhooks?|endpoints?|middleware|schema|stdout|stderr|runtime|compiler|interpreter|syntax|"
     r"semver|async|mutex|callbacks?|decorators?|dependenc(?:y|ies)|repos?|repository|commits?|"
     r"branch|pull request|plugins?|extensions?|settings|parameters?|hooks?|config|container|"
     r"tokens?|prompts?|backend|frontend|database|databases|deploy\w*|pipeline|compile\w*|debug\w*|"
     r"refactor\w*|variables?|functions?|methods?|arrays?|strings?|booleans?|integers?|loops?|"
     r"scripts?|terminal|cron|daemon|proxy|websockets?|sockets?|caching|threads?|queries|migrations?|"
     r"lockfile|workspace|monorepo|modules?|virtualenv|venv|bundler|linter|unit tests?|mocks?|"
     r"mocking|fixtures?|logging|boilerplate|serverless|microservices?|latency|"
     r"throughput|binary|binaries|encoding|serializ\w+|parsing|parser)\b", multi=True)
_cue("docs", "trouble_words", 1.0,
     r"\berror\b|\bexception\b|\bbug\b|\bcrash\w*|\bfails\b|\bfailed\b|\bnot working\b|\bissues?\b|"
     r"\bfehler\b|\berreur\b|\bproblem\b")
_cue("docs", "library_words", 1.0, r"\blibrar(?:y|ies)\b|\bpackages?\b|\bmodules?\b|\bframework\b")
_cue("docs", "reference_plain", 1.0, r"\breference\b|\breferenz\b|\breferencia\b|\bspecification\b")
_cue("docs", "release_notes_doc", 1.0, r"\breleases? notes?\b", group="relnotes")
_cue("docs", "open_source", 1.0,
     r"\bopen[- ]?source\b|\bcodigo abierto\b|\bcodice aperto\b|\bquelloffen\w*|\bsource ouverte\b|"
     r"\bcode source\b")
_cue("docs", "alternatives", 1.0, r"\balternatives? (?:to|a|zu|aux?)\b|\balternativen? (?:zu|fuer|fur)\b")
_cue("docs", "self_hosted", 1.0,
     r"\bself[- ]?host\w*|\bselbst gehostet\b|\bauto-?heberge\w*|\bautoalojado\b")

# --- security --------------------------------------------------------------
_cue("security", "cve_id", 5.0, r"\bcve[- ]?\d{4}[- ]\d{3,}\b", group="cve", req=(_DIGIT,))
_cue("security", "cve", 3.5, r"\bcves?\b", group="cve")
_cue("security", "zero_day", 4.0, r"\bzero[- ]?days?\b|\b0[- ]day\b")
_cue("security", "vulnerability", 3.5,
     r"\bvulnerabilit\w+|\bvulnerable\b|\bschwachstell\w+|\bsicherheitsl(?:ue|u)cke\w*|"
     r"\bsicherheitsproblem\w*|\bfailles? de securite\b|\bvulnerabilidad\w*|\bvulnerabilita\b")
_cue("security", "exploit", 3.0, r"\bexploit\w*")
_cue("security", "threat", 3.5,
     r"\bransomware\b|\bmalware\b|\bspyware\b|\btrojan\w*|\bbotnets?\b|\brootkits?\b|\bbackdoors?\b|"
     r"\bkeyloggers?\b|\bphishing\b|\bschadsoftware\b|\bmaliciels?\b|\bcomputer ?virus\b|"
     r"\bcomputervirus\b|\berpressungstrojaner\b")
_cue("security", "advisory_sec", 3.5,
     r"\bsecurity (?:advisor(?:y|ies)|bulletin|notice|patch(?:es)?|updates?|fix(?:es)?|flaws?|holes?|"
     r"bugs?|issues?|breach(?:es)?|incidents?|researchers?)\b|\bsicherheitshinweis\w*|"
     r"\bsicherheitswarnung\w*|\bsicherheitsupdate\w*|\bsicherheitspatch\w*|\bsicherheitsvorfall\w*|"
     r"\bavis de securite\b|\bbulletin de securite\b|\baviso de seguridad\b|\bboletin de seguridad\b|"
     r"\bavviso di sicurezza\b|\bbollettino di sicurezza\b", group="advisory")
_cue("security", "advisory", 2.0,
     r"(?<!travel )(?<!health )(?<!weather )\badvisor(?:y|ies)\b(?!\s+board)", group="advisory")
_cue("security", "attack", 3.5,
     r"\bcyber-?attack\w*|\bcyberangriff\w*|\bcyberattaque\w*|\bciberataque\w*|\battacco informatico\b|"
     r"\battacchi informatici\b|\bddos\b|\bsupply[- ]chain (?:attack|compromise)\w*|"
     r"\blieferkettenangriff\w*|\bdata breach\w*|\bdata leak\w*|\bdatenleck\w*|\bdatenpanne\w*|"
     r"\bfuite de donnees\b|\bviolation de donnees\b|\bfiltracion de datos\b|"
     r"\bbrecha de seguridad\b|\bviolazione dei dati\b")
_cue("security", "vuln_class", 4.0,
     r"\bsql injection\b|\bxss\b|\bcsrf\b|\bssrf\b|\bremote code execution\b|\brce\b|"
     r"\bprivilege escalation\b|\bbuffer overflow\b|\buse[- ]after[- ]free\b|\bpath traversal\b|"
     r"\bdirectory traversal\b|\bcommand injection\b|\bauthentication bypass\b|\bauth bypass\b|"
     r"\bcvss\b|\bcwe-\d+\b|\bsandbox escape\b|\bman[- ]in[- ]the[- ]middle\b")
_cue("security", "cyber_words", 2.0,
     r"\bcyber ?security\b|\binfosec\b|\bit-sicherheit\b|\bcybersicherheit\b|\bciberseguridad\b|"
     r"\bcybersecurite\b|\bsicurezza informatica\b")
_cue("security", "hacker", 1.5, r"\bhack(?:er|ers|ed|ing)?\b|\bhackerangriff\w*")
_cue("security", "patch", 1.5, r"\bpatch(?:es|ed)?\b(?!\s+notes)")
_cue("security", "mitigation", 1.5,
     r"\bmitigat\w+|\bremediat\w+|\bhardening\b|\bincident response\b|\bthreat (?:intelligence|actor)s?\b|"
     r"\bindicators? of compromise\b|\bcompromised\b")
_cue("security", "security_plain", 1.0, r"(?<!social )\bsecurity\b|\bsicherheit\b|\bsecurite\b|\bseguridad\b")

# --- local -----------------------------------------------------------------
_cue("local", "near_me", 4.0,
     r"\bnear me\b|\bnearby\b|\bclose to me\b|\bin my area\b|\baround me\b|\bin (?:der|meiner) n(?:ae|a)he\b|"
     r"\bin der umgebung\b|\bpres de chez moi\b|\bpres de moi\b|\ba proximite\b|\bautour de moi\b|"
     r"\bcerca de (?:mi|aqui)\b|\bcerca de mi ubicacion\b|\bvicino a me\b|\bnei dintorni\b|"
     r"\bqui vicino\b|\bnahe bei\b")
_cue("local", "hours", 3.5,
     r"\bopening (?:hours|times)\b|\bhours of operation\b|\bopen (?:now|today|tonight|late|24 hours)\b|"
     r"\b(?:oe|o)ffnungszeit\w*|\bgeschaeftszeiten\b|\bgeschaftszeiten\b|\bgeoeffnet\b|\bgeoffnet\b|"
     r"\bhoraires?\b|\bouvert (?:aujourd|maintenant|dimanche|le)\b|\bhorarios? de (?:apertura|atencion)\b|"
     r"\babierto (?:ahora|hoy|los)\b|\borari di apertura\b|\baperto (?:oggi|ora|domenica)\b")
_cue("local", "weather", 3.0,
     r"\bweather\b|\bwetter\w*|\bmeteo\b|\bprevisions? meteo\w*|\bel tiempo\b|\btiempo en\b|"
     r"\bpronostico\b|\bprevisioni del tempo\b|\bwettervorhersage\b")
_cue("local", "forecast", 1.5, r"\bforecast\b|\bvorhersage\b")
_cue("local", "precipitation", 1.5,
     r"\brain\b|\bregen\b|\bsnow\b|\bschnee\b|\bpluie\b|\blluvia\b|\bpioggia\b|\bneige\b|\bnieve\b|"
     r"\bneve\b|\btemperaturen\b|\bsonnig\b|\bgewitter\b")
_cue("local", "tonight", 2.0,
     r"\bheute abend\b|\bheute nacht\b|\btonight\b|\bthis evening\b|\bce soir\b|\besta noche\b|"
     r"\bstasera\b|\bthis weekend\b|\bdieses wochenende\b|\bam wochenende\b|\bce week-?end\b|"
     r"\beste fin de semana\b|\bquesto fine settimana\b|\bmorgen abend\b")
_cue("local", "poi", 2.0,
     r"\brestaurants?\b|\bcafes?\b|\bbars?\b|\bpubs?\b|\bhotels?\b|\bhostels?\b|\bpharmac(?:y|ies|ie)\b|"
     r"\bapotheke\w*|\bfarmacia\b|\bsupermarkets?\b|\bsupermarkt\b|\bbakery\b|\bb(?:ae|a)ckerei\b|"
     r"\bboulangerie\b|\bpanaderia\b|\bpanetteria\b|\bbarbers?\b|\bhairdresser\b|\bfriseur\w*|"
     r"\bcoiffeur\b|\bpeluquer\w+|\bparrucchier\w+|\bdentist\w*|\bzahnarzt\b|\bdentiste\b|\bdentista\b|"
     r"\bhospital\b|\bkrankenhaus\b|\bhopital\b|\bospedale\b|\bpetrol station\b|\bgas station\b|"
     r"\btankstelle\b|\bstation-service\b|\bgasolinera\b|\bpizzeria\b|\btrattoria\b|\bgelateria\b|"
     r"\bmetzgerei\b|\bboucherie\b|\bcarniceria\b|\bmacelleria\b|\bcinema\b|\bkino\b|\bmuseums?\b|"
     r"\bmusee\b|\bmuseo\b|\bschwimmbad\b|\bpiscine\b|\bpiscina\b|\bfitnessstudio\b|\bgimnasio\b|"
     r"\bpalestra\b|\bpost office\b|\bpostamt\b|\bbureau de poste\b|\bcorreos\b|\bufficio postale\b|"
     r"\bparkplatz\b|\bparking\b|\baparcamiento\b|\bparcheggio\b|\btakeaway\b|\blieferservice\b")
_cue("local", "events", 1.5,
     r"\bevents?\b|\bveranstaltung\w*|\bevenements?\b|\beventos?\b|\beventi\b|\bwhat's on\b|"
     r"\bthings to do\b|\bwas ist los\b|\bquoi faire\b|\bque hacer\b|\bcosa fare\b|\bflohmarkt\b")
_cue("local", "culture", 1.0,
     r"\bkultur\b|\bculture\b|\bcultura\b|\bkonzert\w*|\bconcerts?\b|\btheat(?:er|re)\b|\bteatro\b|"
     r"\bausstellung\w*|\bfestivals?\b|\bexpositions?\b|\bexhibitions?\b")
_cue("local", "address", 1.5,
     r"(?<!ip )(?<!mac )(?<!email )(?<!e-mail )\baddress\b(?!\s+(?:bar|space|book|resolution))|"
     r"\badresse\b|\bdirections? to\b|\bhow to get to\b|\banfahrt\b|\bwegbeschreibung\b|"
     r"\bitineraire\b|\bcomment aller\b|\bcomo llegar\b|\bdireccion\b|\bindirizzo\b|"
     r"\bcome arrivare\b|\bphone number\b|\btelefonnummer\b|\bnumero de telephone\b|"
     r"\bnumero de telefono\b|\bnumero di telefono\b")
_cue("local", "contact", 1.0, r"\bkontakt\b|\bcontacto\b|\bcontatti\b")

# --- community -------------------------------------------------------------
_cue("community", "platform", 5.0,
     r"\breddit\b|\bsubreddit\b|(?<![\w/])r/[a-z0-9_]{2,}|\bhacker ?news\b|\bshow hn\b|\bask hn\b")
_cue("community", "forum", 4.0,
     r"\bforum\w*|\bforen\b|\bforos?\b|\bnewsgroups?\b|\bmessage boards?\b|\bbulletin boards?\b")
_cue("community", "community_word", 2.5,
     r"\bcommunit(?:y|ies)\b|\bcommunaute\w*|\bcomunidad\w*|\bcomunita\b", group="forum")
_cue("community", "anyone", 3.5,
     r"\b(?:does|has|did|is|will|would|can|could) (?:any|some)(?:one|body)\b|"
     r"\banyone (?:else|here|out there|using|tried|know|knows|used|have|got)\b|"
     r"\bhat (?:schon )?(?:irgendwer|jemand)\b|\b(?:irgendwer|jemand) (?:erfahrung|ahnung|schon)\w*|"
     r"\bquelqu'un (?:a|connait|utilise|sait)\b|\balguien (?:ha|sabe|conoce|usa|ya)\b|"
     r"\bqualcuno (?:ha|sa|conosce|usa)\b")
_cue("community", "discussion", 3.0,
     r"\bdiscussions?\b|\bdiskussion\w*|\bdiscusion\w*|\bdiscussione\b|\bdiscussioni\b|\bdebate\b|"
     r"\bdebatte\b")
_cue("community", "opinions", 3.0,
     r"\bopinions?\b|\bmeinung\w*|\bopiniones\b|\bopinioni\b|\bopinion\w*|"
     r"\bwhat do (?:you|people) think\b|\bwas haltet ihr\b|\bwas halten sie\b|\bqu'en pensez\b|"
     r"\bque opinan\b|\bche ne pensate\b")
_cue("community", "avis", 2.5, r"\bavis\b", group="opinions")
_cue("community", "experiences", 3.0,
     r"\berfahrungen\b|\berfahrungsberichte?\b|\bexperiences? (?:with|of)\b|\bmy experiences?\b|"
     r"\bexperiencias? con\b|\bmis experiencias\b|\bexperiences? avec\b|\bretours? d'experience\b|"
     r"\besperienze con\b|\bexperiences\b|\bexperiencias\b|\besperienze\b", group="experiences")
_cue("community", "worth_it", 2.5,
     r"\bworth it\b|\bworth buying\b|\blohnt sich\b|\bvaut le coup\b|\bvale la pena\b|"
     r"\bne vale la pena\b|\bis it good\b|\bis it worth\b")
_cue("community", "should_i", 2.5,
     r"\bshould i (?:buy|get|use|switch|choose|go)\b|\bwhich (?:one )?(?:is|should)\b|"
     r"\bwhat should i\b|\bwas soll ich\b|\bque dois-je\b|\bque debo\b|\bcosa devo\b")
_cue("community", "long_term", 2.5,
     r"\blong[- ]term (?:review|experience|test|use)\b|\blangzeit\w*|\bafter (?:\d+|a|one|two|three) "
     r"(?:years?|months?) (?:of|with|using)\b")
_cue("community", "real_users", 2.0,
     r"\breal users?\b|\buser reports?\b|\buser reviews?\b|\bnutzerberichte\b|\bnutzerbewertungen\b")
_cue("community", "recommend", 1.5,
     r"\brecommend\w*|\bempfehl\w+|\brecommand\w+|\brecomiend\w+|\brecomend\w+|\bconsiglia\w*")
_cue("community", "advice", 1.5,
     r"\badvice\b|\bratschlag\w*|\bconseils?\b|\bconsejos?\b|\bconsigli\b|\bhelp me\b|\bhilfe\b")
_cue("community", "thread", 1.5, r"\bthreads?\b|\bposts?\b")

# --- news ------------------------------------------------------------------
_cue("news", "news_word", 3.0,
     r"(?<![./:])\bnews\b(?!\.)|\bnachrichten\b|\bneuigkeiten\b|\bactualites?\b|\bnoticias?\b|"
     r"\bnotizie\b|\bnouvelles\b|\bheadlines?\b|\bschlagzeilen\b|\bgros titres\b|\btitulares\b|"
     r"\bnewsticker\b|\bpress releases?\b|\bpressemitteilung\w*|\bpressemeldung\w*|"
     r"\bcommunique de presse\b|\bcomunicado de prensa\b|\bcomunicato stampa\b")
_cue("news", "breaking", 3.5,
     r"\bbreaking(?: news)?\b(?!\s+(?:bad|changes?|point|down|up|in))|\beilmeldung\b|"
     r"\bderniere minute\b|\bultima hora\b|\bultim'ora\b|"
     r"\blive[- ]?(?:ticker|blog|updates?|stream|score|scores|coverage|berichterstattung)\b")
_cue("news", "what_happened", 2.0,
     r"\bwhat happened\b|\bwas ist passiert\b|\bque s'est-il passe\b|\bque paso\b|\bcosa e successo\b")
_cue("news", "earnings", 2.5,
     r"\bearnings\b|\bquartalszahlen\b|\bquartalsergebnis\w*|\bgeschaeftszahlen\b|\bgeschaftszahlen\b|"
     r"\bresultats (?:trimestriels|financiers)\b|\bresultados (?:trimestrales|financieros)\b|"
     r"\brisultati trimestrali\b|\bquarterly (?:results?|report)\b|\bannual report\b|"
     r"\bgeschaeftsbericht\b|\bgeschaftsbericht\b")
_cue("news", "investor_relations", 2.5, r"\binvestor[- ]relations?\b|\binvestor monthly\b")
_cue("news", "markets", 2.0,
     r"\bstock (?:market|price)s?\b|\bshare price\b|\bborse\b|\bboerse\b|\bbourse\b|\bbolsa\b|\bborsa\b|"
     r"\baktienkurs\w*|\bkursziel\b|\bdividend\w*|\bipo\b|\bmerger\w*|\bacquisitions?\b|\bacquires?\b|"
     r"\bacquired\b|\blayoffs?\b|\bjob cuts\b|\bbankruptcy\b|\binsolvenz\w*|\bfaillite\b|\bquiebra\b|"
     r"\bfallimento\b|\bwahlergebnis\w*|\belection results?\b")
_cue("news", "fin_metric", 1.5,
     r"\brevenues?\b|\bumsatz\b|\bchiffre d'affaires\b|\bingresos\b|\bricavi\b|\bgross margin\b|"
     r"\bprofits?\b|\bgewinn\b|\bguidance\b|\bebitda\b|\beps\b|\boperating income\b|\b10-?[qk]\b|\b8-?k\b")
_cue("news", "fiscal_period", 1.5,
     r"\bfiscal (?:year|quarter)\b|\bgeschaeftsjahr\b|\bgeschaftsjahr\b|\bexercice fiscal\b|"
     r"\bejercicio fiscal\b|\banno fiscale\b|\bq[1-4]\b|\bfy ?\d{2,4}\b|\bh[12] ?\d{2,4}\b", req=(_DIGIT, "fiscal", "geschaeftsjahr", "geschaftsjahr", "exercice", "ejercicio", "anno fiscale"))
_cue("news", "periodic_report", 1.5,
     r"\b(?:monthly|quarterly|annual|weekly|monatlich\w*|quartals\w*|jaehrlich\w*)\s+"
     r"(?:revenue|results?|report|sales|umsatz|zahlen|bericht)\b|\bmonthly revenue\b")
_cue("news", "announce", 2.0,
     r"\bannounc\w+|\bankuendig\w*|\bankundig\w*|\bangekuendigt\b|\bangekundigt\b|\bannonc\w+|"
     r"\banunci\w+|\bannunci\w+|\bunveil\w*|\bvorgestellt\b|\bpresentato\b|\bdevoile\w*")
_cue("news", "release_notes", 2.0, r"\breleases? notes?\b|\bpatch notes\b", group="release")
_cue("news", "release_word", 1.0,
     r"\breleas\w+|\blaunch\w*|\bveroeffentlich\w*|\bveroffentlich\w*|\bsortie\b|\blancement\b|"
     r"\blanzamiento\b|\blancio\b", group="release")
_cue("news", "this_period", 2.0,
     r"\bthis (?:week|month|morning)\b|\bdiese woche\b|\bdiesen monat\b|\bcette semaine\b|\bce mois\b|"
     r"\besta semana\b|\beste mes\b|\bquesta settimana\b|\bquesto mese\b|\blast (?:night|week|month)\b|"
     r"\bletzte woche\b|\bla semaine derniere\b|\bla semana pasada\b|\bla settimana scorsa\b")
_cue("news", "today", 1.5,
     r"\btoday\b|\byesterday\b|\bheute\b(?!\s+(?:abend|nacht))|\bgestern\b|\baujourd'?hui\b|\bhoy\b|"
     r"\bayer\b|\boggi\b|\bieri\b")
_cue("news", "latest", 1.0,
     r"\blatest\b|\bnewest\b|\bmost recent\b|\brecent(?:ly)?\b|\bjust (?:released|announced|launched)\b|"
     r"\bneueste\w*|\baktuell\w*|\bderni(?:er|ere|ers|eres)\b|\brecent(?:e|es|s)\b|\bultim[oa]s?\b|"
     r"\bultimi\b|\bultime\b|\breciente\w*|\bcurrently\b|\bright now\b|\bjetzt\b|\bmaintenant\b|\bahora\b|"
     r"\badesso\b")
_cue("news", "blog", 1.0,
     r"\bblog(?:post)?\b|\bblog post\b|\bnewsroom\b|\bnewsletter\b|\bnewswire\b|\bpresse\b|\bprensa\b")
_cue("news", "sports_terms", 2.0,
     r"\bstandings?\b|\bfixtures?\b|\bmatchday\b|\bspieltag\w*|\bspielstand\b|\blineups?\b|"
     r"\baufstellung\b|\bclassement\b|\bclasificacion\b|\bclassifica\b|\bleague table\b|"
     r"\bmatch report\b|\bspielplan\b|\bplayoffs?\b")
_cue("news", "sports_weak", 1.0,
     r"\bscores?\b|\btabelle\b|\bergebnisse\b|\btransfers?\b|\bresults\b|\bhighlights\b")
_cue("news", "month_year", 1.0,
     r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december|"
     r"jan|feb|apr|jun|jul|aug|sep|sept|oct|nov|dec|januar|februar|maerz|marz|mai|juni|juli|oktober|"
     r"dezember|janvier|fevrier|mars|avril|juin|juillet|aout|septembre|octobre|novembre|decembre|"
     r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|"
     r"gennaio|febbraio|aprile|maggio|giugno|luglio|settembre|ottobre)\s+(?:19|20)\d\d\b", group="date", req=(_DIGIT,))
_cue("news", "year", 0.5, r"\b(?:19|20)\d\d\b", group="date", req=(_DIGIT,))

# --- shopping --------------------------------------------------------------
_cue("shopping", "buy", 3.0,
     r"\bbuy(?:ing)?\b|\bwhere to (?:buy|get|order)\b|\bbuyers?'? guide\b|\bkaufen\b|\bkaufberatung\b|"
     r"\bkaufempfehlung\w*|\bkauftipps?\b|\bzu kaufen\b|\bacheter\b|\bachetez\b|\bguide d'achat\b|"
     r"\bcomprar\b|\bdonde comprar\b|\bguia de compra\b|\bcomprare\b|\bacquistare\b|\bdove comprare\b|"
     r"\bguida all'acquisto\b|\bacquisto\b|\bacquisti\b|\bachats?\b|\bcompras\b")
_cue("shopping", "purchase", 2.5, r"\bpurchas\w+|\bbestellen\b|\border online\b|\bcommander\b", group="buy")
_cue("shopping", "price", 2.5,
     r"\bpric(?:e|es|ing|ed)\b|\bpreis(?:e|es|en|liste|entwicklung|erhoehung|senkung|check|tipp)?\b|"
     r"\bprix\b|\bprecios?\b|\bprezzi\b|\bprezzo\b|\bwas kostet\w*|\bwie viel kostet\b|\bwieviel kostet\b|"
     r"\bhow much (?:is|does|do|are)\b|\bcombien (?:coute|ca coute)\b|\bcuanto (?:cuesta|vale|cuestan)\b|"
     r"\bquanto costa\b|\bcost of\b|\bkosten fuer\b")
_cue("shopping", "price_compare", 3.0,
     r"\bpreisvergleich\w*|\bprice compar\w+|\bcompare prices\b|\bcomparateur de prix\b|"
     r"\bcomparador de precios\b|\bcomparatore (?:di )?prezzi\b|\bprice history\b|\bprice drop\b|"
     r"\bprice tracker\b|\bpreisverlauf\b", group="price")
_cue("shopping", "best_under", 3.0,
     r"\b(?:best|top|cheapest|beste[nrms]?|meilleur[es]?|mejor(?:es)?|migliore|migliori|"
     r"g(?:ue|u)nstigste\w*)\b[^.?!\n]{0,60}?\b" + _UNDER + r"\s+" + _AMOUNT, group="amount", req=(_DIGIT,))
_cue("shopping", "under_amount", 2.5, r"\b" + _UNDER + r"\s+" + _AMOUNT, group="amount", req=(_DIGIT,))
_cue("shopping", "currency_amount", 2.0, r"(?<![\w])" + _AMOUNT, group="amount", req=(_DIGIT,))
_cue("shopping", "deals", 3.0,
     r"\bdeals\b|\b(?:best|good|great|hot|super) deal\b(?!\s+of\b)|\bon sale\b|\bfor sale\b|\bdiscounts?\b(?!\s+(?:rate|factor|cash|curve|window))|"
     r"\bcoupons?\b|\bvouchers?\b|\bpromo codes?\b|\brabatt\w*|\bgutschein\w*|\bschn(?:ae|a)ppchen\b|"
     r"\bsonderangebot\w*|\bsoldes\b|\bpromos\b|\bbons? plans?\b|\bdescuentos?\b|\bgangas?\b|"
     r"\bsconti?\b|\bsaldi\b|\bcode promo\b|\bcodice sconto\b", group="deal")
_cue("shopping", "deal_sale", 2.0, r"\bsale\b(?!\s+of)|\bangebot(?:e|en)?\b|\bofertas?\b|\bofferte?\b|\bdeal\b",
     group="deal")
_cue("shopping", "cheap", 2.5,
     r"\bcheap\w*|\bg(?:ue|u)nstig\w*|\bbillig\w*|\bpreiswert\w*|\bpas cher\b|\bbon marche\b|"
     r"\bbarat[oa]s?\b|\bconveniente\b", group="cheap")
_cue("shopping", "budget", 1.5, r"\bbudget\b|\baffordable\b|\bbezahlbar\w*", group="cheap")
_cue("shopping", "shipping", 2.5,
     r"\bin stock\b|\bfree (?:shipping|delivery)\b|\bkostenloser versand\b|\bversandkostenfrei\b|"
     r"\blivraison gratuite\b|\benvio gratis\b|\bspedizione gratuita\b|\blieferbar\b")
_cue("shopping", "retail", 2.0,
     r"\bretailers?\b|\bh(?:ae|a)ndler\b|\bonline[- ]?shop\w*|\bwebshop\b|\bmarketplace\b|\bboutique\b|"
     r"\btienda online\b|\bnegozio online\b", group="retail")
_cue("shopping", "shop_word", 1.5, r"\bshops?\b|\bshopping\b|\btienda\b|\bnegozio\b|\bmagasin\b",
     group="retail")
_cue("shopping", "review_phrase", 3.0,
     r"\b(?:test|tests|testbericht\w*)\s*(?:und|&|and|,|/)?\s*erfahrungen\b|"
     r"\berfahrungen\s*(?:und|&|mit|,|/)?\s*(?:test|testbericht\w*)\b|\bavis (?:et|&) test\b|"
     r"\btest (?:et|&) avis\b|\bresenas? (?:y|&) opiniones\b|\bopiniones (?:y|&) resenas?\b|"
     r"\brecensioni? (?:e|&) opinioni\b|\bopinioni (?:e|&) recensioni?\b|\bcustomer reviews?\b|"
     r"\bproduct reviews?\b|\bkundenbewertung\w*|\bavis clients?\b|\bopiniones de clientes\b|"
     r"\bkundenmeinung\w*|\btestsieger\b|\bkaufempfehlung\b", group="review")
_cue("shopping", "review_word", 1.0,
     r"\breviews?\b|\btestbericht\w*|\bbewertung\w*|\brecension\w+|\bresenas?\b|\bcomparatif\b|"
     r"\bcomparativ[ao]\b|\bcomparison\b|\bvergleich\w*|\bunboxing\b", group="review")
_cue("shopping", "test_word", 0.5, r"\btests?\b|\bim test\b", group="review")
_cue("shopping", "versus", 1.0, r"\bvs\.?(?=\s|$)|\bversus\b|\bgegen\b(?=\s+[a-z]{3,}\s+(?:test|vergleich))")
_cue("shopping", "model_token", 1.5,
     r"\b(?!cve-|(?:fy|q|h)\d)[a-z]{1,5}-?(?!(?:19|20)\d\d\b)\d{2,5}[a-z]{0,3}\d?\b", req=(_DIGIT,),
     group="model")
_cue("shopping", "model_variant", 1.5,
     r"\b\d{1,3}\s?(?:pro|max|ultra|plus|mini|lite|se)\b", req=(_DIGIT,), group="model")
_cue("shopping", "spec_word", 1.0,
     r"\bspecs?\b|\bspecifications?\b|\bdatenblatt\b|\btechnische daten\b|\bfiche technique\b|"
     r"\bcaracteristicas\b|\bscheda tecnica\b|\bwarranty\b|\bgarantie\b|\bgarantia\b")
_cue("shopping", "best_category", 3.0,
     r"\b(?:best|top|cheapest|beste[nrms]?|besten|meilleur[es]?|mejor(?:es)?|migliore|migliori|miglior)\b"
     r"(?:\s+[a-z0-9-]+){0,4}?\s+" + _CAT_STRICT +
     r"(?!\s+(?:settings?|temperature|setup|mode|recipes?|time|tips|tricks|ideas|practices|angles?|lens))",
     group="best_category")
_cue("shopping", "category", 1.0,
     r"\b(?:headphones?|earbuds?|earphones?|headsets?|speakers?|soundbars?|tvs?|televisions?|monitors?|"
     r"laptops?|notebooks?|smartphones?|phones?|tablets?|cameras?|lenses|drones?|printers?|routers?|"
     r"vacuums?|fridges?|refrigerators?|washing machines?|dishwashers?|ovens?|air fryers?|"
     r"coffee machines?|espresso machines?|mattress(?:es)?|sofas?|desks?|office chairs?|shoes|sneakers|"
     r"jackets?|backpacks?|watches|smartwatch(?:es)?|bikes?|e-?bikes?|scooters?|tents?|sleeping bags?|"
     r"dacs?|amplifiers?|iems?|turntables?|subwoofers?|receivers?|preamps?|oled|qled|mini-?led|4k|8k|"
     r"kopfh(?:oe|o)rer|lautsprecher|fernseher|staubsauger\w*|waschmaschine\w*|k(?:ue|u)hlschrank\w*|"
     r"matratze\w*|schuhe|rucksack\w*|fahrrad\w*|casques?|ecouteurs?|enceintes?|televiseurs?|"
     r"ordinateurs? portables?|aspirateurs?|lave-linge|refrigerateurs?|matelas|chaussures|"
     r"sacs? a dos|montres?|velos?|auriculares|altavoces|televisor\w*|portatil\w*|aspiradora\w*|"
     r"lavadora\w*|frigorifico\w*|colchon\w*|zapatillas|mochilas?|relojes?|bicicletas?|cuffie|"
     r"altoparlanti|televisori|aspirapolvere|lavatrice|frigorifero|materasso|scarpe|zaino|orologi[o]?|"
     r"biciclett\w+)\b")


# ---------------------------------------------------------------------------
# Compilation (once, at import)
# ---------------------------------------------------------------------------

def _split_top(pattern: str) -> List[str]:
    """Split ``pattern`` at top-level ``|`` (outside groups and character classes)."""
    parts: List[str] = []
    depth = 0
    in_class = False
    start = 0
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "^":
                i += 1
            if pattern[i + 1:i + 2] == "]":
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append(pattern[start:i])
            start = i + 1
        i += 1
    parts.append(pattern[start:])
    return parts


def _hoist(pattern: str) -> str:
    """``\\ba\\b|\\bb\\b`` -> ``\\b(?:a\\b|b\\b)``: same language, but every alternative
    now starts with a literal, which lets the regex engine reject most positions at once."""
    alts = _split_top(pattern)
    bounded = [a[2:] for a in alts if a.startswith("\\b")]
    if len(bounded) < 2:
        return pattern
    others = [a for a in alts if not a.startswith("\\b")]
    return "\\b(?:" + "|".join(bounded) + ")" + "".join("|" + a for a in others)


def _skip_group(alt: str, i: int) -> int:
    """Index just past the parenthesised group that starts at ``alt[i]``."""
    depth = 0
    in_class = False
    while i < len(alt):
        ch = alt[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _leading_literal(text: str, i: int) -> str:
    """Plain alphanumeric run at ``text[i:]`` that every match must begin with."""
    run = ""
    while i < len(text) and text[i].isalnum() and text[i].isascii():
        nxt = text[i + 1:i + 2]
        if nxt in ("?", "*", "{"):
            break
        run += text[i]
        i += 1
        if nxt == "+":
            break
    return run


def _alt_prefixes(alt: str, depth: int = 0) -> Optional[Tuple[str, ...]]:
    """Three-letter prefixes (one per expansion) that a match of ``alt`` must start with.

    ``alt`` is one top-level alternative. Leading ``\\b`` and look-behinds are
    skipped, and a leading ``(?:x|y)`` group is distributed over its choices.
    Returns None when some start of a match is not a literal of at least three
    plain alphanumerics; such alternatives are tried on every query.
    """
    i = 0
    while True:
        if alt.startswith("\\b", i):
            i += 2
        elif alt.startswith("(?<", i):
            i = _skip_group(alt, i)
        else:
            break
    if alt.startswith("(?:", i) and depth < 3:
        end = _skip_group(alt, i)
        if alt[end:end + 1] in ("?", "*", "+", "{"):
            return None
        out: List[str] = []
        for choice in _split_top(alt[i + 3:end - 1]):
            sub = _alt_prefixes(choice + alt[end:], depth + 1)
            if sub is None:
                return None
            out.extend(sub)
        return tuple(out)
    run = _leading_literal(alt, i)
    return (run[:3],) if len(run) >= 3 else None


class _Cue:
    __slots__ = ("intent", "name", "weight", "group", "multi", "req", "raw")

    def __init__(self, spec: _Spec) -> None:
        self.intent = spec.intent
        self.name = spec.name
        self.weight = spec.weight
        self.group = spec.group
        self.multi = spec.multi
        self.req = spec.req
        self.raw = spec.raw


def _build() -> Tuple[List[_Cue], List[Tuple[int, "re.Pattern[str]"]], Dict[str, Tuple[int, ...]], Tuple[int, ...]]:
    """Compile the cue table.

    Every cue becomes up to two *units* (a cue index and a regex): one for the
    alternatives whose first three letters are known (tried only when the query
    has a word starting with one of those prefixes) and one for the rest
    (tried on every query).
    """
    cues: List[_Cue] = []
    units: List[Tuple[int, "re.Pattern[str]"]] = []
    by_prefix: Dict[str, List[int]] = {}
    always: List[int] = []

    def add(cue_idx: int, alts: List[str], label: str, flags: int = 0) -> int:
        rx = re.compile(_hoist("|".join(alts)), flags)
        if rx.groups:
            raise ValueError(f"cue {label} has a capturing group")
        units.append((cue_idx, rx))
        return len(units) - 1

    for spec in _SPECS:
        cue_idx = len(cues)
        cues.append(_Cue(spec))
        label = f"{spec.intent}:{spec.name}"
        if spec.raw:
            always.append(add(cue_idx, [spec.pattern], label, flags=0))
            continue
        if spec.trig is not None:
            unit = add(cue_idx, [spec.pattern], label)
            for prefix in spec.trig:
                by_prefix.setdefault(prefix, []).append(unit)
            continue
        known: List[str] = []
        unknown: List[str] = []
        prefixes: List[str] = []
        for alt in _split_top(spec.pattern):
            found = _alt_prefixes(alt)
            if found is None:
                unknown.append(alt)
            else:
                known.append(alt)
                prefixes.extend(found)
        if known:
            unit = add(cue_idx, known, label)
            for prefix in dict.fromkeys(prefixes):
                by_prefix.setdefault(prefix, []).append(unit)
        if unknown:
            always.append(add(cue_idx, unknown, label))
    return cues, units, {k: tuple(v) for k, v in by_prefix.items()}, tuple(always)


_CUES, _UNITS, _BY_PREFIX, _ALWAYS = _build()
_WORD = re.compile(r"[a-z0-9]+")
_PLACE = re.compile(
    r"\b(?:in|near|nearby|bei|nahe|dans|pres de|cerca de|vicino a|a|en)\s+[A-Z\u00c0-\u00dc][\w'-]+"
)
_HAS_DIGIT = re.compile(r"\d")
_COMBINING = re.compile("[\u0300-\u036f]")
_TRANSLATE = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u00b4": "'", "\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-",
})
_EMPTY = IntentDecision("general", 0.0, ())


def _normalize(query: str) -> str:
    text = query[:_MAX_QUERY_CHARS].casefold().translate(_TRANSLATE)
    if not text.isascii():
        text = _COMBINING.sub("", unicodedata.normalize("NFKD", text))
    return " ".join(text.split())


def _candidates(text: str) -> List[int]:
    """Indices (table order) of the units worth trying on ``text``."""
    found = set(_ALWAYS)
    for prefix in {w[:3] for w in _WORD.findall(text)}:
        hit = _BY_PREFIX.get(prefix)
        if hit:
            found.update(hit)
    return sorted(found)


def _evaluate(
    unit_indices: List[int], text: str, raw: str
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, List[str]]]:
    """Run the given units; return per-intent score, strongest single group, and cue names that fired."""
    has_digit = _HAS_DIGIT.search(text) is not None
    has_upper = raw != raw.lower()
    hits: Dict[int, set] = {}
    for u in unit_indices:
        cue_idx, rx = _UNITS[u]
        cue = _CUES[cue_idx]
        if cue.req and not any(
            has_digit if sub is _DIGIT else has_upper if sub is _UPPER else sub in text for sub in cue.req
        ):
            continue
        target = raw if cue.raw else text
        if cue.multi:
            found = {m.group(0) for m in rx.finditer(target)}
            if found:
                hits.setdefault(cue_idx, set()).update(found)
        elif rx.search(target) is not None:
            hits.setdefault(cue_idx, set())
    best: Dict[str, Dict[str, float]] = {}
    fired: Dict[str, List[str]] = {}
    for cue_idx in sorted(hits):
        cue = _CUES[cue_idx]
        weight = cue.weight
        if cue.multi:
            weight *= 1.0 + 0.5 * (min(len(hits[cue_idx]), 3) - 1)
        groups = best.setdefault(cue.intent, {})
        fired.setdefault(cue.intent, []).append(cue.name)
        if weight > groups.get(cue.group, 0.0):
            groups[cue.group] = weight
    if "local" in fired and _PLACE.search(raw) is not None:
        fired["local"].append("place_name")
        best["local"]["place_name"] = 1.0
    scores = {intent: sum(groups.values()) for intent, groups in best.items()}
    peaks = {intent: max(groups.values()) for intent, groups in best.items()}
    return scores, peaks, fired


def _decide(scores: Dict[str, float], peaks: Dict[str, float], fired: Dict[str, List[str]]) -> IntentDecision:
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], _TIE_ORDER.index(kv[0])))
    for intent, score in ranked:
        if score < _THRESHOLD[intent]:
            continue
        other = max((s for i, s in scores.items() if i != intent), default=0.0)
        if intent in PRECISE_INTENTS and (
            score - other < _PRECISE_MARGIN or peaks[intent] < _MIN_ANCHOR
        ):
            continue
        x = max(0.0, score - 0.5 * other)
        return IntentDecision(
            intent, round(x / (x + 2.0), 3), tuple(f"{intent}:{n}" for n in fired[intent])
        )
    top = max(scores.values(), default=0.0)
    confidence = round(0.55 - 0.4 * min(1.0, top / 3.0), 3)
    signals = tuple(f"{i}:{n}" for i in INTENTS if i in fired for n in fired[i])
    return IntentDecision("general", confidence, signals)


def classify_intent(query: str) -> IntentDecision:
    """Classify ``query`` into one of :data:`INTENTS`.

    Never raises for odd input: a non-string or blank query is ``general`` with
    confidence 0.0.
    """
    if not isinstance(query, str) or not query.strip():
        return _EMPTY
    raw = query[:_MAX_QUERY_CHARS]
    text = _normalize(raw)
    scores, peaks, fired = _evaluate(_candidates(text), text, raw)
    return _decide(scores, peaks, fired)


def _classify_exhaustive(query: str) -> IntentDecision:
    """Reference implementation: try every cue (no prefix dispatch). Used by the
    tests to prove the dispatch never skips a cue that would have fired."""
    if not isinstance(query, str) or not query.strip():
        return _EMPTY
    raw = query[:_MAX_QUERY_CHARS]
    text = _normalize(raw)
    scores, peaks, fired = _evaluate(list(range(len(_UNITS))), text, raw)
    return _decide(scores, peaks, fired)
