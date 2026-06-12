"""Central configuration for basketaraba scraper and site builder."""

# Base URL for all endpoints
BASE_URL = "https://YOUR_URL_HERE/folder

LOGOS_URL = f"{BASE_URL}/folder/items/"
ACTA_URL_TEMPLATE = f"{BASE_URL}/subfolder/{{item_id}}.ext"


def acta_url(partido_id: str) -> str:
    return ACTA_URL_TEMPLATE.format(partido_id=partido_id)
