import pytest
import yaml

from jobradar.config import ROOT
from jobradar.pipeline.title_match import TitleMatcher


@pytest.fixture(scope="module")
def m():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return TitleMatcher.from_config(cfg["title_filter"])


@pytest.mark.parametrize(
    "title",
    [
        "Head of Strategy",
        "Head of Marketing",
        "Marketing Director",
        "Brand Director",
        "Brand Lead",
        "Brand Leader",
        "Head of Brand",
        "Head of Brand & Communications",
        "CMO",
        "CMO / Head of Marketing (VPN)",
        "Head of Marketing, CMO",
        "Chief Marketing Officer / Head of Acquisition",
        "Head of Marketing and Growth — Startup Studio",
        "Head of Marketing/СМО",
        "Director of Marketing",
        "VP of Marketing",
        "Strategy Director",
        "Strategic Director",
        "Стратегічний директор",
        "Стратегічна директорка",
        "Директор з маркетингу",
        "Директорка з маркетингу",
        "Маркетинг-директор",
        "Маркетинг директор",
        "Бренд-директор",
        "Директор зі стратегії",
        "Керівник відділу маркетингу",
        "Керівниця напряму бренд-маркетингу",
        "Директор по маркетингу",
        "Senior Brand Lead (FMCG)",
    ],
)
def test_target_titles_match(m, title):
    ok, why = m.match(title)
    assert ok, f"{title!r} should match ({why})"


@pytest.mark.parametrize(
    "title",
    [
        "Head of Performance Marketing",
        "Head of SMM",
        "Head of CRM / Retention (TR 1)",
        "Client Service Director (marketing agency)",
        "Head of Operations - New iGaming Brand",
        "Global Business Development Director",
        "SEO Director (SEO, AEO, GEO)",
        "Head of Sales",
        "Marketplace Marketing Specialist",
        "Brand Manager",
        "Бренд-менеджер",
        "Маркетолог",
        "Lead Generation Marketing Manager",
        "Head of Affiliate Marketing",
        "Account Director",
        "Head of Media Buying",
    ],
)
def test_noise_titles_rejected(m, title):
    ok, why = m.match(title)
    assert not ok, f"{title!r} should be rejected"
