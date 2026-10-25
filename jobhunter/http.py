"""Small JSON-over-HTTP helper shared by the job feeds and the geo services."""
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "jobhunter/0.1 (+https://github.com/alexstoicu/jobhunter)"


def request_json(url, payload=None, retries=2, timeout=30):
    """GET (or POST when payload is given) and decode JSON. Retries on 429/5xx."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise
