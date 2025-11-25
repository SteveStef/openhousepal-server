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

Things that still need to be tested:
1. Getting emails when property updates/new property
2. All email links (should work)
