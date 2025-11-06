import time
import asyncio

class RateLimiter:
    def __init__(self):
        self.max_tokens = 2 # this is how many requests per second
        self.bucket = self.max_tokens 
        self.last_request = time.time()
        self.lock = asyncio.Lock()

    async def acquire_token(self):
        async with self.lock:
            while True:
                curr_time = time.time()
                seconds_elapsed = curr_time - self.last_request

                if seconds_elapsed >= 1:
                    self.bucket = self.max_tokens

                if self.bucket > 0:
                    self.bucket -= 1
                    self.last_request = time.time()
                    return

                wait_time = 1 - seconds_elapsed
                await asyncio.sleep(wait_time)

