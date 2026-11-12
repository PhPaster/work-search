from jobradar.collectors.djinni import DjinniCollector, apply_ldjson, parse_job_ldjson, parse_rss
from jobradar.collectors.email_alerts import SITES, parse_alert_message
from jobradar.models import Vacancy


def test_djinni_rss(fixture_bytes):
    items = parse_rss(fixture_bytes("djinni_rss.xml"), query="q")
    assert len(items) == 5
    v = items[0]
    assert v.source == "djinni"
    assert v.source_id.isdigit()
    assert v.url.startswith("https://djinni.co/jobs/")
    assert v.title
    assert len(v.description) > 100 and "<p>" not in v.description
    assert v.posted_at.startswith("20")


def test_djinni_ldjson(fixture_text):
    d = parse_job_ldjson(fixture_text("djinni_job.html"))
    assert d and d["@type"] == "JobPosting"
    v = apply_ldjson(Vacancy("djinni", "1", "u", "t"), d)
    assert v.company == "SBC Performance"
    assert v.remote is True


def test_djinni_ldjson_salary_and_location():
    d = {
        "@type": "JobPosting",
        "hiringOrganization": {"name": "Acme"},
        "jobLocation": [{"address": {"addressLocality": "Kyiv"}}, {"address": {"addressLocality": "Lviv"}}],
        "baseSalary": {"currency": "USD", "value": {"minValue": 3000, "maxValue": 4500}},
    }
    v = apply_ldjson(Vacancy("djinni", "1", "u", "t"), d)
    assert (v.salary_from, v.salary_to, v.currency) == (3000, 4500, "USD")
    assert v.city == "Kyiv, Lviv"
    assert v.salary_text() == "3000–4500 USD"


class _FakeResp:
    def __init__(self, status, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


class _FakeHttp:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, []

    def get(self, url):
        self.calls.append(url)
        for key, resp in self.mapping.items():
            if key in url:
                return resp
        return _FakeResp(404)


def test_djinni_collector_dedups_across_searches_and_survives_one_failure(fixture_bytes):
    rss = fixture_bytes("djinni_rss.xml")
    http = _FakeHttp({"CMO": _FakeResp(200, rss), "Head": _FakeResp(200, rss), "broken": _FakeResp(503)})
    c = DjinniCollector(
        http, [{"all_keywords": "CMO"}, {"all_keywords": "Head"}, {"all_keywords": "broken"}]
    )
    items = c.collect()
    assert len(items) == 5  # same 5 items from two searches -> deduplicated
    assert len(http.calls) == 3


def test_workua_alert_email(fixture_bytes):
    items = parse_alert_message(fixture_bytes("email_workua.eml"), SITES["workua"])
    assert [v.source_id for v in items] == ["7012345", "7012399"]
    v = items[0]
    assert v.title == "Маркетинг-директор"
    assert v.url == "https://www.work.ua/jobs/7012345/"
    assert v.description_is_snippet
    assert "Смачна Кава" in v.description and "Детальніше" not in v.description
    assert (v.salary_from, v.salary_to, v.currency) == (60000, 90000, "UAH")
    assert items[1].title == "Head of Brand"
    assert "unsubscribe" not in " ".join(x.url for x in items)


def test_robotaua_alert_email_with_tracking_links(fixture_bytes):
    items = parse_alert_message(fixture_bytes("email_robotaua.eml"), SITES["robotaua"])
    assert [v.source_id for v in items] == ["10555777", "10555888"]
    v = items[0]
    assert v.title == "Директор з маркетингу"  # image link and "Відгукнутися" are skipped
    assert v.url == "https://robota.ua/company3301/vacancy10555777/"
    assert "Defence Tech Co" in v.description
    assert (v.salary_from, v.currency) == (4000, "USD")
