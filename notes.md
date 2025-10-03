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
- Make the database persist on docker rebuild on the VPS and add backups to this
- After updating preferences, make a zillow request to refresh the properties with the new preferences (test)
- Make the active tag work based off the visitors last visiting
- Paypal payments
- Send emails
- Add retry logic to the zillow service
- Fix the dates on the comments
- Fix the preferences modal diameter not being able to be blank (also not intuitive for switching the type of search)
