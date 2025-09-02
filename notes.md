
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

Current problem:
1. the share collection puts collection in the name instead of showcase
2. Change the link to showcase and backend endpoint

Add functionality for the following:
1. Removing open-houses (what should happen after this?) -> deactivate the collection
2. Replace the viewed section on collection card to interactions
3. Make the active now work propery based off the visior last time visiting
4. Put the phone number somewhere and make the email not truncade
5. Fix the buyer visiting the showcase
6. Likes/dislikes/favorites
7. Property details
8. Showcase preferences invoking showcase refresh?
