"""Spam/mirror-domain filtering and domain-diversity reranking."""

import quality
import search


def _result(url, title="t"):
    return {"url": url, "title": title, "description": "snippet text"}


class TestFilterSpamResults:
    def test_removes_known_mirror_domains(self):
        results = [
            _result("https://stackoverflow.com/q/1"),
            _result("https://newbedev.com/some-copied-answer"),
            _result("https://githubmemory.com/repo/issue"),
        ]

        kept, removed = quality.filter_spam_results(results)

        assert [r["url"] for r in kept] == ["https://stackoverflow.com/q/1"]
        assert removed == ["githubmemory.com", "newbedev.com"]

    def test_matches_www_and_subdomains(self):
        results = [
            _result("https://www.newbedev.com/x"),
            _result("https://de.newbedev.com/x"),
        ]

        kept, removed = quality.filter_spam_results(results)

        assert kept == []
        assert removed == ["de.newbedev.com", "newbedev.com"]

    def test_lookalike_registrations_are_not_false_positives(self):
        # A blocked domain used as a prefix of an unrelated registration must
        # NOT match: only the exact domain or true subdomains are blocked.
        results = [
            _result("https://newbedev.com.evil.example/post"),
            _result("https://newbedev.community/post"),
        ]

        kept, removed = quality.filter_spam_results(results)

        assert [r["url"] for r in kept] == [
            "https://newbedev.com.evil.example/post",
            "https://newbedev.community/post",
        ]
        assert removed == []

    def test_blocklist_covers_live_sighted_mirrors(self):
        results = [
            _result("https://fixmycodeerror.com/q"),
            _result("https://stacklesson.com/q"),
            _result("https://docs.w3cub.com/python~3/library/ssl"),
        ]

        kept, removed = quality.filter_spam_results(results)

        assert kept == []
        assert removed == ["docs.w3cub.com", "fixmycodeerror.com", "stacklesson.com"]

    def test_extra_blocked_domains_from_config(self):
        results = [_result("https://content-farm.example/post")]

        kept, removed = quality.filter_spam_results(results, extra_blocked=["content-farm.example"])

        assert kept == []
        assert removed == ["content-farm.example"]

    def test_allowed_domains_rescue_blocked_entries(self):
        results = [_result("https://newbedev.com/x")]

        kept, removed = quality.filter_spam_results(results, allowed=["newbedev.com"])

        assert len(kept) == 1
        assert removed == []

    def test_clean_results_pass_through_unchanged(self):
        results = [_result("https://docs.python.org/3/"), _result("https://github.com/x/y")]

        kept, removed = quality.filter_spam_results(results)

        assert kept == results
        assert removed == []


class TestRerankDomainDiversity:
    def test_third_result_from_same_domain_is_demoted(self):
        results = [
            _result("https://a.example/1"),
            _result("https://a.example/2"),
            _result("https://a.example/3"),
            _result("https://b.example/1"),
        ]

        reranked, demoted = quality.rerank_domain_diversity(results, max_per_domain=2)

        assert [r["url"] for r in reranked] == [
            "https://a.example/1",
            "https://a.example/2",
            "https://b.example/1",
            "https://a.example/3",
        ]
        assert demoted == 1

    def test_short_lists_are_untouched(self):
        results = [_result("https://a.example/1"), _result("https://a.example/2")]

        reranked, demoted = quality.rerank_domain_diversity(results, max_per_domain=1)

        assert reranked == results
        assert demoted == 0

    def test_diverse_lists_keep_order(self):
        results = [
            _result("https://a.example/1"),
            _result("https://b.example/1"),
            _result("https://c.example/1"),
        ]

        reranked, demoted = quality.rerank_domain_diversity(results)

        assert reranked == results
        assert demoted == 0


class TestResultQualityPipeline:
    def test_pipeline_filters_and_reports_metadata(self):
        result = {"results": [
            _result("https://stackoverflow.com/q/1"),
            _result("https://newbedev.com/copy"),
            _result("https://docs.python.org/3/"),
        ]}

        search._apply_result_quality_pipeline(result, config={})

        assert [r["url"] for r in result["results"]] == [
            "https://stackoverflow.com/q/1",
            "https://docs.python.org/3/",
        ]
        assert result["metadata"]["spam_filtered"] == {
            "removed_count": 1,
            "domains": ["newbedev.com"],
        }

    def test_pipeline_applies_domain_diversity_cap(self):
        result = {"results": [
            _result("https://a.example/1"),
            _result("https://a.example/2"),
            _result("https://a.example/3"),
            _result("https://b.example/1"),
        ]}

        search._apply_result_quality_pipeline(result, config={})

        assert [r["url"] for r in result["results"]][:3] == [
            "https://a.example/1", "https://a.example/2", "https://b.example/1",
        ]
        assert result["metadata"]["domain_diversity_demoted"] == 1

    def test_spam_filter_can_be_disabled_via_config(self):
        result = {"results": [_result("https://newbedev.com/copy")]}

        search._apply_result_quality_pipeline(result, config={"quality": {"filter_spam": False}})

        assert len(result["results"]) == 1
        assert "metadata" not in result

    def test_diversity_cap_can_be_disabled_via_config(self):
        result = {"results": [
            _result("https://a.example/1"),
            _result("https://a.example/2"),
            _result("https://a.example/3"),
        ]}

        search._apply_result_quality_pipeline(result, config={"quality": {"max_results_per_domain": 0}})

        assert [r["url"] for r in result["results"]] == [
            "https://a.example/1", "https://a.example/2", "https://a.example/3",
        ]

    def test_allowed_domains_config_rescues_domain(self):
        result = {"results": [_result("https://newbedev.com/copy")]}

        search._apply_result_quality_pipeline(
            result, config={"quality": {"allowed_domains": ["newbedev.com"]}},
        )

        assert len(result["results"]) == 1

    def test_site_query_skips_domain_diversity_rerank(self):
        # A deliberately one-domain query must keep its provider order.
        result = {"results": [
            _result("https://github.com/x/1"),
            _result("https://github.com/x/2"),
            _result("https://github.com/x/3"),
            _result("https://randomblog.example/post"),
        ]}

        search._apply_result_quality_pipeline(
            result, config={}, query="site:github.com fastapi upload example",
        )

        assert [r["url"] for r in result["results"]] == [
            "https://github.com/x/1",
            "https://github.com/x/2",
            "https://github.com/x/3",
            "https://randomblog.example/post",
        ]
        assert "metadata" not in result

    def test_include_domains_skip_diversity_rerank(self):
        result = {"results": [
            _result("https://arxiv.org/abs/1"),
            _result("https://arxiv.org/abs/2"),
            _result("https://arxiv.org/abs/3"),
            _result("https://other.example/x"),
        ]}

        search._apply_result_quality_pipeline(
            result, config={}, query="attention is all you need", include_domains=["arxiv.org"],
        )

        assert [r["url"] for r in result["results"]][:3] == [
            "https://arxiv.org/abs/1",
            "https://arxiv.org/abs/2",
            "https://arxiv.org/abs/3",
        ]

    def test_site_query_for_blocked_domain_wins_over_blocklist(self):
        # Explicitly searching a blocked domain expresses intent: keep it.
        result = {"results": [_result("https://newbedev.com/copy")]}

        search._apply_result_quality_pipeline(
            result, config={}, query="site:newbedev.com some query",
        )

        assert len(result["results"]) == 1

    def test_unconstrained_queries_still_get_diversity_rerank(self):
        result = {"results": [
            _result("https://a.example/1"),
            _result("https://a.example/2"),
            _result("https://a.example/3"),
            _result("https://b.example/1"),
        ]}

        search._apply_result_quality_pipeline(
            result, config={}, query="fastapi upload file example",
        )

        assert result["metadata"]["domain_diversity_demoted"] == 1


class TestExtractDomainConstraints:
    def test_collects_site_operators_and_include_domains(self):
        constraints = quality.extract_domain_constraints(
            "site:github.com site:Docs.Python.org asyncio", include_domains=["arxiv.org"],
        )

        assert constraints == ["arxiv.org", "docs.python.org", "github.com"]

    def test_plain_queries_have_no_constraints(self):
        assert quality.extract_domain_constraints("fastapi upload file example") == []
        assert quality.extract_domain_constraints("", include_domains=None) == []
