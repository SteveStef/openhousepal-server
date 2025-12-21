import time
import asyncio

class RateLimiter:
    def __init__(self):
        self.max_tokens = 5  # Maximum requests per second
        self.tokens_per_second = 5  # Refill rate (tokens added per second)
        self.bucket = self.max_tokens
        self.last_refill = time.time()
        self.lock = asyncio.Lock()

    async def acquire_token(self):
        async with self.lock:
            while True:
                curr_time = time.time()
                seconds_elapsed = curr_time - self.last_refill

                # Refill tokens based on elapsed time (proportional refill)
                tokens_to_add = seconds_elapsed * self.tokens_per_second
                self.bucket = min(self.max_tokens, self.bucket + tokens_to_add)
                self.last_refill = curr_time

                if self.bucket >= 1:
                    self.bucket -= 1
                    return

                # Calculate wait time until next token is available
                wait_time = (1 - self.bucket) / self.tokens_per_second
                await asyncio.sleep(wait_time)

