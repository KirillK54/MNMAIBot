from types import SimpleNamespace

from vpi_bot.models import Country
from vpi_bot.ui.views import CountryPanel, CountrySelectView


def test_country_panel_and_selector_fit_discord_component_limits():
    bot = SimpleNamespace()
    countries = [Country(id=i, name=f"Государство {i}") for i in range(1, 31)]
    selector = CountrySelectView(bot, owner_id=999, countries=countries)
    assert len(selector.children) == 1
    assert len(selector.children[0].options) == 25
    panel = CountryPanel(bot, country_id=1, owner_id=999)
    assert len(panel.children) == 10
    assert any(getattr(child, "label", None) == "Как играть" for child in panel.children)
    assert all(child.row is None or child.row in {0, 1, 2, 3} for child in panel.children)
