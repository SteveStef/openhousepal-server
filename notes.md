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
1. Does the preferences include the address that was used? If so then display it in the preferences modal
2. Test the City/Township search when creating a collection
3. Make a way to delete the open-house PDF's
4. Create a way to delete the collections
5. After updating preferences, make a zillow request to refresh the properties with the new preferences
6. Check if the property details actually delete after 48 hours
7. Make the active tag work based off the visitors last visiting
8. Paypal payments
