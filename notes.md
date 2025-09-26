Here is the flow:
1. Agent signup/login (makes a entry in users table)
2. Agent makes a open house pdf (creates open house event entry)
3. Buyer fills out form -> (creates open house visitor and collection and fills collection with related properties)
3.5. in the background, the collection auto refreshes with the collection preferences
4. Agent make the link public and sends to client the link
5. Buyer likes, dislikes, and comments on properties.
6. Agent removes properties from collection.
7. Buyer requests a tour of a house
8. After finished with collection, agent deactives it

Next Steps:
4. After updating preferences, make a zillow request to refresh the properties with the new preferences (test)
5. Check if the property details actually delete after 48 hours
6. Make the active tag work based off the visitors last visiting
7. Paypal payments



This is the problem: 
ValueError: the greenlet library is required to use this function. No module named 'greenlet'
