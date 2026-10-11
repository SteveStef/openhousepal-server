import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.bright_mls_service import bright_mls_service


async def main() -> None:
    token = await bright_mls_service._get_access_token()
    response = await bright_mls_service.client.get(
        f"{bright_mls_service.api_base_url}/$metadata",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
    )
    text = response.text
    print(text[:1200])
    print("\nEntitySet names:")
    print(re.findall(r"<EntitySet\s+Name=\"([^\"]+)\"", text)[:80])
    print("\nEntityType names containing history/price/change:")
    names = re.findall(r"<EntityType\s+Name=\"([^\"]+)\"", text)
    print([name for name in names if re.search(r"history|price|change", name, re.I)][:80])
    print("\nProperty names containing history/price/change:")
    props = re.findall(r"<Property\s+Name=\"([^\"]+)\"", text)
    print(sorted(set([name for name in props if re.search(r"history|price|change", name, re.I)]))[:120])
    await bright_mls_service.close()


if __name__ == "__main__":
    asyncio.run(main())
