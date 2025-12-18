## How Subscriptions work
1. Signup with standard/premium, you have free trials for 30 days
2. If you upgrade or downgrade, you do not need to put any credit card details in, it does it automatically (only when you are on the free trial)
3. If you cancel during the trial, you have the remaining trial time access to keep using the same tier you had.
4. However if you resubscribe (even during the trial period you have to pay for the subscription without a trial)
5. now after resubscribing to basic, I then upgrade to premium, it does not charge the user, but the premium charges takes place on the curret billing cycle, while giving the user access to the premium features
6. If you cancel, even if the month is not over, you no longer have access to anything

Potential problem
When I upgrade, to premium from the basic (active, not trial) it does not charge me. Infinite premium after downgrade?


## How I want the next steps to look:
1. Build a PDF for similar properties (maybe)
###  Try new idea (extend the similar property radius then do the following) 
2. When client signs into open house 
    - use the description from the property visited + metadata
    - build combined description string
    - Generate embeddings and store in the vector database
    - Query top-N similar property vectors
    - Have these properties in the collection

Next Steps:
1. During property sync, invalidate the property details cache for only the properties that are in the collection. (implemented but needs testing)
2. Then use a staggering property sync for each collection.  (done, it runs every hour, 10 at a time, last_sync sorted each run)
3. Then for the properties that stay in the colletion that are no longer for sale, add UI to indicate that it's no longer available (implemented but needs testing)

Now after the video ad is completed, push frontend and backend to prod, and update the env aswell


Make new zillow service file that will use this API instead: https://rapidapi.com/oneapiproject/api/zllw-working-api/playground/apiendpoint_ac412114-613e-4305-a20b-ad2b0eb3984a
Make the output compatible with my current fields in the database

This API has more requests per month with:
    - $20 - 5req/sec
    - $60 - 25req/sec
    - $250 - 50req/sec

Just fixed a critical bug:
1. When I updated the address, it will now update the coordinates so that the search works (It was making coordinate search when I updated it to search for address)

Current Problems:
1. The township search does not work for the new API and it is a piece of functionality that I cannot afford to lose. May need to revert back to old api for either primary or just to get the township but are we okay with spending 20 more dollars? it would also allow more fault tolorance
2. Make sure the new radius works properly for the new API as it it being multiplied by 1.8 then divided by 2.

The viewed property is not working. It is not counting as a view for the agent view or the client to no longer display (new)


