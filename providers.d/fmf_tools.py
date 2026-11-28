import json
from wsp_core.providers import make_request

DEFAULT_EXTRACT_URL = "https://mpp-agent-data.fmf-x402.workers.dev/mpp/web/scrape"


def extract_fmf_tools(url, max_chars=4000, api_url=DEFAULT_EXTRACT_URL):
    """
    Extract clean text/markdown from a URL using FMF Tools.

    Parameters
    ----------
    url : str
        The URL to extract content from.
    max_chars : int, optional
        Maximum number of characters to return (default 4000).
    api_url : str, optional
        The FMF Tools extraction endpoint. Defaults to the public endpoint.

    Returns
    -------
    dict
        A dictionary containing at least ``markdown`` and optionally ``raw_html`` and ``metadata``.
    """
    payload = {"url": url, "max_chars": max_chars}
    body = json.dumps(payload).encode("utf-8")
    response = make_request(api_url, headers={}, body=body)
    # FMF Tools returns a JSON with keys: markdown, rawHtml, metadata
    return {
        "markdown": response.get("markdown"),
        "raw_html": response.get("rawHtml"),
        "metadata": response.get("metadata", {}),
    }
