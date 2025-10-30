import sys
import os 
import asyncio 
from dotenv import load_dotenv
load_dotenv()

from app.services.paypal_service import paypal_service 
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

async def test() -> bool:
    try:
        token = await paypal_service.get_token()
        return True
    except Exeption as e:
        print(e)
        return False

if __name__ == '__main__':
    a = asyncio.run(test())
    if a:
        print("The client ID is valid")
    else:
        print("The client ID is bad")
